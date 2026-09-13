"""反驳结果审查节点（确定性闸门）：核对条目齐全、降级不可核验结论、落盘。

本节点是**确定性/智能分离**里确定性一侧的最后一道关：模型给出的结论在此被统一
复核与降级，`findings_verified.json` 只由本节点写出。降级而非丢弃是贯穿始终的原则——
任何被降级的条目都保留原文并写明理由，绝不静默消失。
"""

from __future__ import annotations

from typing import Any

from ... import findings_util as fu
from ...configuration import ReviewConfig
from ...log import get_logger
from ...schemas import RawFindings
from ...state import ReviewState
from ..common import (
    _config,
    _execute_batch,
    _findings_raw_path,
    _findings_verified_path,
    _read_json,
    _stats,
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
        "verdict_source": "fallback",
        "demoted_reason": "模型漏返回该条结论",
    }


def _demote(verdict: dict[str, Any], finding: dict[str, Any], threshold: float) -> dict[str, Any]:
    """按确定性规则降级「名义上已确认、实则不可核验」的结论。

    两类降级（都在原始输出上留有痕迹，便于审计）：
    1. `contract_dependent` —— 该条依赖本模块之外不可见的契约（调用方加锁约定、
       外部长度约束等）。反驳阶段看不到该契约，无从独立核验，把这种条目计入
       confirmed 会让精确率虚高。`schemas.py` 早已承诺「展示时降级」却无人消费。
    2. 置信度低于阈值 —— 实测两条误报的置信度是 0.50 / 0.55，而真正的致命缺陷是 0.95。
       模型自己的置信度是有效信号，此前从未被任何分支读取。
    """
    if verdict.get("conclusion") != "confirmed":
        return verdict
    if verdict.get("contract_dependent"):
        return {
            **verdict,
            "conclusion": "uncertain",
            "demoted_reason": "依赖本模块外不可见契约，本阶段无法独立核验",
        }
    try:
        confidence = float(verdict.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < threshold:
        return {
            **verdict,
            "conclusion": "uncertain",
            "demoted_reason": f"置信度 {confidence:.2f} 低于阈值 {threshold:.2f}，证据不足以确认",
        }
    return verdict


def _resolve_claim_conflicts(ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一主张上出现互相冲突的结论时，统一降为 `uncertain` 并保留全部分歧原文。

    **不静默择一**：`confirmed` 与 `refuted` 同时出现，说明两个方向都有强主张，
    任选其一都会同时带来假阳性与漏报两种风险。降级 + 标记 + 保留原文，交人工裁决，
    这与工程既有的「绝不静默丢弃」一致。

    注意只在**同一主张**（锚点 + 缺陷种类）内判定。同一语句上的不同缺陷得出不同结论
    是正常的：实测 `parser.c:13` 上并存三个都成立的缺陷，结论分别为
    confirmed / refuted / refuted，把它们当矛盾处理会掩盖真实信息。
    """
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for item in ordered:
        groups.setdefault(fu.claim_key(item["finding"]), []).append(item)

    resolved = 0
    for key, items in groups.items():
        if len(items) < 2:
            continue
        conclusion, inconsistent = fu.resolve_conclusions(i["verdict"]["conclusion"] for i in items)
        if not inconsistent:
            continue
        resolved += 1
        siblings = [
            {
                "id": i["finding"].get("id"),
                "conclusion": i["verdict"].get("conclusion"),
                "severity": i["verdict"].get("severity"),
                "confidence": i["verdict"].get("confidence"),
                "rebuttal_opinion": i["verdict"].get("rebuttal_opinion", ""),
            }
            for i in items
        ]
        for item in items:
            item["verdict"] = {
                **item["verdict"],
                "conclusion": conclusion,
                "inconsistent": True,
                "demoted_reason": "同一主张存在互相冲突的结论，已交人工裁决",
                "sibling_verdicts": [s for s in siblings if s["id"] != item["finding"].get("id")],
            }
    if resolved:
        logger.warning("[审查] %d 个主张存在互相冲突的结论，已降为 uncertain 并保留分歧原文", resolved)
    return ordered


def _review_check_one(cfg: ReviewConfig, state: ReviewState, module: str) -> dict[str, Any]:
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

    demoted = 0
    for item in ordered:
        before = item["verdict"].get("conclusion")
        item["verdict"] = _demote(item["verdict"], item["finding"], cfg.confirmed_confidence_threshold)
        if item["verdict"].get("conclusion") != before:
            demoted += 1
            logger.debug(
                "[审查] %s %s 降级 %s -> uncertain: %s",
                module,
                item["finding"].get("id"),
                before,
                item["verdict"].get("demoted_reason"),
            )

    ordered = _resolve_claim_conflicts(ordered)
    ordered.sort(key=lambda i: (fu.severity_rank(i["verdict"].get("severity")), str(i["finding"].get("file")), int(i["finding"].get("line") or 0)))

    payload["results"] = ordered
    payload["observations"] = [o.model_dump() for o in raw.observations]
    if missing:
        # 整批反驳调用失败时，全部条目会落到这里：此时 confirmed=0/uncertain=N 看起来「健康」，
        # 必须让它在日志与统计里显形，否则失败被伪装成正常结果。
        logger.warning(
            "[审查] %s 缺失反驳结论 %d 条，已标记 uncertain（verdict_source=fallback）: %s",
            module,
            len(missing),
            missing,
        )

    _write_json(_findings_verified_path(cfg, module), payload)
    stats = _stats(ordered, len(raw.observations))
    logger.info(
        "[审查] %s findings=%d confirmed=%d refuted=%d uncertain=%d 降级=%d 缺结论=%d 产物=%s",
        module,
        len(ordered),
        stats["confirmed"],
        stats["refuted"],
        stats["uncertain"],
        demoted,
        len(missing),
        _findings_verified_path(cfg, module),
    )
    return {"module_results": {module: payload}}


def review_check_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    return _execute_batch(
        "review_check", state, lambda module: _review_check_one(cfg, state, module), channel="module_results"
    )
