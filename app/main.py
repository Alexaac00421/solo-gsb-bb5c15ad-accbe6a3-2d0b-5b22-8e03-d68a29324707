"""城市普查调查片区调整 API。"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Tuple

from fastapi import Depends, FastAPI, HTTPException, Response

from . import store
from .models import AdjustmentRequest, MapCreateRequest
from .validation import (
    cross_district_edges,
    district_populations,
    validate_plan,
)

app = FastAPI(
    title="城市普查调查片区调整 API",
    version="1.0.0",
    description="基础地图一次录入；按基准版本批量转移街区，整体校验后产生不可变方案版本。",
)


# ---------------------------------------------------------------- 工具

def err(status: int, code: str, message: str, **extra):
    """统一错误响应：HTTPException(detail={error, message, ...})。"""
    raise HTTPException(status_code=status, detail={"error": code, "message": message, **extra})


def get_conn():
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Depends(get_conn)


def load_map_or_404(conn: sqlite3.Connection, map_id: str) -> dict:
    m = store.get_map(conn, map_id)
    if m is None:
        err(404, "map_not_found", f"地图 '{map_id}' 不存在", map_id=map_id)
    return m


def version_payload(map_row: dict, version: int, assignment: Dict[str, str],
                    created_at) -> dict:
    """版本详情：归属、各片区人口、跨片区相邻边。"""
    return {
        "map_id": map_row["map_id"],
        "version": version,
        "assignment": assignment,
        "district_populations": district_populations(
            map_row["blocks"], map_row["districts"], assignment
        ),
        "district_limits": map_row["districts"],
        "cross_district_edges": cross_district_edges(map_row["edges"], assignment),
        "created_at": created_at,
    }


def validate_map_payload(req: MapCreateRequest):
    """校验基础地图静态数据，返回规范化后的 (blocks, edges, districts)。"""
    # 重复街区
    seen, dups = set(), set()
    for b in req.blocks:
        (dups if b.id in seen else seen).add(b.id)
    if dups:
        err(422, "duplicate_blocks", "街区编号重复", blocks=sorted(dups))
    blocks = {b.id: b.population for b in req.blocks}

    # 重复片区
    seen, dups = set(), set()
    for d in req.districts:
        (dups if d.id in seen else seen).add(d.id)
    if dups:
        err(422, "duplicate_districts", "片区编号重复", districts=sorted(dups))
    districts = {
        d.id: {"min_population": d.min_population, "max_population": d.max_population}
        for d in req.districts
    }

    # 自环
    self_loops = sorted({a for a, b in req.edges if a == b})
    if self_loops:
        err(422, "self_loop_edges", "相邻边存在自环（街区与自身相邻）", blocks=self_loops)

    # 相邻边引用未知街区
    unknown = sorted({x for edge in req.edges for x in edge if x not in blocks})
    if unknown:
        err(422, "unknown_blocks", "相邻边引用了未知街区", blocks=unknown)

    # 重复相邻边（无向，A-B 与 B-A 视为同一条）
    seen_e, dup_e = set(), set()
    for a, b in req.edges:
        key = tuple(sorted((a, b)))
        (dup_e if key in seen_e else seen_e).add(key)
    if dup_e:
        err(422, "duplicate_edges", "相邻边重复（无向边 A-B 与 B-A 视为同一条）",
            edges=[list(e) for e in sorted(dup_e)])
    edges: List[Tuple[str, str]] = sorted(seen_e)

    # 初始归属：未知街区 / 未归属街区 / 未知片区
    unknown_blocks = sorted(set(req.assignment) - set(blocks))
    if unknown_blocks:
        err(422, "unknown_blocks", "初始归属中包含未知街区", blocks=unknown_blocks)
    missing = sorted(set(blocks) - set(req.assignment))
    if missing:
        err(422, "unassigned_blocks", "存在未归属任何片区的街区（每个街区必须恰好归属一个片区）",
            blocks=missing)
    unknown_districts = sorted(set(req.assignment.values()) - set(districts))
    if unknown_districts:
        err(422, "unknown_districts", "初始归属引用了未知片区", districts=unknown_districts)

    return blocks, edges, districts


# ---------------------------------------------------------------- 路由

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/maps", status_code=201, summary="一次性录入基础地图与初始方案")
def create_map(req: MapCreateRequest, conn: sqlite3.Connection = Conn):
    blocks, edges, districts = validate_map_payload(req)

    # 初始方案也必须通过整体校验（非空 / 连通 / 人口限额）
    issues = validate_plan(blocks=blocks, edges=edges, districts=districts,
                           assignment=req.assignment)
    if issues:
        err(422, "plan_validation_failed", "初始方案未通过校验", issues=issues)

    map_id = req.map_id or "default"
    try:
        store.create_map(conn, map_id=map_id, blocks=blocks, edges=edges,
                         districts=districts, assignment=req.assignment)
    except store.MapExistsError:
        err(409, "map_exists", f"地图 '{map_id}' 已存在，基础地图只能录入一次",
            map_id=map_id)

    map_row = load_map_or_404(conn, map_id)
    return version_payload(map_row, 1, req.assignment, map_row["created_at"])


@app.get("/maps", summary="列出全部地图")
def list_maps(conn: sqlite3.Connection = Conn):
    return {"maps": store.list_maps(conn)}


@app.get("/maps/{map_id}", summary="地图静态信息与当前版本号")
def get_map(map_id: str, conn: sqlite3.Connection = Conn):
    return load_map_or_404(conn, map_id)


@app.get("/maps/{map_id}/versions", summary="列出版本历史")
def list_versions(map_id: str, conn: sqlite3.Connection = Conn):
    load_map_or_404(conn, map_id)
    return {"map_id": map_id, "versions": store.list_versions(conn, map_id)}


@app.get("/maps/{map_id}/versions/{version}", summary="查询任一历史版本（不可变快照）")
def get_version(map_id: str, version: int, conn: sqlite3.Connection = Conn):
    map_row = load_map_or_404(conn, map_id)
    v = store.get_version(conn, map_id, version)
    if v is None:
        err(404, "version_not_found", f"地图 '{map_id}' 不存在版本 {version}",
            map_id=map_id, version=version)
    return version_payload(map_row, version, v["assignment"], v["created_at"])


@app.post("/maps/{map_id}/adjustments", status_code=201,
          summary="提交或预演一次调整（批量转移街区）")
def adjust(map_id: str, req: AdjustmentRequest, response: Response,
           conn: sqlite3.Connection = Conn):
    map_row = load_map_or_404(conn, map_id)
    blocks, edges, districts = map_row["blocks"], map_row["edges"], map_row["districts"]

    # 转移指令本身校验：未知街区 / 重复街区 / 未知目标片区
    seen, dups = set(), set()
    for mv in req.moves:
        (dups if mv.block_id in seen else seen).add(mv.block_id)
    if dups:
        err(422, "duplicate_move_blocks", "同一调整中街区被重复转移", blocks=sorted(dups))
    unknown_blocks = sorted({mv.block_id for mv in req.moves} - set(blocks))
    if unknown_blocks:
        err(422, "unknown_blocks", "转移指令包含未知街区", blocks=unknown_blocks)
    unknown_districts = sorted({mv.to_district for mv in req.moves} - set(districts))
    if unknown_districts:
        err(422, "unknown_districts", "转移指令包含未知目标片区", districts=unknown_districts)

    # 基准版本必须存在且为当前最新版本（预演与提交口径一致）
    base = store.get_version(conn, map_id, req.base_version)
    if base is None:
        err(404, "version_not_found", f"地图 '{map_id}' 不存在版本 {req.base_version}",
            map_id=map_id, version=req.base_version)
    current = map_row["current_version"]
    if current != req.base_version:
        err(409, "stale_base_version",
            f"基准版本 {req.base_version} 已过期，当前最新版本为 {current}；"
            f"请基于最新版本重新提交",
            base_version=req.base_version, current_version=current)

    # 应用整批转移，得到调整后整体归属
    new_assignment = dict(base["assignment"])
    for mv in req.moves:
        new_assignment[mv.block_id] = mv.to_district

    # 整体校验：任一片区失败则整批不生效
    issues = validate_plan(blocks=blocks, edges=edges, districts=districts,
                           assignment=new_assignment)
    if issues:
        err(422, "plan_validation_failed",
            "调整后方案未通过校验，整批转移未生效", issues=issues)

    if req.dry_run:  # 只读预演：不落库，结果与同版本提交一致
        response.status_code = 200
        plan = version_payload(map_row, current + 1, new_assignment, None)
        del plan["version"], plan["created_at"]  # 预演不产生真实版本
        return {"dry_run": True, "committed": False,
                "would_be_version": current + 1, **plan}

    try:
        new_version = store.commit_version(
            conn, map_id=map_id, base_version=req.base_version,
            assignment=new_assignment)
    except store.StaleVersionError as exc:  # 并发下被抢先提交
        err(409, "stale_base_version",
            f"基准版本 {req.base_version} 已过期，当前最新版本为 {exc.current_version}；"
            f"请基于最新版本重新提交",
            base_version=req.base_version, current_version=exc.current_version)

    stored = store.get_version(conn, map_id, new_version)
    return {"dry_run": False, "committed": True,
            **version_payload(map_row, new_version, new_assignment,
                              stored["created_at"])}
