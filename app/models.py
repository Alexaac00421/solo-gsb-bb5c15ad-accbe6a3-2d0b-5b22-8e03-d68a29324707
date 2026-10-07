"""请求体模型。"""

from typing import Dict, List, Tuple

from pydantic import BaseModel, Field


class BlockIn(BaseModel):
    id: str = Field(min_length=1, description="街区编号")
    population: int = Field(description="非负人口")


class DistrictIn(BaseModel):
    id: str = Field(min_length=1, description="片区编号")
    min_population: int = Field(description="人口下限（非负）")
    max_population: int = Field(description="人口上限（>= 下限）")


class MapInit(BaseModel):
    """基础地图一次录入。"""

    blocks: List[BlockIn] = Field(description="街区列表（编号唯一、人口非负）")
    edges: List[Tuple[str, str]] = Field(description="双向相邻边，无自环、无重复")
    districts: List[DistrictIn] = Field(description="片区列表（编号唯一、限额有效）")
    assignment: Dict[str, str] = Field(description="初始归属：街区编号 -> 片区编号")


class Transfer(BaseModel):
    block: str = Field(min_length=1, description="待转移街区编号")
    to_district: str = Field(min_length=1, description="目标片区编号")


class AdjustmentIn(BaseModel):
    """一次调整：基于某版本同时转移多个街区。"""

    base_version: int = Field(ge=1, description="基准版本号")
    transfers: List[Transfer] = Field(min_length=1, description="本批转移列表")
