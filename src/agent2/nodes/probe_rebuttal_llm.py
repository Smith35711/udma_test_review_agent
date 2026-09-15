"""探测分析反驳（大模型）：上下文不完整时，先取证再下结论。

大模块的 findings 来自各个子块，每条只看得见模块的一角；而结论成立与否常在子块之外。
本节点因此分两轮：第一轮能给结论的给结论、给不了的提证据请求；取证之后第二轮再判。
最后一轮**必须每条都有结论**，拿不到证据的判「待确认」并写明缺什么——不硬判、不静默。

同一段代码同时承担首轮与末轮，靠"第几轮"区分，避免两套写法各自漂移。
"""

from __future__ import annotations

from ..artifacts import write_json
from ..llm_retry import PROBE_CHUNK_TIMEOUT, call_llm
from ..log import get_logger
from ..prompts.probe import PROBE_PROMPT
from ..schemas import CONCLUSIONS, ProbeRebuttalResult, unanswered_findings
from ..state import ReviewState
from .architecture_llm import render_calls, render_symbols
from .rebuttal_llm import render_findings

logger = get_logger(__name__)

MAX_PROBE_ROUNDS = 2
"""取证轮数上限。用户已确认取 2。"""


def _render_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return "（这是第一轮，还没有取过证据）"
    blocks = []
    for item in evidence:
        blocks.append(f"—— 第 {item['round']} 轮取证结果 ——")
        for result in item["results"]:
            marker = "（已截断）" if result["truncated"] else ""
            blocks.append(f"[{result['kind']} {result['target']}]{marker}\n{result['result']}")
    return "\n".join(blocks)


def probe_rebuttal_llm(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    symbols = state["symbols"]
    graph = state["call_graph"]

    findings = state.get("findings") or []
    rebuttals = list(state.get("rebuttals") or [])
    pending = unanswered_findings(findings, rebuttals)
    if not pending:
        return {"rebuttal_items": [], "probe_queries": []}

    rounds = state.get("probe_rounds", 0)
    is_final = rounds + 1 >= MAX_PROBE_ROUNDS
    overview = f"{render_symbols(symbols)}\n\n调用关系：\n{render_calls(graph)}"

    prompt = PROBE_PROMPT.format(
        module_id=module["id"],
        findings=render_findings(pending),
        source_code=overview,
        conclusions=" / ".join(CONCLUSIONS),
        evidence=_render_evidence(state.get("probe_evidence") or []),
        round_label=(
            "最后一轮：不再取证，每一条都必须给出结论，拿不到证据的判「待确认」并写明缺什么"
            if is_final
            else f"第 {rounds + 1} 轮（共 {MAX_PROBE_ROUNDS} 轮）：可以提出证据请求"
        ),
    )
    result = call_llm(config, prompt, ProbeRebuttalResult, chunk_timeout=PROBE_CHUNK_TIMEOUT)

    items = [item.model_dump() for item in result.items]
    queries = [] if is_final else [query.model_dump() for query in result.queries]
    write_json(config.artifact_path(module["id"], "raw_rebuttals"), items)
    logger.info(
        "探测反驳: 模块=%s 第 %d/%d 轮 待反驳=%d 结论=%d 证据请求=%d",
        module["id"], rounds + 1, MAX_PROBE_ROUNDS, len(pending), len(items), len(queries),
    )
    return {"rebuttal_items": items, "probe_queries": queries, "probe_rounds": rounds + 1}
