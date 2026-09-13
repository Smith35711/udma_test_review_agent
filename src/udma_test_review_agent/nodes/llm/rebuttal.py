"""单模块反驳节点（LLM）：整模块一次请求批量证伪验证。"""

from __future__ import annotations

import json
import time
from typing import Any

from ...configuration import ReviewConfig
from ...log import get_logger
from ...prompts.rebuttal import REBUTTAL_BATCH_PROMPT
from ...schemas import Finding, RawFindings, RebuttalReport, ScanResult, Verdict
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import (
    _config,
    _execute_batch,
    _findings_verified_path,
    _load_json,
    _module_arch_path,
    _stats,
)
from ._base import _llm

logger = get_logger(__name__)


def _finding_block(scan: ScanResult, finding: Finding, cache: dict[str, list[str]]) -> str:
    # 以问题行为中心取片段（而非从函数头截断）：长函数尾部的 finding 否则会拿到
    # 一份不含该语句的「上下文」，反驳只能写证据不足或误判。
    func_name, excerpt = c_scanner.function_excerpt(scan, finding.file, finding.line, cache=cache)
    callers = c_scanner.upstream_call_sites(scan, func_name, cache=cache) if func_name else []
    logger.debug(
        "[反驳] 入批 %s 位置=%s:%d 定位函数=%s 片段字符=%d 上游调用点=%d",
        finding.id,
        finding.file,
        finding.line,
        func_name or "(未定位)",
        len(excerpt),
        len(callers),
    )
    payload = finding.model_dump()
    return (
        f"### id={finding.id}\n"
        f"finding: {json.dumps(payload, ensure_ascii=False)}\n"
        f"问题行上下文（{func_name or '未定位所属函数'}，行号以 | 左侧为准）：\n"
        f"{excerpt or '(未取到源码片段)'}\n"
        f"上游调用点代码：\n"
        + ("\n\n".join(callers) or "(调用图中无上游调用点，可能为入口函数或近似调用图未覆盖)")
    )


def _arch_brief(cfg: ReviewConfig, module: str) -> str:
    """读取本模块架构，作为反驳判定豁免时的契约依据。

    反驳提示词第六章的豁免规则（调用方负责加锁、参数已由上游校验、非边界函数）
    全部以模块契约为前提，而原实现只传 `{findings}`：模型被要求套用豁免，
    却拿不到定义豁免的那份契约，于是对契约类断言一律倾向 refuted——
    实测这正是 `M01 f6`、`M02 f4` 被推翻的直接原因。
    """
    path = _module_arch_path(cfg, module)
    arch = _load_json(path)
    if arch is None:
        # 契约不可用不该让反驳整体失败：豁免判定缺少依据是既有的退化路径，
        # 已在下游以「缺少契约依据」显式记账，比丢整个模块的反驳结论代价小得多。
        logger.warning("[反驳] %s 缺少可用的架构产物，豁免判定将缺少契约依据: %s", module, path)
        return "(本模块架构不可用)"
    contract = arch.get("concurrency_contract") or {}
    lines = [
        f"职责：{arch.get('responsibility', '')}",
        "接口契约：",
    ]
    for interface in arch.get("interfaces", []) or []:
        lines.append(
            f"  - {interface.get('signature', '')} @{interface.get('file', '')}:{interface.get('line', 0)} "
            f"前置条件={interface.get('precondition') or '未声明'} "
            f"已校验={interface.get('input_validated')} 边界={interface.get('is_boundary')}"
        )
    lines.append(
        f"并发契约：线程模型={contract.get('thread_model', '未声明')}；"
        f"调用方负责加锁={contract.get('caller_locked') or '无'}；"
        f"内部持锁边界={contract.get('internal_locks') or '无'}"
    )
    return "\n".join(lines)


def _batch_rebut(scan: ScanResult, findings: list[Finding], cfg, module_arch: str) -> dict[str, Verdict]:
    """对给定 findings 发起一次批量反驳，返回 {id: Verdict}（仅保留被请求的 id）。"""
    cache: dict[str, list[str]] = {}
    blocks = [_finding_block(scan, finding, cache) for finding in findings]
    valid_ids = {finding.id for finding in findings}
    prompt = REBUTTAL_BATCH_PROMPT.format(findings="\n\n".join(blocks), module_arch=module_arch)
    logger.debug("[反驳] 批量请求 findings=%d prompt_chars=%d", len(findings), len(prompt))
    report = _llm(prompt, RebuttalReport, cfg, role="rebuttal")
    verdicts: dict[str, Verdict] = {}
    for verdict in report.results:
        if verdict.id in valid_ids and verdict.id not in verdicts:
            verdicts[verdict.id] = verdict
    return verdicts


def _fallback_verdict(finding: Finding) -> Verdict:
    return Verdict(
        id=finding.id,
        conclusion="uncertain",
        severity=finding.severity,
        confidence=0.0,
        rebuttal_opinion="批量反驳未返回该条结论，证据不足，降级待人工确认",
        refutation_log="批量反驳未返回该条结论，重新请求后仍缺失",
        evidence_note="",
        contract_dependent=False,
    )


def _rebuttal_one(
    cfg: ReviewConfig, scan: ScanResult, state: ReviewState, module: str
) -> dict[str, Any]:
    verified_path = _findings_verified_path(cfg, module)
    cached = _load_json(verified_path)
    if cached is not None:
        logger.info("[反驳] 命中断点: %s", module)
        return {"module_results": {module: cached}}

    raw = RawFindings.model_validate(state["module_raw"][module])
    findings = raw.findings
    started = time.monotonic()
    logger.debug("[反驳] %s 原始findings=%d 产物=%s", module, len(findings), verified_path)

    module_arch = _arch_brief(cfg, module)
    verdicts: dict[str, Verdict] = {}
    retried = False
    if findings:
        verdicts = _batch_rebut(scan, findings, cfg, module_arch)
        missing = [finding for finding in findings if finding.id not in verdicts]
        if missing:
            retried = True
            logger.warning(
                "[反驳] %s 批量返回缺失 %d 条，提取后重新请求: %s",
                module,
                len(missing),
                [finding.id for finding in missing],
            )
            verdicts.update(_batch_rebut(scan, missing, cfg, module_arch))
        for finding in findings:
            if finding.id not in verdicts:
                logger.warning("[反驳] %s %s 重新请求后仍缺失，标记 uncertain", module, finding.id)
                verdicts[finding.id] = _fallback_verdict(finding)

    results = [
        {"finding": finding.model_dump(), "verdict": verdicts[finding.id].model_dump()}
        for finding in findings
    ]
    payload = {
        "module": module,
        "results": results,
        "observations": [o.model_dump() for o in raw.observations],
    }
    stats = _stats(results, len(raw.observations))
    logger.info(
        "[反驳] %s 完成 耗时%.1fs 重试=%s confirmed=%d refuted=%d uncertain=%d（待审查节点核对落盘）",
        module,
        time.monotonic() - started,
        retried,
        stats["confirmed"],
        stats["refuted"],
        stats["uncertain"],
    )
    for finding in findings:
        verdict = verdicts[finding.id]
        logger.debug("[反驳] %s %s -> %s (置信度%.2f)", module, finding.id, verdict.conclusion, verdict.confidence)
    return {"module_results": {module: payload}}


def rebuttal_module_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    return _execute_batch(
        "rebuttal_module",
        state,
        lambda module: _rebuttal_one(cfg, scan, state, module),
        channel="module_results",
    )
