"""SQLite 持久化：基础地图 + 不可变方案版本。

- meta 表存基础地图（JSON，仅初始化时写入一次）；
- versions 表存每个方案版本的完整归属快照，只插不改，保证旧版本不漂移；
- 所有写操作在模块级锁内完成，提交时在同一临界区校验基准版本并落库。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER,
    transfers  TEXT NOT NULL,
    assignment TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        self._conn.close()

    # ---- 基础地图 ----

    def get_map(self) -> Optional[dict]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'map'").fetchone()
        return json.loads(row["value"]) if row else None

    def save_map(self, map_obj: dict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('map', ?)",
                (json.dumps(map_obj, ensure_ascii=False),),
            )

    # ---- 方案版本 ----

    def create_version(self, parent_id: Optional[int], transfers: list, assignment: dict) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO versions (parent_id, transfers, assignment, created_at) VALUES (?, ?, ?, ?)",
                (
                    parent_id,
                    json.dumps(transfers, ensure_ascii=False),
                    json.dumps(assignment, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "transfers": json.loads(row["transfers"]),
            "assignment": json.loads(row["assignment"]),
            "created_at": row["created_at"],
        }

    def get_version(self, version_id: int) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        return self._row_to_version(row) if row else None

    def latest_version_id(self) -> Optional[int]:
        row = self._conn.execute("SELECT MAX(id) AS id FROM versions").fetchone()
        return row["id"]

    def list_versions(self) -> List[dict]:
        rows = self._conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
        return [self._row_to_version(r) for r in rows]
