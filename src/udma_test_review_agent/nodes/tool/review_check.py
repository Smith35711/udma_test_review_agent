"""反驳结果审查节点（确定性）：核对每条 finding 均被反驳并输出，通过后落盘。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...schemas import RawFindings
from ...state import ReviewState
from ..common import (
    _config,
    _findings_raw_path,
    _findings_verified_path,
    _read_json,
    _stats,
    _tracked,
    _write_json,
)

logger = get_logger(__name__)

VALID_CONCLUSIONS = {"confirmed", "refuted", "uncertain"}


def _fallback_verdict(finding: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": finding.get("id", ""),
        "conclusion": "uncertain",
        "severity": finding.get("severity", "一般"),
        "confidence": 0.0,
        "rebuttal_opinion": "审查节点未发现该条反驳结论，证据不足，降级待人工确认",
        "refutation_log": reason,
        "evidence_note": "",
        "contract_dependent": False,
    }


@_tracked("review_check")
def review_check_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    module = state["module_order"][state.get("review_index", 0)]
    payload = dict(state["module_results"][module])

    raw = RawFindings.model_validate(_read_json(_findings_raw_path(cfg, module)))
    existing = {
        item["finding"]["id"]: item
        for item in payload.get("results", [])
        if item.get("finding", {}).get("id")
    }

    ordered: list[dict[str, Any]] = []
    missing: list[str] = []
    for finding in raw.findings:
        item = existing.get(finding.id)
        conclusion = (item or {}).get("verdict", {}).get("conclusion")
        if item is None or conclusion not in VALID_CONCLUSIONS:
            missing.append(finding.id)
            item = {
                "finding": finding.model_dump(),
                "verdict": _fallback_verdict(finding.model_dump(), "审查节点未发现该条反驳结论，标记 uncertain"),
            }
        ordered.append(item)

    payload["results"] = ordered
    payload["observations"] = [o.model_dump() for o in raw.observations]
    if missing:
        logger.warning("[审查] %s 缺失反驳结论 %d 条，已标记 uncertain: %s", module, len(missing), missing)

    _write_json(_findings_verified_path(cfg, module), payload)
    stats = _stats(ordered, len(raw.observations))
    logger.info(
        "[审查] %s findings=%d 全部已反驳并输出 confirmed=%d refuted=%d uncertain=%d 产物=%s",
        module,
        len(ordered),
        stats["confirmed"],
        stats["refuted"],
        stats["uncertain"],
        _findings_verified_path(cfg, module),
    )
    return {"module_results": {module: payload}}
