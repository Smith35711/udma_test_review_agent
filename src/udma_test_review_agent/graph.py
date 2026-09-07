"""图组装：评审流程主编排图。

  scan -> global_arch -> module_archs -> critical_paths -> merge
       -> (review_module -> rebuttal_module -> advance)*N -> report -> END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .nodes import (
    advance_node,
    critical_paths_node,
    global_arch_node,
    merge_node,
    module_archs_node,
    report_node,
    rebuttal_module_node,
    review_module_node,
    scan_node,
)
from .state import ReviewState


def _should_continue(state: ReviewState) -> str:
    index = state.get("review_index", 0)
    return "review_module" if index < len(state["module_order"]) else "report"


builder = StateGraph(ReviewState)
builder.add_node("scan", scan_node)
builder.add_node("global_arch", global_arch_node)
builder.add_node("module_archs", module_archs_node)
builder.add_node("critical_paths", critical_paths_node)
builder.add_node("merge", merge_node)
builder.add_node("review_module", review_module_node)
builder.add_node("rebuttal_module", rebuttal_module_node)
builder.add_node("advance", advance_node)
builder.add_node("report", report_node)

builder.add_edge(START, "scan")
builder.add_edge("scan", "global_arch")
builder.add_edge("global_arch", "module_archs")
builder.add_edge("module_archs", "critical_paths")
builder.add_edge("critical_paths", "merge")
builder.add_edge("merge", "review_module")
builder.add_edge("review_module", "rebuttal_module")
builder.add_edge("rebuttal_module", "advance")
builder.add_conditional_edges("advance", _should_continue, {"review_module": "review_module", "report": "report"})
builder.add_edge("report", END)

graph: CompiledStateGraph = builder.compile()
