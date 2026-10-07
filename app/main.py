"""HTTP API：基础地图录入、调整预演/提交、版本查询。"""

import os
from typing import Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import core
from .models import AdjustmentIn, MapInit
from .store import Store

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "data/app.db")


class ApiError(Exception):
    """统一业务错误：{"error": {"code", "message", "details"}}"""

    def __init__(self, status_code: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def create_app(db_path: str = DEFAULT_DB_PATH) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="城市普查片区调整 API", version="1.0.0")

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求体格式错误",
                    "details": {"errors": jsonable_encoder(exc.errors())},
                }
            },
        )

    # ---- 内部 helpers ----

    def validate_map_payload(body: MapInit) -> Tuple[dict, list, dict, dict]:
        """校验基础地图录入，返回 (populations, edges, limits, assignment)。"""
        if not body.blocks:
            raise ApiError(400, "EMPTY_BLOCKS", "街区列表不能为空")
        if not body.districts:
            raise ApiError(400, "EMPTY_DISTRICTS", "片区列表不能为空")

        populations = {}
        for b in body.blocks:
            if b.id in populations:
                raise ApiError(400, "DUPLICATE_BLOCK", f"街区 {b.id} 重复录入", {"block": b.id})
            if b.population < 0:
                raise ApiError(
                    400, "INVALID_POPULATION",
                    f"街区 {b.id} 人口为负数（{b.population}），人口须非负",
                    {"block": b.id, "population": b.population},
                )
            populations[b.id] = b.population

        limits = {}
        for d in body.districts:
            if d.id in limits:
                raise ApiError(400, "DUPLICATE_DISTRICT", f"片区 {d.id} 重复录入", {"district": d.id})
            if d.min_population < 0 or d.max_population < 0 or d.max_population < d.min_population:
                raise ApiError(
                    400, "INVALID_LIMITS",
                    f"片区 {d.id} 限额无效：须满足 0 <= min_population <= max_population，"
                    f"实际为 [{d.min_population}, {d.max_population}]",
                    {"district": d.id, "min_population": d.min_population,
                     "max_population": d.max_population},
                )
            limits[d.id] = {"min_population": d.min_population, "max_population": d.max_population}

        edges = []
        seen_edges = set()
        for a, b in body.edges:
            if a == b:
                raise ApiError(400, "SELF_LOOP", f"相邻边 ({a}, {b}) 是自环", {"edge": [a, b]})
            for block in (a, b):
                if block not in populations:
                    raise ApiError(
                        400, "UNKNOWN_BLOCK", f"相邻边 ({a}, {b}) 引用了未知街区 {block}",
                        {"edge": [a, b], "block": block},
                    )
            key = tuple(sorted((a, b)))
            if key in seen_edges:
                raise ApiError(400, "DUPLICATE_EDGE", f"相邻边 ({a}, {b}) 重复", {"edge": [a, b]})
            seen_edges.add(key)
            edges.append([a, b])

        assignment = dict(body.assignment)
        for block, district in assignment.items():
            if block not in populations:
                raise ApiError(
                    400, "UNKNOWN_BLOCK", f"初始归属引用了未知街区 {block}",
                    {"block": block},
                )
            if district not in limits:
                raise ApiError(
                    400, "UNKNOWN_DISTRICT", f"街区 {block} 被分配到未知片区 {district}",
                    {"block": block, "district": district},
                )
        unassigned = sorted(set(populations) - set(assignment))
        if unassigned:
            raise ApiError(
                400, "UNASSIGNED_BLOCKS",
                f"以下街区缺少初始片区归属: {', '.join(unassigned)}",
                {"blocks": unassigned},
            )
        return populations, edges, limits, assignment

    def require_map() -> dict:
        map_obj = store.get_map()
        if map_obj is None:
            raise ApiError(409, "MAP_NOT_INITIALIZED", "基础地图尚未录入，请先调用 POST /api/map")
        return map_obj

    def plan_view(map_obj: dict, assignment: dict) -> dict:
        return {
            "assignment": assignment,
            "districts": core.district_view(map_obj["populations"], map_obj["limits"], assignment),
            "cross_district_edges": core.cross_district_edges(map_obj["edges"], assignment),
        }

    def run_adjustment(body: AdjustmentIn, require_latest: bool) -> Tuple[dict, dict, dict, list]:
        """计算调整结果，返回 (map, base_version, new_assignment, issues)。"""
        map_obj = require_map()
        base = store.get_version(body.base_version)
        if base is None:
            raise ApiError(
                404, "VERSION_NOT_FOUND", f"版本 {body.base_version} 不存在",
                {"base_version": body.base_version},
            )
        if require_latest:
            latest = store.latest_version_id()
            if base["id"] != latest:
                raise ApiError(
                    409, "VERSION_CONFLICT",
                    f"基准版本 {base['id']} 不是最新版本 {latest}，可能存在并发提交，请基于最新版本重新调整",
                    {"base_version": base["id"], "latest_version": latest},
                )

        seen_blocks = set()
        for t in body.transfers:
            if t.block in seen_blocks:
                raise ApiError(
                    400, "DUPLICATE_TRANSFER", f"街区 {t.block} 在同一批调整中被重复转移",
                    {"block": t.block},
                )
            seen_blocks.add(t.block)
            if t.block not in map_obj["populations"]:
                raise ApiError(
                    400, "UNKNOWN_BLOCK", f"转移了未知街区 {t.block}", {"block": t.block},
                )
            if t.to_district not in map_obj["limits"]:
                raise ApiError(
                    400, "UNKNOWN_DISTRICT", f"目标片区 {t.to_district} 不存在",
                    {"block": t.block, "district": t.to_district},
                )

        assignment = dict(base["assignment"])
        for t in body.transfers:
            assignment[t.block] = t.to_district
        issues = core.validate_assignment(
            map_obj["populations"], map_obj["edges"], map_obj["limits"], assignment,
        )
        return map_obj, base, assignment, issues

    # ---- 路由 ----

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "initialized": store.get_map() is not None}

    @app.post("/api/map", status_code=201)
    def init_map(body: MapInit) -> dict:
        """一次录入基础地图与初始片区方案（初始方案须通过校验）。"""
        with store.lock():
            if store.get_map() is not None:
                raise ApiError(409, "MAP_ALREADY_INITIALIZED", "基础地图已录入，不可重复初始化")
            populations, edges, limits, assignment = validate_map_payload(body)
            issues = core.validate_assignment(populations, edges, limits, assignment)
            if issues:
                raise ApiError(
                    400, "INITIAL_PLAN_INVALID", "初始方案未通过校验", {"issues": issues},
                )
            store.save_map({"populations": populations, "edges": edges, "limits": limits})
            version_id = store.create_version(parent_id=None, transfers=[], assignment=assignment)
            return {
                "version": version_id,
                "blocks": len(populations),
                "edges": len(edges),
                "districts": list(limits),
                "resulting": plan_view(store.get_map(), assignment),
            }

    @app.get("/api/map")
    def get_map() -> dict:
        map_obj = require_map()
        return {
            **map_obj,
            "latest_version": store.latest_version_id(),
            "version_count": len(store.list_versions()),
        }

    @app.post("/api/adjustments/preview")
    def preview_adjustment(body: AdjustmentIn) -> dict:
        """只读预演：结果与同版本提交一致，不产生新版本。"""
        with store.lock():
            map_obj, base, assignment, issues = run_adjustment(body, require_latest=False)
            result = {
                "valid": not issues,
                "base_version": base["id"],
                "issues": issues,
            }
            if not issues:
                result["resulting"] = plan_view(map_obj, assignment)
            return result

    @app.post("/api/adjustments", status_code=201)
    def commit_adjustment(body: AdjustmentIn) -> dict:
        """提交调整：校验通过则生成不可变新版本，任一失败则整批不生效。"""
        with store.lock():
            map_obj, base, assignment, issues = run_adjustment(body, require_latest=True)
            if issues:
                raise ApiError(
                    422, "ADJUSTMENT_INVALID",
                    "调整后方案未通过校验，整批未生效", {"issues": issues},
                )
            transfers = [t.model_dump() for t in body.transfers]
            version_id = store.create_version(
                parent_id=base["id"], transfers=transfers, assignment=assignment,
            )
            return {
                "valid": True,
                "version": version_id,
                "base_version": base["id"],
                "resulting": plan_view(map_obj, assignment),
            }

    @app.get("/api/versions")
    def list_versions() -> dict:
        require_map()
        return {
            "versions": [
                {
                    "id": v["id"],
                    "parent_id": v["parent_id"],
                    "transfers": v["transfers"],
                    "created_at": v["created_at"],
                }
                for v in store.list_versions()
            ]
        }

    @app.get("/api/versions/{version_id}")
    def get_version(version_id: int) -> dict:
        """查询任一版本：归属、各片区人口、跨片区相邻边（历史版本不漂移）。"""
        map_obj = require_map()
        version = store.get_version(version_id)
        if version is None:
            raise ApiError(
                404, "VERSION_NOT_FOUND", f"版本 {version_id} 不存在",
                {"version": version_id},
            )
        return {
            "id": version["id"],
            "parent_id": version["parent_id"],
            "transfers": version["transfers"],
            "created_at": version["created_at"],
            **plan_view(map_obj, version["assignment"]),
        }

    return app


app = create_app()
