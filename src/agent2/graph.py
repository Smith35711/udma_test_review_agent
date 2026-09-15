"""图组装：三张图。

- **扫描图**：架构分析阶段的整体扫描（本期只有一个节点，后续分析节点在此追加）。
- **小模块检视图**：单模块流水线，源码可以整体进上下文。
- **大模块检视图**：源码装不下，先把它"看小"——符号表 → 调用图 → 架构方案 → 分解校验
  → 子块并行检视 → 合并 → 带探测取证的反驳 → 报告。

模块的串行循环与并发**不在这里**，而在命令行入口：模块之间彼此独立，把循环做进图
需要额外的游标状态与回边，放在入口用线程池驱动一眼能读懂。CLI 按行数把模块分流到
后两张图。

小模块检视图：

    review_llm → review_check_tool ─[合格]→ rebuttal_llm → rebuttal_check_tool
                        │                                        │
                   [不合格] 该模块终止                    omission_check_tool
                                                                 │
                                              [还有遗漏] 回到 rebuttal_llm
                                                                 │
                                                          report_tool → END

大模块检视图：

    symbol_table_tool → call_graph_tool → architecture_llm → architecture_check_tool
                              ▲                                     │
                              └──────[方案需重做且未到上限]─────────┤
                                                                    │[定稿]
                                                        chunk_reviews_llm
                                                                    │
                                                   review_check_tool ┤(不合格则终止)
                                                                    │
                                       probe_rebuttal_llm → rebuttal_check_tool
                                              ▲                     │
                                              └──[仍有证据请求]── probe_tool
                                                                    │
                                                     omission_check_tool →(遗漏)
                                                                            │
                                                              report_tool → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes.architecture_check_tool import architecture_check_tool
from .nodes.architecture_llm import architecture_llm
from .nodes.call_graph_tool import call_graph_tool
from .nodes.chunk_reviews_llm import chunk_reviews_llm
from .nodes.dependencies_tool import dependencies_tool
from .nodes.omission_check_tool import omission_check_tool
from .nodes.probe_rebuttal_llm import MAX_PROBE_ROUNDS, probe_rebuttal_llm
from .nodes.probe_tool import probe_tool
from .nodes.rebuttal_check_tool import rebuttal_check_tool
from .nodes.rebuttal_llm import rebuttal_llm
from .nodes.report_tool import report_tool
from .nodes.review_check_tool import review_check_tool
from .nodes.review_llm import review_llm
from .nodes.scan_tool import scan_tool
from .nodes.symbol_table_tool import symbol_table_tool
from .state import ReviewState, ScanState


def build_scan_graph():
    """架构分析图：先扫出模块清单，再整理各模块对项目内其他模块的依赖（背景信息）。"""
    builder = StateGraph(ScanState)
    builder.add_node("scan_tool", scan_tool)
    builder.add_node("dependencies_tool", dependencies_tool)
    builder.add_edge(START, "scan_tool")
    builder.add_edge("scan_tool", "dependencies_tool")
    builder.add_edge("dependencies_tool", END)
    return builder.compile()


# ---------------- 小模块 ----------------


def _after_review_check(state: ReviewState) -> str:
    """格式不合法则该模块到此为止，其余模块不受影响。"""
    return END if state.get("failed") else "rebuttal_llm"


def _after_omission_check(state: ReviewState) -> str:
    """还有未经反驳的条目就回边补做，否则进入报告。"""
    return "rebuttal_llm" if state.get("rebuttal_pending") else "report_tool"


def build_review_graph():
    """小模块检视图。"""
    builder = StateGraph(ReviewState)
    builder.add_node("review_llm", review_llm)
    builder.add_node("review_check_tool", review_check_tool)
    builder.add_node("rebuttal_llm", rebuttal_llm)
    builder.add_node("rebuttal_check_tool", rebuttal_check_tool)
    builder.add_node("omission_check_tool", omission_check_tool)
    builder.add_node("report_tool", report_tool)

    builder.add_edge(START, "review_llm")
    builder.add_edge("review_llm", "review_check_tool")
    builder.add_conditional_edges(
        "review_check_tool", _after_review_check, {"rebuttal_llm": "rebuttal_llm", END: END}
    )
    builder.add_edge("rebuttal_llm", "rebuttal_check_tool")
    builder.add_edge("rebuttal_check_tool", "omission_check_tool")
    builder.add_conditional_edges(
        "omission_check_tool",
        _after_omission_check,
        {"rebuttal_llm": "rebuttal_llm", "report_tool": "report_tool"},
    )
    builder.add_edge("report_tool", END)
    return builder.compile()


# ---------------- 大模块 ----------------


def _after_plan_check(state: ReviewState) -> str:
    """方案还需要重做就回边，否则进入子块检视。"""
    return "architecture_llm" if state.get("plan_pending") else "chunk_reviews_llm"


def _after_probe_check(state: ReviewState) -> str:
    """还有证据请求、且没到轮数上限，就去取证；否则交给下一步。

    没有请求时**不得**再跑一轮：那会在没有新证据的情况下重判已经判定的条目。
    """
    if state.get("probe_queries") and state.get("probe_rounds", 0) < MAX_PROBE_ROUNDS:
        return "probe_tool"
    return "omission_check_tool"


def _after_chunk_review_check(state: ReviewState) -> str:
    return END if state.get("failed") else "probe_rebuttal_llm"


def _after_probe_omission(state: ReviewState) -> str:
    """大模块的遗漏补做回到探测反驳节点（它本身就是带取证的反驳）。"""
    return "probe_rebuttal_llm" if state.get("rebuttal_pending") else "report_tool"


def build_large_review_graph():
    """大模块检视图。"""
    builder = StateGraph(ReviewState)
    builder.add_node("symbol_table_tool", symbol_table_tool)
    builder.add_node("call_graph_tool", call_graph_tool)
    builder.add_node("architecture_llm", architecture_llm)
    builder.add_node("architecture_check_tool", architecture_check_tool)
    builder.add_node("chunk_reviews_llm", chunk_reviews_llm)
    builder.add_node("review_check_tool", review_check_tool)
    builder.add_node("probe_rebuttal_llm", probe_rebuttal_llm)
    builder.add_node("rebuttal_check_tool", rebuttal_check_tool)
    builder.add_node("probe_tool", probe_tool)
    builder.add_node("omission_check_tool", omission_check_tool)
    builder.add_node("report_tool", report_tool)

    builder.add_edge(START, "symbol_table_tool")
    builder.add_edge("symbol_table_tool", "call_graph_tool")
    builder.add_edge("call_graph_tool", "architecture_llm")
    builder.add_edge("architecture_llm", "architecture_check_tool")
    builder.add_conditional_edges(
        "architecture_check_tool",
        _after_plan_check,
        {"architecture_llm": "architecture_llm", "chunk_reviews_llm": "chunk_reviews_llm"},
    )
    builder.add_edge("chunk_reviews_llm", "review_check_tool")
    builder.add_conditional_edges(
        "review_check_tool",
        _after_chunk_review_check,
        {"probe_rebuttal_llm": "probe_rebuttal_llm", END: END},
    )
    builder.add_edge("probe_rebuttal_llm", "rebuttal_check_tool")
    builder.add_conditional_edges(
        "rebuttal_check_tool",
        _after_probe_check,
        {"probe_tool": "probe_tool", "omission_check_tool": "omission_check_tool"},
    )
    builder.add_edge("probe_tool", "probe_rebuttal_llm")
    builder.add_conditional_edges(
        "omission_check_tool",
        _after_probe_omission,
        {"probe_rebuttal_llm": "probe_rebuttal_llm", "report_tool": "report_tool"},
    )
    builder.add_edge("report_tool", END)
    return builder.compile()
