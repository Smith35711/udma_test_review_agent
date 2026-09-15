"""反驳（大模型）：对尚无结论的检视意见逐条证伪。

首轮进入时待反驳的是全部意见；被遗漏检查回边时只剩补做的那部分。
本节点靠「已有哪些结论」自行判断待办范围，故同一段代码同时承担首轮与补做两轮。

本节点**只产出、不判定**：模型返回什么就落什么，编号是否合法、结论是否在闭集内
一律由紧随其后的 `rebuttal_check_tool` 校验。
"""

from __future__ import annotations

from ..artifacts import read_source_files, write_json
from ..llm import stream_llm_once
from ..log import get_logger
from ..prompts.rebuttal import REBUTTAL_PROMPT
from ..schemas import CONCLUSIONS, RebuttalResult, unanswered_findings
from ..state import ReviewState

logger = get_logger(__name__)


def render_findings(findings: list[dict]) -> str:
    """渲染成编号清单；编号即模型要回填的 id。"""
    blocks = []
    for finding in findings:
        blocks.append(
            f"{finding['id']} [{finding['severity']}] {finding['file']}:{finding['line']}\n"
            f"  描述：{finding['description']}\n"
            f"  证据：{finding['evidence']}\n"
            f"  修复方案：{finding['suggestion']}"
        )
    return "\n".join(blocks)


def rebuttal_llm(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    findings = state.get("findings") or []
    rebuttals = list(state.get("rebuttals") or [])

    pending = unanswered_findings(findings, rebuttals)
    if not pending:
        return {"rebuttal_items": []}

    prompt = REBUTTAL_PROMPT.format(
        module_id=module["id"],
        findings=render_findings(pending),
        source_code=read_source_files(config.project_root, module["files"]),
        conclusions=" / ".join(CONCLUSIONS),
    )
    result = stream_llm_once(prompt, RebuttalResult, **config.llm_options())

    items = [item.model_dump() for item in result.items]
    write_json(config.artifact_path(module["id"], "raw_rebuttals"), items)
    logger.info("反驳产出: 模块=%s 待反驳=%d 条 模型返回=%d 条", module["id"], len(pending), len(items))
    return {"rebuttal_items": items}
