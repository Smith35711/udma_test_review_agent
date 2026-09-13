"""单模块检视节点（LLM）：综合检视、按域检视与高风险专项检视。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ... import findings_util as fu
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
from ...tools.executors import c_scanner, static_analyzers
from ..common import (
    _config,
    _execute_batch,
    _findings_raw_path,
    _global_context_for_module,
    _module_arch_path,
    _module_size_path,
    _read_json,
    _truncate,
    _write_json,
)
from ..common import _load_tool_evidence as load_tool_evidence
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


def _review_pass(prompt: str, cfg: ReviewConfig, pass_name: str) -> RawFindings:
    logger.debug("[检视] 发起检视 pass=%s prompt_chars=%d", pass_name, len(prompt))
    result = _llm(prompt, RawFindings, cfg)
    for finding in result.findings:
        finding.source_pass = pass_name
    for observation in result.observations:
        observation.source_pass = pass_name
    findings = [f for f in result.findings if f.channel == "finding"]
    observations = list(result.observations) + [f for f in result.findings if f.channel != "finding"]
    logger.debug(
        "[检视] pass=%s 返回 findings=%d observations=%d", pass_name, len(findings), len(observations)
    )
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
        raw = _review_pass(current, cfg, "small")
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


def _enrich(scan: ScanResult, finding: Finding, cache: dict[str, list[str]]) -> tuple[Finding, dict[str, str]]:
    """为单条 finding 计算锚点与位置校验状态，返回 (新对象, 校验结果)。"""
    location = c_scanner.classify_location(scan, finding.file, finding.line, cache=cache)
    data = finding.model_dump()
    if location["path"]:
        data["file"] = location["path"]
    data["evidence_status"] = location["status"]
    data["anchor_fingerprint"] = fu.line_fingerprint(location["line_text"])
    data["anchor"] = fu.anchor_id(data["file"], finding.line, data["anchor_fingerprint"])
    return Finding.model_validate(data), location


def _enrich_and_split(
    scan: ScanResult, raw_batches: list[RawFindings]
) -> tuple[list[Finding], list[Finding], list[str]]:
    """给全部条目补锚点与位置校验，并把位置无效的 finding 降级为 observation。

    位置校验是**三值**的（见 `c_scanner.classify_location`）：`unattributable` 只表示
    文本级扫描器定位不到所属函数（头文件声明、宏、多行签名、文件作用域），那是扫描器的
    能力边界而非模型错误，**必须保留**——实测 `buffer.h` 上的契约缺陷正落在此类，
    按「无效」处理会把它们整类丢掉。只有 `invalid`（文件不在扫描范围、行号越界、
    空行或纯注释）才降级，且降级时把理由写进描述，绝不静默丢弃。
    """
    cache: dict[str, list[str]] = {}
    merged: list[Finding] = []
    observations: list[Finding] = []
    invalid: list[str] = []

    for batch in raw_batches:
        for finding in batch.findings:
            enriched, location = _enrich(scan, finding, cache)
            if location["status"] == "invalid":
                reason = location["reason"]
                invalid.append(f"{finding.file}:{finding.line}")
                data = enriched.model_dump()
                data["channel"] = "observation"
                data["description"] = f"[位置校验未通过：{reason}] {data.get('description', '')}"
                observations.append(Finding.model_validate(data))
                logger.debug("[检视] 位置无效降级 %s:%s %s", finding.file, finding.line, reason)
                continue
            merged.append(enriched)
        for observation in batch.observations:
            enriched, _ = _enrich(scan, observation, cache)
            observations.append(enriched)
    return merged, observations, invalid


def _tool_candidates(
    scan: ScanResult,
    module_files: set[str],
    covered: set[tuple[str, int]],
    entries: list[dict],
    cache: dict[str, list[str]],
) -> list[Finding]:
    """补漏：工具独立报出、而模型在本模块**一个字都没提**的位置，转为候选 finding。

    只在「该锚点上模型没有任何主张」时注入——模型已提到该语句时，工具的作用是印证或
    裁决（在 `review_check`），再注入一条只会让同一条语句重复出卡。
    """
    candidates: list[Finding] = []
    for entry, mapping in static_analyzers.injectable(entries):
        file = entry.get("file", "")
        line = int(entry.get("line") or 0)
        if file not in module_files or line <= 0:
            continue
        if any(anchor_file == file and abs(anchor_line - line) <= static_analyzers.MATCH_TOLERANCE
               for anchor_file, anchor_line in covered):
            continue
        finding = Finding(
            id="",
            file=file,
            line=line,
            category=mapping.category,
            defect_kind=mapping.defect_kind,
            severity=mapping.severity,
            title=f"{entry.get('tool')} 独立检出：{entry.get('rule')}",
            description=f"静态分析工具 {entry.get('tool')} 在该位置报告：{entry.get('message')}",
            root_cause="由外部静态分析工具独立判定，未经模型归纳",
            suggestion="按工具报告的规则修复该处，并补充回归用例",
            impact=f"工具等级：{entry.get('severity')}；规则：{entry.get('rule')}",
            evidence=f"{entry.get('tool')} 报告 {entry.get('rule')}：{entry.get('message')}",
            call_chain=[],
            channel="finding",
            source="tool",
            source_pass="tool",
            tool_evidence=[entry],
        )
        enriched, location = _enrich(scan, finding, cache)
        if location["status"] == "invalid":
            logger.debug("[检视] 工具候选位置校验未通过，丢弃 %s:%s %s", file, line, location["reason"])
            continue
        candidates.append(enriched)
    return candidates


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
            raw_batches.append(_review_pass(prompt, cfg, "large"))
    flagged = _module_flagged(scan, global_arch, module)
    if flagged:
        prompt = FOCUSED_REVIEW_PROMPT.format(
            module_arch=common["module_arch"],
            source=_truncate(c_scanner.module_source_text(scan, module), 60000),
        )
        raw_batches.append(_review_pass(prompt, cfg, "focused"))
    logger.debug("[检视] %s size_class=%s 批次=%d 高风险=%s", module, size_class, len(raw_batches), flagged)

    merged, observations, invalid = _enrich_and_split(scan, raw_batches)
    total_raw = len(merged) + len(invalid)

    # 补漏：工具独立报出、模型完全没提的位置补为候选 finding。这是纯增益的召回提升，
    # 依据是「工具报告 = 该位置确实有问题」；反之（工具没报）绝不作任何推断。
    tool_report = load_tool_evidence(cfg)
    if tool_report.entries:
        covered = {(f.file, f.line) for f in merged}
        candidates = _tool_candidates(
            scan,
            module_files,
            covered,
            [e.model_dump() for e in tool_report.entries],
            {},  # 独立缓存：候选数量少，且与上方批次解析无共享收益
        )
        if candidates:
            logger.info(
                "[检视] %s 工具补漏 %d 条: %s",
                module,
                len(candidates),
                [f"{f.file}:{f.line} {f.defect_kind}" for f in candidates],
            )
            merged.extend(candidates)

    # 去重：同一锚点 + 同一缺陷种类算同一主张。按行号归并会丢掉同一语句上的不同缺陷
    # （实测 parser.c:13 上并存三个都成立的缺陷），故只在主张级别合并。
    deduped = [Finding.model_validate(d) for d in fu.dedup_claims([f.model_dump() for f in merged])]
    # **先按严重度排序再截断**：原来按批次插入顺序截断，大模块里第三条批次里的致命缺陷
    # 会被第一批次的提示挤掉，且被丢的是哪条只留在调试日志里。
    deduped.sort(key=lambda f: (fu.severity_rank(f.severity), f.file, f.line))
    final_findings = deduped[: cfg.max_findings_per_pass]
    truncated = deduped[len(final_findings):]
    for idx, finding in enumerate(final_findings, 1):
        finding.id = f"f{idx}"
    if truncated:
        logger.warning(
            "[检视] %s 超出上限 %d，已丢弃 %d 条（按严重度降序保留）: %s",
            module,
            cfg.max_findings_per_pass,
            len(truncated),
            [f"{f.file}:{f.line}" for f in truncated],
        )

    # observations：去重、剔除与已确认主张重复的条目、重编号。
    # 原实现只给 findings 重编号，observations 保留各批次自赋的 o1..oN，
    # 而每个批次都被要求「全局唯一」——跨批次不可能做到，实测三个模块全部出现 id 冲突。
    covered = {fu.claim_key(f.model_dump()) for f in final_findings}
    seen_obs: set[tuple[str, str]] = set()
    kept_observations: list[Finding] = []
    redundant = 0
    for observation in observations:
        key = fu.claim_key(observation.model_dump())
        if key in covered or key in seen_obs:
            redundant += 1
            continue
        seen_obs.add(key)
        kept_observations.append(observation)
    kept_observations.sort(key=lambda o: (fu.severity_rank(o.severity), o.file, o.line))
    for idx, observation in enumerate(kept_observations, 1):
        observation.id = f"o{idx}"

    logger.debug(
        "[检视] %s 合并去重 原始=%d 去重后=%d 上限=%d obs原始=%d obs保留=%d(剔除冗余%d) 位置无效降级=%d",
        module,
        total_raw,
        len(final_findings),
        cfg.max_findings_per_pass,
        len(observations),
        len(kept_observations),
        redundant,
        len(invalid),
    )
    logger.info(
        "[检视] %s findings=%d observations=%d%s",
        module,
        len(final_findings),
        len(kept_observations),
        f"（{len(invalid)} 条因位置无效降级）" if invalid else "",
    )
    return RawFindings(findings=final_findings, observations=kept_observations).model_dump()


def _review_one(
    cfg: ReviewConfig,
    scan: ScanResult,
    global_arch: GlobalArchitecture,
    module: str,
    size_class: str,
) -> dict[str, Any]:
    if _findings_raw_path(cfg, module).exists():
        logger.info("[检视] 命中断点: %s", module)
        return {"module_raw": {module: _read_json(_findings_raw_path(cfg, module))}}

    started = time.monotonic()
    data = _review_module_raw(scan, global_arch, module, size_class, cfg)
    _write_json(_findings_raw_path(cfg, module), data)
    logger.info("[检视] %s 完成，耗时 %.1fs", module, time.monotonic() - started)
    return {"module_raw": {module: data}}


def _size_class(cfg: ReviewConfig, sizes: dict[str, Any], module: str) -> str:
    """取模块规模分类；state 缺失时回退读 size.json，两者都没有则报错。

    原来此处是 `.get("size_class") or "large"`——静默兜底成 large 会让一个本应走
    小模块全量路径的模块悄悄改走按文件分批路径，路径选择、提示词、审查环节全变，
    而日志里看不出任何异常。宁可让该模块显式失败（有失败隔离兜底）。
    """
    value = (sizes.get(module) or {}).get("size_class")
    if value:
        return str(value)
    artifact = _read_json(_module_size_path(cfg, module)) if _module_size_path(cfg, module).exists() else {}
    value = artifact.get("size_class")
    if value:
        logger.warning("[检视] %s 规模分类不在 state 中，已从 size.json 回退读取: %s", module, value)
        return str(value)
    raise ValueError(f"模块 {module} 缺少规模分类（state 与 size.json 均无），无法确定检视路径")


def review_module_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    global_arch = GlobalArchitecture.model_validate(state["global_arch"])
    sizes = state.get("module_size") or {}

    def _run(module: str) -> dict[str, Any]:
        return _review_one(cfg, scan, global_arch, module, _size_class(cfg, sizes, module))

    return _execute_batch("review_module", state, _run, channel="module_raw")
