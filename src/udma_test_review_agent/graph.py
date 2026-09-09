"""图组装：评审流程主编排图。

  scan -> global_arch -> module_archs -> critical_paths -> merge
       -> (size_analysis -> review_module -> rebuttal_module -> review_check -> module_report -> advance)*N
       -> report -> END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .log import get_logger
from .nodes.llm.critical_paths import critical_paths_node
from .nodes.llm.global_arch import global_arch_node
from .nodes.llm.module_archs import module_archs_node
from .nodes.llm.module_report import module_report_node
from .nodes.llm.rebuttal import rebuttal_module_node
from .nodes.llm.review import review_module_node
from .nodes.tool.advance import advance_node
from .nodes.tool.merge import merge_node
from .nodes.tool.report import report_node
from .nodes.tool.review_check import review_check_node
from .nodes.tool.scan import scan_node
from .nodes.tool.size_analysis import size_analysis_node
from .state import ReviewState

logger = get_logger(__name__)


def _should_continue(state: ReviewState) -> str:
    index = state.get("review_index", 0)
    return "size_analysis" if index < len(state["module_order"]) else "report"


builder = StateGraph(ReviewState)
builder.add_node("scan", scan_node)
builder.add_node("global_arch", global_arch_node)
builder.add_node("module_archs", module_archs_node)
builder.add_node("critical_paths", critical_paths_node)
builder.add_node("merge", merge_node)
builder.add_node("size_analysis", size_analysis_node)
builder.add_node("review_module", review_module_node)
builder.add_node("rebuttal_module", rebuttal_module_node)
builder.add_node("review_check", review_check_node)
builder.add_node("module_report", module_report_node)
builder.add_node("advance", advance_node)
builder.add_node("report", report_node)

builder.add_edge(START, "scan")
builder.add_edge("scan", "global_arch")
builder.add_edge("global_arch", "module_archs")
builder.add_edge("module_archs", "critical_paths")
builder.add_edge("critical_paths", "merge")
builder.add_edge("merge", "size_analysis")
builder.add_edge("size_analysis", "review_module")
builder.add_edge("review_module", "rebuttal_module")
builder.add_edge("rebuttal_module", "review_check")
builder.add_edge("review_check", "module_report")
builder.add_edge("module_report", "advance")
builder.add_conditional_edges("advance", _should_continue, {"size_analysis": "size_analysis", "report": "report"})
builder.add_edge("report", END)

graph: CompiledStateGraph = builder.compile()
logger.debug("[图] 已编译 节点=%s 边数=%d", sorted(builder.nodes), len(getattr(builder, "edges", [])))
