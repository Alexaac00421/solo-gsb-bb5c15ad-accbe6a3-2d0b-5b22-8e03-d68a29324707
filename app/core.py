"""片区方案校验与视图计算（纯函数，不依赖存储层）。

约定：
- populations: {街区编号: 人口}
- edges: [[街区A, 街区B], ...] 双向相邻边
- limits: {片区编号: {"min_population": int, "max_population": int}}
- assignment: {街区编号: 片区编号}
"""

from typing import Dict, List


def build_adjacency(populations: Dict[str, int], edges: List[List[str]]) -> Dict[str, set]:
    adj = {b: set() for b in populations}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    return adj


def validate_assignment(
    populations: Dict[str, int],
    edges: List[List[str]],
    limits: Dict[str, dict],
    assignment: Dict[str, str],
) -> List[dict]:
    """校验整体归属方案，返回问题列表（空列表表示通过）。

    每个片区检查三项：非空、人口在 [min, max] 内、片区间相邻图连通。
    问题项包含受影响片区及断开街区或人口差额。
    """
    adj = build_adjacency(populations, edges)
    members: Dict[str, List[str]] = {d: [] for d in limits}
    for block, district in assignment.items():
        members[district].append(block)

    issues: List[dict] = []
    for district, lim in limits.items():
        blocks = sorted(members[district])
        if not blocks:
            issues.append({
                "district": district,
                "type": "empty",
                "message": f"片区 {district} 调整后为空",
            })
            continue

        pop = sum(populations[b] for b in blocks)
        lo, hi = lim["min_population"], lim["max_population"]
        if pop < lo:
            issues.append({
                "district": district,
                "type": "population_below_min",
                "message": f"片区 {district} 人口 {pop} 低于下限 {lo}，差额 {lo - pop}",
                "population": pop,
                "min_population": lo,
                "deficit": lo - pop,
            })
        elif pop > hi:
            issues.append({
                "district": district,
                "type": "population_above_max",
                "message": f"片区 {district} 人口 {pop} 超过上限 {hi}，差额 {pop - hi}",
                "population": pop,
                "max_population": hi,
                "excess": pop - hi,
            })

        # 连通性：从排序后的首个街区出发做 DFS，未访问到的即为断开街区
        member_set = set(blocks)
        seen = set()
        stack = [blocks[0]]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(n for n in adj[node] if n in member_set and n not in seen)
        disconnected = sorted(member_set - seen)
        if disconnected:
            issues.append({
                "district": district,
                "type": "disconnected",
                "message": f"片区 {district} 内街区不连通，断开街区: {', '.join(disconnected)}",
                "disconnected_blocks": disconnected,
            })
    return issues


def district_view(
    populations: Dict[str, int],
    limits: Dict[str, dict],
    assignment: Dict[str, str],
) -> Dict[str, dict]:
    """各片区视图：成员街区、总人口及上下限。"""
    members: Dict[str, List[str]] = {d: [] for d in limits}
    for block, district in assignment.items():
        members[district].append(block)
    view = {}
    for district, lim in limits.items():
        blocks = sorted(members[district])
        view[district] = {
            "blocks": blocks,
            "population": sum(populations[b] for b in blocks),
            "min_population": lim["min_population"],
            "max_population": lim["max_population"],
        }
    return view


def cross_district_edges(edges: List[List[str]], assignment: Dict[str, str]) -> List[dict]:
    """跨片区相邻边：两端街区归属不同片区的边。"""
    result = []
    for a, b in edges:
        da, db = assignment[a], assignment[b]
        if da != db:
            result.append({"blocks": [a, b], "districts": [da, db]})
    return result
