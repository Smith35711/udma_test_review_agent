"""反驳产物的格式校验（确定性）：只校验格式，不校验内容。

与 `review_check_tool.py` 对称，但**处置方式刻意不同**：
检视产物不合法意味着该模块的结论整体不可信，故终止该模块；
反驳的条目彼此独立，一条不合法不影响其余，故丢弃它、交给遗漏检查去补做，
补做两轮仍拿不到才转「待确认」。

丢弃必须留痕——静默丢弃会让报告凭空少掉条目，而这正是本工程要消除的失败模式。

校验口径：编号必须是本轮待反驳的编号之一且不重复、结论在闭集内、理由非空。
"""

from __future__ import annotations

from ..artifacts import write_json
from ..log import get_logger
from ..schemas import CONCLUSIONS, unanswered_findings
from ..state import ReviewState

logger = get_logger(__name__)

_CONCLUSION_SET = frozenset(CONCLUSIONS)


def _format_problem(item: dict, pending_ids: set[str], accepted_ids: set[str]) -> str:
    """返回该条目的格式问题；没有问题返回空串。"""
    identifier = item.get("id")
    if identifier not in pending_ids:
        return f"编号不在本轮待反驳范围内：{identifier!r}"
    if identifier in accepted_ids:
        return f"编号重复：{identifier!r}"
    conclusion = item.get("conclusion")
    if conclusion not in _CONCLUSION_SET:
        return f"结论不在闭集内：{conclusion!r}"
    if not str(item.get("reason", "")).strip():
        return f"理由为空：{identifier!r}"
    return ""


def rebuttal_check_tool(state: ReviewState) -> dict:
    module_id = state["module"]["id"]
    findings = state.get("findings") or []
    rebuttals = list(state.get("rebuttals") or [])
    pending_ids = {f["id"] for f in unanswered_findings(findings, rebuttals)}

    accepted: list[dict] = []
    accepted_ids: set[str] = set()
    for item in state.get("rebuttal_items") or []:
        problem = _format_problem(item, pending_ids, accepted_ids)
        if problem:
            logger.warning(
                "反驳产物格式不合法，丢弃该条并交由遗漏检查补做: 模块=%s %s", module_id, problem
            )
            continue
        accepted_ids.add(item["id"])
        accepted.append(item)

    merged = rebuttals + accepted
    write_json(state["config"].artifact_path(module_id, "checked_rebuttals"), merged)
    logger.info(
        "反驳校验通过: 模块=%s 本轮收下=%d 条 累计=%d/%d",
        module_id, len(accepted), len(merged), len(findings),
    )
    return {"rebuttals": merged}
