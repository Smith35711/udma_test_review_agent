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
from ...prompts.review_repair import CHECKLIST_REPAIR_PROMPT
from ...schemas import (
    Finding,
    GlobalArchitecture,
    ModuleArchitecture,
    RawFindings,
    ReviewCheck,
    ReviewFunnel,
    ScanResult,
)
from ...state import ReviewState
from ...tools.executors import c_scanner, static_analyzers
from ..common import (
    _belongs_to_module,
    _config,
    _critical_paths_for_module,
    _critical_paths_path,
    _entry_is_located,
    _execute_batch,
    _findings_raw_path,
    _global_context_for_module,
    _inventory,
    _load_artifact,
    _load_json,
    _module_arch_path,
    _module_size_path,
    _truncate,
    _write_json,
)
from ..common import _load_tool_evidence as load_tool_evidence
from ._base import _llm

logger = get_logger(__name__)


def _checklist_text(arch: ModuleArchitecture) -> str:
    """渲染检查项；**已定位到 file:line 的显式带出位置**。

    带位置这件事是给模型看的信号：上游架构阶段已经把缺陷定位到了具体语句
    （实测 `parser.c:17` 的 `count==0` 越界就在其中），而检视阶段此前完全丢掉这层信息，
    于是「上游已知、下游丢失」——这正是最典型的漏检模式。位置带出来后，
    提示词第三节的「逐项核对」才有可执行的对象。
    """
    lines = []
    for item in arch.checklist:
        location = f" @{item.file}:{item.line}" if item.file and item.line else ""
        lines.append(f"- [{item.category}] {item.item}{location}")
    return "\n".join(lines) or "(无)"


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
    """本模块是否值得追加一次专项检视。

    只认**已被代码证实**的条目（`_entry_is_located`）：专项检视是一次完整的额外 LLM 调用，
    若让一条凭函数名猜出来的「并发热点」触发它，代价是白花钱 + 产出一整类假阳性。
    模块归属与检视上下文共用 `_belongs_to_module`，避免两处规则不一致导致的静默错配。
    """
    inventory = _inventory(scan, module)
    files = {f.path for f in inventory.files}
    entries = [e for e in global_arch.trust_entries if _belongs_to_module(e.file, files)]
    spots = [s for s in global_arch.concurrency_hotspots if _belongs_to_module(s.file, files)]
    located = [e for e in (*entries, *spots) if _entry_is_located(e)]
    if len(located) != len(entries) + len(spots):
        logger.debug(
            "[检视] %s 高风险判定忽略 %d 条未经证实的架构条目",
            module,
            len(entries) + len(spots) - len(located),
        )
    return bool(located)


def _review_pass(prompt: str, cfg: ReviewConfig, pass_name: str) -> RawFindings:
    logger.debug("[检视] 发起检视 pass=%s prompt_chars=%d", pass_name, len(prompt))
    result = _llm(prompt, RawFindings, cfg, role="review")
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
    return _llm(prompt, ReviewCheck, cfg, role="review")


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
    covered: list[Finding],
    entries: list[dict],
    cache: dict[str, list[str]],
) -> tuple[list[Finding], int, int]:
    """补漏：工具独立报出、而模型在本模块**一个字都没提**的主张，转为候选 finding。

    只在「模型没提过」时注入——模型已提到时，工具的作用是印证或裁决（在
    `review_check`），再注入一条只会重复出卡。两道判定，任一命中即弃用：

    - **同位置**：模型在该语句上已有主张（哪怕缺陷种类不同）。工具报的是不是同一件事
      由 `review_check` 裁决，此处不越权新增；
    - **同一主张**（按 `fu.kinds_overlap` 判同类）：同一函数内同类缺陷模型已主张过。这一条是必需的
      ——工具会为同一处缺陷的每条路径各报一次，而模型只报一次，位置判定拦不住。

    返回 `(候选, 被这两道判定弃用的条数, 因位置校验未通过而丢弃的条数)`。两个计数都要
    进漏斗：工具补漏为 0 既可能是「工具没报」，也可能是「全被拦下了」，含义完全不同。

    同类判定用 `fu.kinds_overlap`，与 `dedup_claims` 同源：模型条目只带族哨兵时，工具
    报出的同族具体种类仍应算「已提过」，否则同一次泄漏会因一侧缺 `defect_kind` 被补第二张卡。
    """
    covered_dicts = [f.model_dump() for f in covered]
    candidates: list[Finding] = []
    redundant = 0
    rejected = 0
    for entry, mapping in static_analyzers.injectable(entries):
        file = entry.get("file", "")
        line = int(entry.get("line") or 0)
        if file not in module_files or line <= 0:
            continue
        same_function = c_scanner.containing_function_name(scan, file, line)
        at_same_line = any(
            str(f.get("file") or "") == file
            and abs(int(f.get("line") or 0) - line) <= static_analyzers.MATCH_TOLERANCE
            for f in covered_dicts
        )
        same_claim = any(
            str(f.get("file") or "") == file
            and c_scanner.containing_function_name(scan, str(f.get("file") or ""), int(f.get("line") or 0))
            == same_function
            and fu.kinds_overlap(
                f.get("defect_kind"), f.get("category"), mapping.defect_kind, mapping.category
            )
            for f in covered_dicts
        )
        if at_same_line or same_claim:
            redundant += 1
            logger.debug(
                "[检视] 工具候选与既有主张重复，弃用 %s:%s %s（函数=%s，%s）",
                file,
                line,
                mapping.defect_kind,
                same_function or "未定位",
                "同位置" if at_same_line else "同函数同种类",
            )
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
            rejected += 1
            continue
        candidates.append(enriched)
        # 已注入的候选也纳入「模型已提过」集合，使同一处缺陷的多条工具路径只补一条。
        covered_dicts.append(enriched.model_dump())
    return candidates, redundant, rejected


_CHECKLIST_LINE_TOLERANCE = 2
"""检查项位置与 finding 位置的允许偏差。

模型的 finding 行号常与检查项相差一两行（指向语句的开头而非关键字）。比对时留一点
容差，避免把「已经检出了」误判成漏检——这个记账一旦虚高，就失去了督促作用。
"""


def _declares_covered_function(
    scan: ScanResult, item: Any, covered_functions: set[str], cache: dict[str, list[str]]
) -> bool:
    """检查项锚在某个函数的**声明行**、而本轮结论落在该函数的实现里，视为已回应。

    头文件声明是这条记账最常见的假警报来源：上游把检查项锚在 `buffer.h:6` 的
    `void copy_name(...)` 上，检视阶段则在 `buffer.c:7` 的函数体里给出了结论。
    两者是同一个函数的两面，按位置比对必然判成「无回应」——实测一轮下来 5 条
    「已定位但无回应」里有 2 条（`buffer.h:6`/`:7`）属于这种误报，而这一节的全部
    价值就在于被人当真。
    只看**声明行里出现的**函数名，不做全文匹配：检查项正文提到某个函数名多半只是
    顺带一提，用它销账会让记账虚高（同 `_CHECKLIST_LINE_TOLERANCE` 的理由）。
    """
    if not covered_functions:
        return False
    text = c_scanner.line_text_at(scan, item.file, item.line, cache=cache)
    if not text:
        return False
    return any(name in covered_functions for name in c_scanner.DECL_NAME_RE.findall(text))


def _checklist_coverage(
    scan: ScanResult,
    arch: ModuleArchitecture,
    findings: list[Finding],
    observations: list[Finding],
    *,
    concern: bool,
) -> list[Any]:
    """返回**尚未被回应**的已定位检查项。

    检查项带着 file:line 进来，说明架构阶段已经看见了那里的问题；若检视阶段整条无回应，
    就是信息在链路上丢失。命中判定的松紧按用途分两档：

    - `concern=False`（最终记账）：位置邻近（±`_CHECKLIST_LINE_TOLERANCE`）或同函数声明
      行有**任意**结论即算回应。这一节是给人看的督促，**假警报比漏报更有害**（整节误报
      会让人整体忽略它），故从宽。
    - `concern=True`（补检触发）：还要求**关切族一致**。从紧是必需的——同一行上可能并存
      多个都成立的缺陷（实测 `parser.c:13` 上 E07 源指针不推进、E08 缺长度校验、
      E09 定长拷贝无 NUL 三件事），按位置销账时报了 E07 就把另两件一起抹掉，而它们分别
      对应上一条已定位检查项。触发补检宁可多问一句，也不能让上游线索这样静默蒸发。
    """
    cache: dict[str, list[str]] = {}
    located = [*findings, *observations]
    covered = {(f.file, f.line, fu.category_family(f.category)) for f in located}
    covered_functions = {
        name
        for f in located
        if (name := c_scanner.containing_function_name(scan, f.file, f.line))
    }
    open_items: list[Any] = []
    for item in arch.checklist:
        if not (item.file and item.line):
            continue
        item_family = fu.category_family(item.category)
        if any(
            file == item.file
            and abs(line - item.line) <= _CHECKLIST_LINE_TOLERANCE
            and (not concern or family == item_family)
            for file, line, family in covered
        ):
            continue
        if _declares_covered_function(scan, item, covered_functions, cache):
            continue
        open_items.append(item)
    return open_items


def _checklist_gaps(
    scan: ScanResult,
    arch: ModuleArchitecture,
    findings: list[Finding],
    observations: list[Finding],
) -> list[str]:
    """上游已定位、而本轮既未出 finding 也未出 observation 的检查项（最终记账）。

    只统计与告警，不改写结论——检查项不成立本来就是合格结果，需要人工区分的是
    「核对过、判定不成立」与「压根没看」。
    """
    return [
        f"{item.file}:{item.line} [{item.category}] {item.item}"
        for item in _checklist_coverage(scan, arch, findings, observations, concern=False)
    ]


def _open_items_text(scan: ScanResult, items: list[Any]) -> str:
    """渲染待补检的检查项：每条附上问题行附近的函数片段，供模型就地问答。"""
    cache: dict[str, list[str]] = {}
    blocks: list[str] = []
    for item in items:
        _, excerpt = c_scanner.function_excerpt(scan, item.file, item.line, cache=cache)
        context = "\n".join(excerpt.splitlines()[:30]) or "(未取到源码片段)"
        blocks.append(
            f"- [{item.category}] {item.item} @{item.file}:{item.line}\n"
            f"  所在函数片段（行号以 | 左侧为准）：\n{context}"
        )
    return "\n\n".join(blocks)


def _critical_paths(state: ReviewState, cfg: ReviewConfig) -> list[dict[str, Any]]:
    """取关键路径清单：优先 state，缺失时回退读产物。

    关键路径只是上下文增益，取不到不该让检视失败——失败会连同该模块的检视一起丢掉，
    代价远大于少一段上下文。
    """
    data = state.get("critical_paths")
    if not data:
        data = _load_json(_critical_paths_path(cfg)) or {}
    return list((data or {}).get("paths") or [])


def _review_module_raw(
    scan: ScanResult,
    global_arch: GlobalArchitecture,
    module: str,
    size_class: str,
    cfg: ReviewConfig,
    state_paths: list[dict[str, Any]],
) -> dict[str, Any]:
    arch = _load_artifact(_module_arch_path(cfg, module), ModuleArchitecture)
    if arch is None:
        # 架构产物是检视的硬输入：缺失或损坏时给出可诊断的错误，交由失败隔离
        # 跳过该模块，而不是让裸的 JSONDecodeError 冒到批次层。
        raise FileNotFoundError(f"模块 {module} 缺少可用的架构产物: {_module_arch_path(cfg, module)}")
    inventory = _inventory(scan, module)
    module_files = {f.path for f in inventory.files}
    global_context = _global_context_for_module(global_arch, module, module_files)
    critical = _critical_paths_for_module(state_paths, module_files)
    common = {
        "module_arch": (
            f"职责={arch.responsibility}\n接口契约:\n{_interfaces_text(arch)}\n"
            f"并发契约: {_contract_text(arch)}\n检查项:\n{_checklist_text(arch)}"
        ),
        "global_context": global_context,
        "critical_paths": critical,
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
                critical_paths=critical,
                headers=headers,
                source=c_scanner.file_source_text(scan, file_info.path),
            )
            raw_batches.append(_review_pass(prompt, cfg, "large"))
    flagged = _module_flagged(scan, global_arch, module)
    if flagged:
        prompt = FOCUSED_REVIEW_PROMPT.format(
            module_arch=common["module_arch"],
            critical_paths=critical,
            source=_truncate(c_scanner.module_source_text(scan, module), 60000),
        )
        raw_batches.append(_review_pass(prompt, cfg, "focused"))
    logger.debug("[检视] %s size_class=%s 批次=%d 高风险=%s", module, size_class, len(raw_batches), flagged)

    merged, observations, invalid = _enrich_and_split(scan, raw_batches)

    # 检查项闭环：上游架构阶段已定位到 file:line 的检查项，若本轮没有任何**同关切族**
    # 的结论，就追加一次只针对这些项的小范围补检，并把结果并入同一套 enrich/去重/反驳
    # 流水线。这修的是信息交接的结构性缺口——原先这些线索只被事后记账，流程里没有任何
    # 动作去补；同一行上的多个不同缺陷还会因位置销账被一并抹掉。
    open_items = _checklist_coverage(scan, arch, merged, observations, concern=True)
    if open_items:
        logger.info(
            "[检视] %s 检查项补检触发 %d 条: %s",
            module,
            len(open_items),
            [f"{item.file}:{item.line}" for item in open_items],
        )
        repair_prompt = CHECKLIST_REPAIR_PROMPT.format(
            module_arch=common["module_arch"],
            open_items=_open_items_text(scan, open_items),
            source=_truncate(c_scanner.module_source_text(scan, module), 60000),
        )
        repair_findings, repair_observations, repair_invalid = _enrich_and_split(
            scan, [_review_pass(repair_prompt, cfg, "repair")]
        )
        merged.extend(repair_findings)
        observations.extend(repair_observations)
        invalid.extend(repair_invalid)
    total_raw = len(merged) + len(invalid)
    funnel = ReviewFunnel(
        model_findings=total_raw,
        model_observations=len(observations) - len(invalid),
        location_invalid=len(invalid),
        checklist_repair=len(open_items),
    )

    # 补漏：工具独立报出、模型完全没提的主张补为候选 finding。这是纯增益的召回提升，
    # 依据是「工具报告 = 该位置确实有问题」；反之（工具没报）绝不作任何推断。
    tool_report = load_tool_evidence(cfg)
    if tool_report.entries:
        candidates, tool_redundant, tool_rejected = _tool_candidates(
            scan,
            module_files,
            merged,
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
        funnel.tool_added = len(candidates)
        funnel.tool_redundant = tool_redundant
        funnel.tool_rejected = tool_rejected
        if tool_redundant or tool_rejected:
            logger.debug(
                "[检视] %s 工具候选弃用 %d 条（与既有主张重复 %d，位置校验未通过 %d）",
                module,
                tool_redundant + tool_rejected,
                tool_redundant,
                tool_rejected,
            )

    # 去重：同一锚点 + 同一缺陷种类算同一主张。按行号归并会丢掉同一语句上的不同缺陷
    # （实测 parser.c:13 上并存三个都成立的缺陷），故只在主张级别合并。
    deduped = [Finding.model_validate(d) for d in fu.dedup_claims([f.model_dump() for f in merged])]
    funnel.after_dedup = len(deduped)
    # **先按严重度排序再截断**：原来按批次插入顺序截断，大模块里第三条批次里的致命缺陷
    # 会被第一批次的提示挤掉，且被丢的是哪条只留在调试日志里。
    deduped.sort(key=lambda f: (fu.severity_rank(f.severity), f.file, f.line))
    final_findings = deduped[: cfg.max_findings_per_pass]
    truncated = deduped[len(final_findings):]
    funnel.capped = len(truncated)
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
    # 用 `claims_match` 判重（而非精确 claim_key）：同一处缺陷在不同批次/补检里可能一次
    # 带具体 defect_kind、一次只是族哨兵，精确键会把它们当两条，观察项会与结论重复出现。
    finding_dicts = [f.model_dump() for f in final_findings]
    kept_observations: list[Finding] = []
    redundant = 0
    for observation in observations:
        obs = observation.model_dump()
        if any(fu.claims_match(f, obs) for f in finding_dicts) or any(
            fu.claims_match(prev.model_dump(), obs) for prev in kept_observations
        ):
            redundant += 1
            continue
        kept_observations.append(observation)
    kept_observations.sort(key=lambda o: (fu.severity_rank(o.severity), o.file, o.line))
    for idx, observation in enumerate(kept_observations, 1):
        observation.id = f"o{idx}"
    funnel.obs_redundant = redundant
    funnel.kept_findings = len(final_findings)
    funnel.kept_observations = len(kept_observations)

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
    gaps = _checklist_gaps(scan, arch, final_findings, kept_observations)
    logger.info(
        "[检视] %s findings=%d observations=%d%s%s",
        module,
        len(final_findings),
        len(kept_observations),
        f"（{len(invalid)} 条因位置无效降级）" if invalid else "",
        f"｜带位置检查项未回应 {len(gaps)} 条" if gaps else "",
    )
    if gaps:
        logger.warning("[检视] %s 以下已定位检查项本轮无任何回应（可能漏检）: %s", module, gaps)
    return RawFindings(
        findings=final_findings,
        observations=kept_observations,
        checklist_gaps=gaps,
        funnel=funnel,
    ).model_dump()


def _review_one(
    cfg: ReviewConfig,
    scan: ScanResult,
    global_arch: GlobalArchitecture,
    module: str,
    size_class: str,
    state_paths: list[dict[str, Any]],
) -> dict[str, Any]:
    cached = _load_artifact(_findings_raw_path(cfg, module), RawFindings)
    if cached is not None:
        logger.info("[检视] 命中断点: %s", module)
        return {"module_raw": {module: cached.model_dump()}}

    started = time.monotonic()
    data = _review_module_raw(scan, global_arch, module, size_class, cfg, state_paths)
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
    artifact = _load_json(_module_size_path(cfg, module)) or {}
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
    paths = _critical_paths(state, cfg)

    def _run(module: str) -> dict[str, Any]:
        return _review_one(cfg, scan, global_arch, module, _size_class(cfg, sizes, module), paths)

    return _execute_batch("review_module", state, _run, channel="module_raw")
