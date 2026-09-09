"""单模块反驳节点（LLM）：整模块一次请求批量证伪验证。"""

from __future__ import annotations

import json
import time
from typing import Any

from ...log import get_logger
from ...prompts.rebuttal import REBUTTAL_BATCH_PROMPT
from ...schemas import Finding, RawFindings, RebuttalReport, ScanResult, Verdict
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import (
    _config,
    _findings_verified_path,
    _read_json,
    _stats,
    _tracked,
    _truncate,
)
from ._base import _llm

logger = get_logger(__name__)


def _finding_block(scan: ScanResult, finding: Finding) -> str:
    func_name, function_code = c_scanner.containing_function_code(scan, finding.file, finding.line)
    callers = c_scanner.upstream_callers(scan, func_name) if func_name else []
    logger.debug(
        "[反驳] 入批 %s 位置=%s:%d 定位函数=%s 函数代码字符=%d 上游调用点=%d",
        finding.id,
        finding.file,
        finding.line,
        func_name or "(未定位)",
        len(function_code),
        len(callers),
    )
    return (
        f"### id={finding.id}\n"
        f"finding: {json.dumps(finding.model_dump(), ensure_ascii=False)}\n"
        f"所在函数（{func_name or '未定位'}）：\n{_truncate(function_code, 6000) or '(未定位到所在函数)'}\n"
        f"上游调用点：\n" + ("\n".join(callers) or "(调用图中无上游调用点，可能为入口函数或近似调用图未覆盖)")
    )


def _batch_rebut(scan: ScanResult, findings: list[Finding], cfg) -> dict[str, Verdict]:
    """对给定 findings 发起一次批量反驳，返回 {id: Verdict}（仅保留被请求的 id）。"""
    blocks = [_finding_block(scan, finding) for finding in findings]
    valid_ids = {finding.id for finding in findings}
    prompt = REBUTTAL_BATCH_PROMPT.format(findings="\n\n".join(blocks))
    logger.debug("[反驳] 批量请求 findings=%d prompt_chars=%d", len(findings), len(prompt))
    report = _llm(prompt, RebuttalReport, cfg)
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


@_tracked("rebuttal_module")
def rebuttal_module_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    index = state.get("review_index", 0)
    module = state["module_order"][index]

    verified_path = _findings_verified_path(cfg, module)
    if verified_path.exists():
        logger.info("[反驳] 命中断点: %s", module)
        return {"module_results": {module: _read_json(verified_path)}}

    raw = RawFindings.model_validate(state["module_raw"][module])
    findings = raw.findings
    started = time.monotonic()
    logger.debug("[反驳] %s 原始findings=%d 产物=%s", module, len(findings), verified_path)

    verdicts: dict[str, Verdict] = {}
    retried = False
    if findings:
        verdicts = _batch_rebut(scan, findings, cfg)
        missing = [finding for finding in findings if finding.id not in verdicts]
        if missing:
            retried = True
            logger.warning(
                "[反驳] %s 批量返回缺失 %d 条，提取后重新请求: %s",
                module,
                len(missing),
                [finding.id for finding in missing],
            )
            verdicts.update(_batch_rebut(scan, missing, cfg))
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
