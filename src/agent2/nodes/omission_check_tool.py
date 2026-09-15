"""遗漏检查（确定性）：找出没有结论的意见，决定回边补做还是继续往下走。

模型整批反驳时可能漏返若干条。需求规格 §5.6 要求把这些漏掉的交回反驳节点补做；
本节点给出判定，回边由 `graph.py` 的条件边执行。

**补做有上限**，达上限仍无结论的条目一律标为「待确认」。需求要求反驳过程不得增减
条目，静默丢弃会让报告少掉本应呈现的问题。
"""

from __future__ import annotations

from ..artifacts import write_json
from ..log import get_logger
from ..schemas import unanswered_findings
from ..state import ReviewState

logger = get_logger(__name__)

MAX_OMISSION_ROUNDS = 2
"""最多交回反驳节点补做的轮数。"""

_GIVE_UP_REASON = f"经 {MAX_OMISSION_ROUNDS} 轮补做仍未取得反驳结论，留待人工确认"


def omission_check_tool(state: ReviewState) -> dict:
    module_id = state["module"]["id"]
    findings = state.get("findings") or []
    rebuttals = list(state.get("rebuttals") or [])

    missing = unanswered_findings(findings, rebuttals)
    if not missing:
        return {"rebuttal_pending": False}

    rounds = state.get("omission_rounds", 0) + 1
    if rounds <= MAX_OMISSION_ROUNDS:
        logger.warning(
            "有 %d 条未经反驳，交回反驳节点补做（第 %d/%d 轮）",
            len(missing), rounds, MAX_OMISSION_ROUNDS,
        )
        return {"omission_rounds": rounds, "rebuttal_pending": True}

    merged = rebuttals + [
        {"id": finding["id"], "conclusion": "待确认", "reason": _GIVE_UP_REASON}
        for finding in missing
    ]
    write_json(state["config"].artifact_path(module_id, "checked_rebuttals"), merged)
    logger.warning("补做达上限，%d 条转为待确认: 模块=%s", len(missing), module_id)
    return {"rebuttals": merged, "rebuttal_pending": False}
