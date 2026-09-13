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
from ...tools.executors import static_analyzers
from ..common import (
    _config,
    _execute_batch,
    _findings_raw_path,
    _findings_verified_path,
    _read_json,
    _stats,
    _write_json,
)
from ..common import _load_tool_evidence as load_tool_evidence

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


def _apply_tool_evidence(
    verdict: dict[str, Any], finding: dict[str, Any], entries: list[dict]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """用外部工具证据校正结论，返回 `(verdict, finding)`。**工具是独立真相源，不是第三个投票者。**

    三档处置（依据「静态分析工具报告 = 该位置确实存在问题」这一确定性事实）：

    - **同缺陷种类命中** —— 工具独立复现了模型的这条具体判断。这是最强的外部证据：
      `refuted` 与 `uncertain` 一律升为 `confirmed`（工具不会因为「拿不准」而报错），
      `confirmed` 保持并跳过置信度降级（工具已证实，模型自评的置信度不再重要）。
    - **仅同位置命中**（工具在同一行报了别的缺陷）—— 该语句确实有问题，故**不允许
      `refuted`**，降为 `uncertain` 并附证据交人工；但也不升为 confirmed——工具说的是
      另一件事，不能替模型的这条断言背书。
    - **无命中** —— 原样返回。**工具没报不等于没问题**：实测最重要的
      `buffer.c:7` 无界 strcpy 两个工具都抓不到，用缺席当反证会直接造成漏报。
    """
    same_kind, same_place = static_analyzers.match_for(finding, entries)
    if not same_kind and not same_place:
        return verdict, finding
    finding = {**finding, "tool_evidence": [*(finding.get("tool_evidence") or []), *same_kind, *same_place]}
    if same_kind:
        if verdict.get("conclusion") == "confirmed":
            return verdict, finding
        return (
            {
                **verdict,
                "conclusion": "confirmed",
                "confidence": max(float(verdict.get("confidence") or 0.0), 0.9),
                "verdict_source": "tool",
                "demoted_reason": "",
                "rebuttal_opinion": (
                    f"{verdict.get('rebuttal_opinion') or ''}"
                    f" ｜ 外部工具独立证实：{same_kind[0].get('tool')} {same_kind[0].get('rule')}"
                ).strip(" ｜"),
            },
            finding,
        )
    if verdict.get("conclusion") == "refuted":
        return (
            {
                **verdict,
                "conclusion": "uncertain",
                "verdict_source": "tool",
                "inconsistent": True,
                "demoted_reason": (
                    f"模型判定为误报，但 {same_place[0].get('tool')} 在同一位置报告 "
                    f"{same_place[0].get('rule')}；工具缺席不等同于证伪，此处证据冲突交人工裁决"
                ),
            },
            finding,
        )
    return verdict, finding


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
    # 工具证据放在最后：它的证明力高于模型的置信度自评与「分歧无法裁决」这两条启发式，
    # 故先让它们把结论定下来，再由外部证据做最终校正（或推翻）。
    tool_entries = [e.model_dump() for e in load_tool_evidence(cfg).entries]
    if tool_entries:
        corroborated = 0
        for item in ordered:
            before = item["verdict"].get("conclusion")
            item["verdict"], item["finding"] = _apply_tool_evidence(
                item["verdict"], item["finding"], tool_entries
            )
            if item["verdict"].get("conclusion") != before:
                corroborated += 1
                logger.debug(
                    "[审查] %s %s 工具证据校正 %s -> %s: %s",
                    module,
                    item["finding"].get("id"),
                    before,
                    item["verdict"].get("conclusion"),
                    item["verdict"].get("demoted_reason") or "工具独立证实",
                )
        if corroborated:
            logger.info(
                "[审查] %s 工具证据校正 %d 条结论（外部静态分析独立印证/裁决）", module, corroborated
            )
    ordered.sort(key=lambda i: (fu.severity_rank(i["verdict"].get("severity")), str(i["finding"].get("file")), int(i["finding"].get("line") or 0)))

    payload["results"] = ordered
    payload["observations"] = [o.model_dump() for o in raw.observations]
    # 漏检记账随产物往下走：检视阶段算出的「已定位检查项无回应」清单必须进最终产物，
    # 否则它只停在日志里，人工读报告时看不到——而这正是最需要被看到的一类问题。
    payload["checklist_gaps"] = list(raw.checklist_gaps)
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
