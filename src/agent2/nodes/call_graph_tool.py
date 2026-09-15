"""调用图（确定性）：由符号表与函数体算出模块内的调用关系与循环调用组。

近似（局限见 `csymbols` 的模块注释）：调用图是**下界**——报出的边可信，未报出的
边不代表不存在（宏、函数指针、跨行调用都会漏）。同一个函数名在多个文件里有定义时，
边指向不明，这类调用不连边，计入 `ambiguous_calls`。

循环调用组（强连通分量）在这里算好并落盘：它既是架构分析提示词的**输入**
（让模型一开始就不会把互相递归的函数切到两块），也是分解校验的依据。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .. import csymbols
from ..artifacts import read_text, write_json
from ..log import get_logger
from ..state import ReviewState

logger = get_logger(__name__)


def _reachable(start: str, out_edges: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        for next_name in out_edges.get(stack.pop(), ()):
            if next_name not in seen:
                seen.add(next_name)
                stack.append(next_name)
    return seen


def cyclic_groups(node_names: list[str], edges: list[dict]) -> list[list[str]]:
    """挑出互相可达的函数组（强连通分量），即「循环/相互递归调用」的那些组。

    用「两个节点能否互相走到」判定，而不是 Tarjan：函数数量只有几十到几百，
    这个写法一眼能读懂，省下的那点复杂度不值得。自递归（自己调自己）也算一组。
    """
    out_edges: dict[str, list[str]] = {}
    for edge in edges:
        out_edges.setdefault(edge["caller"], []).append(edge["callee"])

    reach = {name: _reachable(name, out_edges) for name in node_names}
    remaining, groups = set(node_names), []
    while remaining:
        node = min(remaining)
        # 节点自己必然属于自己所在的分量：不轻易写成「互相可达」那种表述，
        # 非递归的函数谁也走不到它（含它自己），那样算出来的组会是空集，循环不收敛
        group = {node} | {
            name for name in remaining if name in reach[node] and node in reach[name]
        }
        remaining -= group
        ordered = sorted(group)
        if len(ordered) > 1 or node in out_edges.get(node, ()):
            groups.append(ordered)
    return groups


def call_graph_tool(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    root = Path(config.project_root)
    symbols = state["symbols"]

    functions = symbols["interfaces"] + symbols["internals"]
    counts = Counter(f["name"] for f in functions)
    # 同名多定义：边会指向不明，直接不连，宁可少一条边也不要连错
    duplicated = {name for name, count in counts.items() if count > 1}
    known = {f["name"] for f in functions} - duplicated
    if duplicated:
        logger.warning("模块 %s 有同名函数定义，其调用边一律不连: %s", module["id"], sorted(duplicated))

    edges, ambiguous = [], 0
    for function in functions:
        text = read_text(root / function["file"])
        for call in csymbols.scan_calls(text, function):
            if call["callee"] in duplicated:
                ambiguous += 1
                continue
            if call["callee"] in known:
                edges.append(
                    {
                        "caller": function["name"],
                        "callee": call["callee"],
                        "file": function["file"],
                        "line": call["line"],
                    }
                )

    graph = {
        "nodes": [
            {"name": f["name"], "file": f["file"], "line": f["line"], "lines": f["lines"]}
            for f in functions
            if f["name"] in known
        ],
        "edges": edges,
        "cycles": cyclic_groups(sorted(known), edges),
        "ambiguous_calls": ambiguous,
    }
    write_json(config.artifact_path(module["id"], "call_graph"), graph)
    logger.info(
        "调用图: 模块=%s 节点=%d 边=%d 循环调用组=%d 同名歧义调用=%d",
        module["id"], len(graph["nodes"]), len(edges), len(graph["cycles"]), ambiguous,
    )
    return {"call_graph": graph}
