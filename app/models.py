"""请求数据模型（Pydantic）。"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator


class BlockIn(BaseModel):
    """街区：编号 + 非负人口。"""

    id: str = Field(min_length=1, max_length=64)
    population: int = Field(ge=0, description="非负人口")


class DistrictIn(BaseModel):
    """片区：编号 + 人口上下限。"""

    id: str = Field(min_length=1, max_length=64)
    min_population: int = Field(ge=0)
    max_population: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_limits(self) -> "DistrictIn":
        if self.max_population < self.min_population:
            raise ValueError(
                f"片区 {self.id} 限额无效: max_population({self.max_population}) "
                f"< min_population({self.min_population})"
            )
        return self


class MapCreateRequest(BaseModel):
    """基础地图一次性录入。"""

    map_id: Optional[str] = Field(
        default=None, min_length=1, max_length=64,
        description="地图标识，缺省为 default",
    )
    blocks: List[BlockIn] = Field(min_length=1)
    edges: List[Tuple[str, str]] = Field(
        default_factory=list, description="双向相邻边，[街区A, 街区B]"
    )
    districts: List[DistrictIn] = Field(min_length=1)
    assignment: Dict[str, str] = Field(
        description="初始片区归属: {街区编号: 片区编号}，每个街区恰好归属一个片区"
    )


class MoveIn(BaseModel):
    """单次转移：把一个街区划入目标片区。"""

    block_id: str = Field(min_length=1)
    to_district: str = Field(min_length=1)


class AdjustmentRequest(BaseModel):
    """一次调整：基于某个版本同时转移多个街区。"""

    base_version: int = Field(ge=1, description="基准版本号，必须等于当前最新版本")
    moves: List[MoveIn] = Field(min_length=1)
    dry_run: bool = Field(default=False, description="true 时只读预演，不产生新版本")
