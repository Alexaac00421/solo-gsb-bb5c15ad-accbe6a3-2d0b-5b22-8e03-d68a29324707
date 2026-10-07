"""方案校验与派生数据计算（纯函数，提交与预演共用，保证结果一致）。"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

# blocks:    {街区编号: 人口}
# edges:     [[街区A, 街区B], ...]（已规范化、去重、无自环）
# districts: {片区编号: {"min_population": int, "max_population": int}}
# assignment:{街区编号: 片区编号}


def build_adjacency(
    blocks: Dict[str, int], edges: List[Tuple[str, str]]
) -> Dict[str, Set[str]]:
    adjacency: Dict[str, Set[str]] = {b: set() for b in blocks}
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    return adjacency


def _components(nodes: Set[str], adjacency: Dict[str, Set[str]]) -> List[List[str]]:
    """nodes 集合在相邻图（仅考虑集合内部的边）下的连通分量。"""
    unseen = set(nodes)
    components: List[List[str]] = []
    for start in sorted(nodes):
        if start not in unseen:
            continue
        unseen.discard(start)
        stack = [start]
        comp = []
        while stack:
            node = stack.pop()
            comp.append(node)
            for nxt in adjacency.get(node, ()):
                if nxt in unseen:  # unseen 只含本片区街区，天然过滤跨片区边
                    unseen.discard(nxt)
                    stack.append(nxt)
        components.append(sorted(comp))
    return components


def validate_plan(
    *,
    blocks: Dict[str, int],
    edges: List[Tuple[str, str]],
    districts: Dict[str, Dict[str, int]],
    assignment: Dict[str, str],
) -> List[dict]:
    """校验整体归属方案。

    对每个片区检查：非空、片区内相邻图连通、人口在 [min, max] 限额内。
    返回问题列表；空列表表示通过。任一问题都会导致整批调整不生效。
    """
    adjacency = build_adjacency(blocks, edges)
    members: Dict[str, List[str]] = {d: [] for d in districts}
    for block, district in assignment.items():
        members[district].append(block)

    issues: List[dict] = []
    for district in sorted(districts):
        district_blocks = members[district]

        if not district_blocks:
            issues.append({
                "district": district,
                "type": "empty_district",
                "message": f"片区 {district} 不包含任何街区（片区必须非空）",
            })
            continue

        population = sum(blocks[b] for b in district_blocks)
        lo = districts[district]["min_population"]
        hi = districts[district]["max_population"]
        if population < lo or population > hi:
            difference = population - hi if population > hi else population - lo
            issues.append({
                "district": district,
                "type": "population_out_of_range",
                "population": population,
                "min_population": lo,
                "max_population": hi,
                "difference": difference,
                "message": (
                    f"片区 {district} 人口 {population} 超出限额 "
                    f"[{lo}, {hi}]，差额 {difference:+d}"
                ),
            })

        components = _components(set(district_blocks), adjacency)
        if len(components) > 1:
            # 主分量取规模最大者（并列时取最小编号者），其余为断开街区
            components.sort(key=lambda c: (-len(c), c[0]))
            main, *rest = components
            disconnected = sorted(b for comp in rest for b in comp)
            issues.append({
                "district": district,
                "type": "disconnected",
                "components": components,
                "main_component": main,
                "disconnected_blocks": disconnected,
                "message": (
                    f"片区 {district} 内部不连通，共 {len(components)} 个连通分量，"
                    f"断开街区: {disconnected}"
                ),
            })
    return issues


def district_populations(
    blocks: Dict[str, int],
    districts: Dict[str, Dict[str, int]],
    assignment: Dict[str, str],
) -> Dict[str, int]:
    populations = {d: 0 for d in districts}
    for block, district in assignment.items():
        populations[district] += blocks[block]
    return populations


def cross_district_edges(
    edges: List[Tuple[str, str]], assignment: Dict[str, str]
) -> List[dict]:
    """跨片区相邻边：两端街区归属不同片区的边。"""
    result = []
    for a, b in edges:
        da, db = assignment[a], assignment[b]
        if da != db:
            result.append({
                "block_a": a, "block_b": b,
                "district_a": da, "district_b": db,
            })
    return result
