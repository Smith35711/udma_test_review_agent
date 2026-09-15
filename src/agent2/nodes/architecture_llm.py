"""架构分析与分解方案（大模型）：判断大模块的形态，给出逻辑子块的划分。

输入是**精简的符号表、调用关系与循环调用组，不是源码**——200K 上下文装不下大模块，
这正是本条分支存在的理由。
"""

from __future__ import annotations

from ..artifacts import write_json
from ..configuration import SMALL_MODULE_LINE_LIMIT
from ..llm_retry import call_llm
from ..log import get_logger
from ..prompts.architecture import ARCHITECTURE_FEEDBACK, ARCHITECTURE_PROMPT
from ..schemas import ArchitecturePlan
from ..state import ReviewState

logger = get_logger(__name__)


def render_symbols(symbols: dict) -> str:
    lines = []
    for item in symbols["interfaces"]:
        lines.append(f"  [接口] {item['signature']}   （{item['file']}:{item['line']}，{item['lines']} 行）")
    for item in symbols["internals"]:
        lines.append(f"  [内部] {item['signature']}   （{item['file']}:{item['line']}，{item['lines']} 行）")
    for item in symbols["globals"]:
        lines.append(f"  [全局] {item['name']}   （{item['file']}:{item['line']}）")
    if not lines:
        lines.append("  （没有解析到任何函数或全局变量）")
    return "\n".join(lines)


def render_calls(graph: dict) -> str:
    by_caller: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        by_caller.setdefault(edge["caller"], []).append(edge["callee"])
    if not by_caller:
        return "  （模块内没有解析到函数之间的调用）"
    return "\n".join(
        f"  {caller} -> {'、'.join(sorted(set(callees)))}"
        for caller, callees in sorted(by_caller.items())
    )


def _render_cycles(cycles: list[list[str]]) -> str:
    if not cycles:
        return "  （没有检测到循环调用组）"
    return "\n".join(f"  第 {index} 组：{'、'.join(group)}" for index, group in enumerate(cycles, 1))


def architecture_llm(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    symbols = state["symbols"]
    graph = state["call_graph"]

    feedback = state.get("plan_feedback") or ""
    prompt = ARCHITECTURE_PROMPT.format(
        module_id=module["id"],
        line_count=module["lines"],
        function_count=len(symbols["interfaces"]) + len(symbols["internals"]),
        symbols_digest=render_symbols(symbols),
        call_digest=render_calls(graph),
        cycles_digest=_render_cycles(graph["cycles"]),
        plan_feedback=ARCHITECTURE_FEEDBACK.format(feedback=feedback) if feedback else "",
        chunk_line_limit=SMALL_MODULE_LINE_LIMIT,
    )
    plan = call_llm(config, prompt, ArchitecturePlan)
    payload = plan.model_dump()
    write_json(config.artifact_path(module["id"], "architecture"), payload)
    logger.info(
        "架构分析: 模块=%s 形态=%s 子块=%d 个",
        module["id"], payload["patterns"] or "（无明显形态）", len(payload["chunks"]),
    )
    # 反馈用过即清：留着会让下一轮的提示词重复带上一次的原因
    return {"architecture": payload, "plan_feedback": ""}
