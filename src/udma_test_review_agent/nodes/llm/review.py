"""单模块检视节点（LLM）：综合检视、按域检视与高风险专项检视。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ...configuration import ReviewConfig
from ...log import get_logger
from ...prompts.review import REVIEW_PROMPT
from ...prompts.review_check import REVIEW_CONTENT_CHECK_PROMPT
from ...prompts.review_domain import DOMAIN_REVIEW_PROMPT
from ...prompts.review_focused import FOCUSED_REVIEW_PROMPT
from ...schemas import (
    Finding,
    GlobalArchitecture,
    ModuleArchitecture,
    RawFindings,
    ReviewCheck,
    ScanResult,
)
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import (
    _config,
    _findings_raw_path,
    _global_context_for_module,
    _module_arch_path,
    _read_json,
    _tracked,
    _truncate,
    _write_json,
)
from ._base import _llm

logger = get_logger(__name__)


def _checklist_text(arch: ModuleArchitecture) -> str:
    return "\n".join(f"- [{i.category}] {i.item}" for i in arch.checklist) or "(无)"


def _interfaces_text(arch: ModuleArchitecture) -> str:
    return "\n".join(
        f"- {i.signature} @{i.file}:{i.line} 前置条件={i.precondition or '未声明'} "
        f"已校验={i.input_validated} 边界={i.is_boundary}"
        for i in arch.interfaces
    ) or "(无)"


def _contract_text(arch: ModuleArchitecture) -> str:
    c = arch.concurrency_contract
    return (
        f"线程模型={c.thread_model}；调用方负责加锁={c.caller_locked or '无'}；"
        f"内部持锁边界={c.internal_locks or '无'}"
    )


def _module_flagged(scan: ScanResult, global_arch: GlobalArchitecture, module: str) -> bool:
    inventory = next(m for m in scan.modules if m.name == module)
    files = {f.path for f in inventory.files}
    names = {Path(f).name for f in files}
    entries = [e for e in global_arch.trust_entries if Path(e.file).name in names]
    spots = [s for s in global_arch.concurrency_hotspots if Path(s.file).name in names]
    return bool(entries or spots)


def _review_pass(prompt: str, cfg: ReviewConfig) -> RawFindings:
    logger.debug("[检视] 发起检视 prompt_chars=%d", len(prompt))
    result = _llm(prompt, RawFindings, cfg)
    findings = [f for f in result.findings if f.channel == "finding"]
    observations = list(result.observations) + [f for f in result.findings if f.channel != "finding"]
    logger.debug("[检视] 单次返回 findings=%d observations=%d", len(findings), len(observations))
    return RawFindings(findings=findings, observations=observations)


def _content_review(module: str, raw: RawFindings, cfg: ReviewConfig) -> ReviewCheck:
    prompt = REVIEW_CONTENT_CHECK_PROMPT.format(
        module=module,
        findings=json.dumps(raw.model_dump(), ensure_ascii=False),
    )
    logger.debug(
        "[检视] 内容审查 module=%s findings=%d observations=%d prompt_chars=%d",
        module,
        len(raw.findings),
        len(raw.observations),
        len(prompt),
    )
    return _llm(prompt, ReviewCheck, cfg)


def _small_review(prompt: str, module: str, cfg: ReviewConfig) -> RawFindings:
    """小模块：生成 → 格式与内容审查 → 未通过则带反馈重新生成，直至通过或达上限。"""
    feedback = ""
    last: RawFindings | None = None
    for attempt in range(1, cfg.max_review_attempts + 1):
        current = prompt if not feedback else (
            f"{prompt}\n\n上一次审查未通过，问题如下，请修正后重新输出完整 JSON：\n{feedback}"
        )
        raw = _review_pass(current, cfg)
        check = _content_review(module, raw, cfg)
        if check.passed:
            logger.debug("[检视] %s 内容审查通过(第%d次)", module, attempt)
            return raw
        last = raw
        feedback = "\n".join(check.issues) or check.feedback
        logger.warning(
            "[检视] %s 内容审查未通过(第%d/%d次): %s",
            module,
            attempt,
            cfg.max_review_attempts,
            feedback[:300],
        )
    if last is None:
        raise RuntimeError(f"模块 {module} 内容审查重试次数配置无效: {cfg.max_review_attempts}")
    logger.warning("[检视] %s 内容审查重试达上限，采用最后一版", module)
    return last


def _review_module_raw(
    scan: ScanResult,
    global_arch: GlobalArchitecture,
    module: str,
    size_class: str,
    cfg: ReviewConfig,
) -> dict[str, Any]:
    arch = ModuleArchitecture.model_validate(_read_json(_module_arch_path(cfg, module)))
    inventory = next(m for m in scan.modules if m.name == module)
    module_files = {f.path for f in inventory.files}
    global_context = _global_context_for_module(global_arch, module, module_files)
    common = {
        "module_arch": (
            f"职责={arch.responsibility}\n接口契约:\n{_interfaces_text(arch)}\n"
            f"并发契约: {_contract_text(arch)}\n检查项:\n{_checklist_text(arch)}"
        ),
        "global_context": global_context,
    }
    raw_batches: list[RawFindings] = []
    if size_class == "small":
        prompt = REVIEW_PROMPT.format(
            **common,
            checklist=_checklist_text(arch),
            source=c_scanner.module_source_text(scan, module),
        )
        raw_batches.append(_small_review(prompt, module, cfg))
    else:
        headers = c_scanner.header_signatures_text(scan, module)
        for file_info in inventory.files:
            if not file_info.path.endswith(".c"):
                continue
            prompt = DOMAIN_REVIEW_PROMPT.format(
                module_arch=common["module_arch"],
                headers=headers,
                source=c_scanner.file_source_text(scan, file_info.path),
            )
            raw_batches.append(_review_pass(prompt, cfg))
    flagged = _module_flagged(scan, global_arch, module)
    if flagged:
        prompt = FOCUSED_REVIEW_PROMPT.format(
            module_arch=common["module_arch"],
            source=_truncate(c_scanner.module_source_text(scan, module), 60000),
        )
        raw_batches.append(_review_pass(prompt, cfg))
    logger.debug("[检视] %s size_class=%s 批次=%d 高风险=%s", module, size_class, len(raw_batches), flagged)

    merged: list[Finding] = []
    seen: set[tuple] = set()
    observations: list[Finding] = []
    for batch in raw_batches:
        for finding in batch.findings:
            key = (finding.file, finding.line // 10, finding.category)
            if key in seen:
                continue
            seen.add(key)
            merged.append(finding)
        observations.extend(batch.observations)
    total_raw = sum(len(batch.findings) for batch in raw_batches)
    final_findings = merged[: cfg.max_findings_per_pass]
    for idx, finding in enumerate(final_findings, 1):
        finding.id = f"f{idx}"
    logger.debug(
        "[检视] %s 合并去重 原始=%d 去重后=%d 上限=%d 已重编号",
        module,
        total_raw,
        len(final_findings),
        cfg.max_findings_per_pass,
    )
    logger.info("[检视] %s 原始findings=%d observations=%d", module, len(final_findings), len(observations))
    return RawFindings(findings=final_findings, observations=observations).model_dump()


@_tracked("review_module")
def review_module_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    global_arch = GlobalArchitecture.model_validate(state["global_arch"])
    index = state.get("review_index", 0)
    order = state["module_order"]
    module = order[index]
    size_info = (state.get("module_size") or {}).get(module) or {}
    size_class = size_info.get("size_class") or "large"

    if _findings_raw_path(cfg, module).exists():
        logger.info("[检视] 命中断点: %s", module)
        return {"module_raw": {module: _read_json(_findings_raw_path(cfg, module))}}

    started = time.monotonic()
    data = _review_module_raw(scan, global_arch, module, size_class, cfg)
    _write_json(_findings_raw_path(cfg, module), data)
    logger.info("[检视] %s 完成，耗时 %.1fs", module, time.monotonic() - started)
    return {"module_raw": {module: data}}
