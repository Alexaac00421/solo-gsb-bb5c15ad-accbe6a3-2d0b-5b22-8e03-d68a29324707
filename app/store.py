"""SQLite 持久化层。

- 基础地图与每个方案版本只插不改，历史版本不可漂移。
- 写操作使用 BEGIN IMMEDIATE 事务串行化，配合版本号比对实现乐观并发：
  并发提交同一基准版本时只有一个成功，其余收到 409 stale_base_version。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    map_id          TEXT PRIMARY KEY,
    blocks          TEXT NOT NULL,   -- JSON {街区: 人口}
    edges           TEXT NOT NULL,   -- JSON [[街区A, 街区B], ...]（规范化）
    districts       TEXT NOT NULL,   -- JSON {片区: {min_population, max_population}}
    current_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    map_id     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    assignment TEXT NOT NULL,        -- JSON {街区: 片区}，不可变快照
    created_at TEXT NOT NULL,
    PRIMARY KEY (map_id, version)
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "./data/census.db")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


class MapExistsError(Exception):
    """地图标识已存在。"""


class StaleVersionError(Exception):
    """基准版本不是当前最新版本。"""

    def __init__(self, current_version: int):
        super().__init__(f"current version is {current_version}")
        self.current_version = current_version


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def create_map(conn, *, map_id, blocks, edges, districts, assignment) -> None:
    now = _utcnow()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO maps (map_id, blocks, edges, districts, current_version, created_at)"
            " VALUES (?, ?, ?, ?, 1, ?)",
            (map_id, json.dumps(blocks), json.dumps(edges), json.dumps(districts), now),
        )
        conn.execute(
            "INSERT INTO versions (map_id, version, assignment, created_at) VALUES (?, 1, ?, ?)",
            (map_id, json.dumps(assignment), now),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        _rollback_quietly(conn)
        raise MapExistsError() from exc
    except Exception:
        _rollback_quietly(conn)
        raise


def get_map(conn, map_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM maps WHERE map_id = ?", (map_id,)).fetchone()
    if row is None:
        return None
    return {
        "map_id": row["map_id"],
        "blocks": json.loads(row["blocks"]),
        "edges": [tuple(e) for e in json.loads(row["edges"])],
        "districts": json.loads(row["districts"]),
        "current_version": row["current_version"],
        "created_at": row["created_at"],
    }


def list_maps(conn) -> list:
    rows = conn.execute(
        "SELECT map_id, current_version, created_at FROM maps ORDER BY map_id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn, map_id: str, version: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT assignment, created_at FROM versions WHERE map_id = ? AND version = ?",
        (map_id, version),
    ).fetchone()
    if row is None:
        return None
    return {
        "version": version,
        "assignment": json.loads(row["assignment"]),
        "created_at": row["created_at"],
    }


def list_versions(conn, map_id: str) -> list:
    rows = conn.execute(
        "SELECT version, created_at FROM versions WHERE map_id = ? ORDER BY version",
        (map_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def commit_version(conn, *, map_id: str, base_version: int, assignment: dict) -> int:
    """在 base_version 之上写入新版本；base_version 必须仍是当前最新版本。"""
    now = _utcnow()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT current_version FROM maps WHERE map_id = ?", (map_id,)
        ).fetchone()
        current = row["current_version"]
        if current != base_version:
            raise StaleVersionError(current)
        new_version = current + 1
        conn.execute(
            "INSERT INTO versions (map_id, version, assignment, created_at) VALUES (?, ?, ?, ?)",
            (map_id, new_version, json.dumps(assignment), now),
        )
        conn.execute(
            "UPDATE maps SET current_version = ? WHERE map_id = ?",
            (new_version, map_id),
        )
        conn.execute("COMMIT")
        return new_version
    except Exception:
        _rollback_quietly(conn)
        raise
