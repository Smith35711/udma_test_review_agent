"""评审流程节点实现：S0~S4、检视、反驳、报告。"""

from __future__ import annotations

import json
import re
import time
from functools import wraps
from pathlib import Path
from typing import Any

from .configuration import ReviewConfig
from .control import run_control
from .llm import stream_llm_once
from .log import get_logger
from .prompts import (
    CRITICAL_PATH_PROMPT,
    DOMAIN_REVIEW_PROMPT,
    FOCUSED_REVIEW_PROMPT,
    GLOBAL_ARCH_PROMPT,
    MODULE_ARCH_PROMPT,
    REBUTTAL_PROMPT,
    REPORT_SUMMARY_PROMPT,
    REVIEW_PROMPT,
)
from .schemas import (
    CriticalPathReport,
    Finding,
    GlobalArchitecture,
    ModuleArchitecture,
    RawFindings,
    RebuttalReport,
    ScanResult,
    Verdict,
)
from .state import ReviewState
from .tools.executors import c_scanner

logger = get_logger(__name__)


def _emit(event: dict[str, Any]) -> None:
    """向流式消费者发布自定义事件；无消费者时静默。"""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(event)
    except Exception:  # noqa: BLE001 - 非流式调用无 writer
        pass


def _tracked(name: str):
    """节点装饰器：发布 start/done/error 生命周期事件。"""

    def deco(func):
        @wraps(func)
        def wrapper(state: ReviewState) -> dict[str, Any]:
            run_control.checkpoint()
            _emit({"type": "node", "name": name, "status": "start"})
            try:
                result = func(state)
            except Exception as exc:
                _emit({"type": "node", "name": name, "status": "error", "message": str(exc)[:300]})
                raise
            _emit({"type": "node", "name": name, "status": "done"})
            return result

        return wrapper

    return deco


def _llm_observer():
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 - 非流式调用无 writer
        return None

    def observer(kind: str, data: dict) -> None:
        try:
            writer({"type": "llm", "kind": kind, **data})
        except Exception:  # noqa: BLE001 - 观察者异常不影响主流程
            pass

    return observer


def _config(state: ReviewState) -> ReviewConfig:
    return ReviewConfig(**state["config"])


def _output_dir(cfg: ReviewConfig, module: str = "") -> Path:
    path = Path(cfg.output_root) / module if module else Path(cfg.output_root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _llm(prompt: str, schema: type, cfg: ReviewConfig, retries: int = 2):
    observer = _llm_observer()
    last_exc: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            return stream_llm_once(
                prompt,
                schema,
                first_token_timeout=cfg.first_token_timeout,
                chunk_timeout=cfg.chunk_timeout,
                observer=observer,
            )
        except (json.JSONDecodeError, ValueError, RuntimeError, TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "[LLM] 调用失败(第%d次): %s: %s；%s",
                attempt,
                type(exc).__name__,
                str(exc)[:200],
                "重试" if attempt <= retries else "放弃",
            )
            time.sleep(2)
    raise last_exc  # type: ignore[misc]


def _truncate(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...(截断，共{len(text)}字)"


def _scan_persist_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "scan.json"


def _global_arch_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "global_architecture.json"


def _critical_paths_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "critical_paths.json"


def _module_dir(cfg: ReviewConfig, module: str) -> Path:
    return _output_dir(cfg, module)


def _module_arch_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "architecture.json"


def _findings_raw_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "findings_raw.json"


def _findings_verified_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "findings_verified.json"


def _module_order(cfg: ReviewConfig, scan: ScanResult) -> list[str]:
    names = [m.name for m in scan.modules]
    if cfg.mode == "single":
        if cfg.target_module not in names:
            raise ValueError(f"目标模块不存在: {cfg.target_module}，可选: {names}")
        return [cfg.target_module]
    return names


# ---------------- S0 扫描 ----------------

@_tracked("scan")
def scan_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _scan_persist_path(cfg)
    if persisted.exists():
        scan = ScanResult.model_validate(_read_json(persisted))
        logger.info("[S0] 命中断点，跳过扫描: %s", persisted)
    else:
        scan = c_scanner.scan_project(cfg.project_root, cfg.small_module_token_threshold)
        _write_json(persisted, scan.model_dump())
    return {"scan": scan.model_dump(), "module_order": _module_order(cfg, scan), "review_index": 0, "module_results": {}}


# ---------------- S1 全局架构 ----------------

@_tracked("global_arch")
def global_arch_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _global_arch_path(cfg)
    if persisted.exists():
        arch = GlobalArchitecture.model_validate(_read_json(persisted))
        logger.info("[S1] 命中断点，跳过全局架构分析: %s", persisted)
        return {"global_arch": arch.model_dump()}

    scan = ScanResult.model_validate(state["scan"])
    root = Path(scan.project_root)
    inventory_lines = [
        f"{m.name} ({m.path}) 文件数={len(m.files)} 约{m.token_estimate}tokens: "
        + ", ".join(f.path for f in m.files)
        for m in scan.modules
    ]
    include_lines = [f"{e.src} -> {e.dst}" for e in scan.include_graph[:200]]
    header_chunks = []
    for module in scan.modules:
        entry = next((f.path for f in module.files if f.path.endswith(".h")), module.files[0].path)
        head = "\n".join((root / entry).read_text(encoding="utf-8", errors="replace").splitlines()[:60])
        header_chunks.append(f"--- {module.name} / {entry} ---\n{head}")
    prompt = GLOBAL_ARCH_PROMPT.format(
        inventory="\n".join(inventory_lines),
        includes="\n".join(include_lines) or "(无本地 include 依赖)",
        headers="\n\n".join(header_chunks),
    )
    arch = _llm(prompt, GlobalArchitecture, cfg)
    _write_json(persisted, arch.model_dump())
    logger.info("[S1] 全局架构完成: 模块=%d 信任面=%d 并发热点=%d", len(arch.modules), len(arch.trust_entries), len(arch.concurrency_hotspots))
    return {"global_arch": arch.model_dump()}


# ---------------- S2 模块架构 ----------------

def _global_context_for_module(global_arch: GlobalArchitecture, module: str, module_files: set[str]) -> str:
    lines = []
    for entry in global_arch.trust_entries:
        if any(entry.file.startswith(f.rsplit("/", 1)[0]) or entry.file in module_files for f in module_files):
            lines.append(f"[信任面] {entry.file}:{entry.line} {entry.kind} {entry.description}")
    for spot in global_arch.concurrency_hotspots:
        if spot.file in module_files or any(spot.file.endswith(f.split("/")[-1]) for f in module_files):
            lines.append(f"[并发] {spot.file}:{spot.line} {spot.kind} {spot.description}")
    convention = "\n".join(f"[惯例] {c}" for c in global_arch.conventions)
    return ("\n".join(lines) + "\n" + convention).strip() or "(无直接相关条目)"


@_tracked("module_archs")
def module_archs_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    global_arch = GlobalArchitecture.model_validate(state["global_arch"])
    order = _module_order(cfg, scan)
    archs: dict[str, Any] = dict(state.get("module_archs") or {})
    for module in order:
        arch_path = _module_arch_path(cfg, module)
        if arch_path.exists():
            archs[module] = _read_json(arch_path)
            logger.info("[S2] 命中断点: %s", module)
            continue
        inventory = next(m for m in scan.modules if m.name == module)
        module_files = {f.path for f in inventory.files}
        source = c_scanner.module_source_text(scan, module)
        prompt = MODULE_ARCH_PROMPT.format(
            module=module,
            global_context=_global_context_for_module(global_arch, module, module_files),
            token_estimate=inventory.token_estimate,
            small_threshold=cfg.small_module_token_threshold,
            source=source if inventory.token_estimate <= cfg.small_module_token_threshold * 2 else _truncate(source, 60000),
        )
        arch = _llm(prompt, ModuleArchitecture, cfg)
        arch.size_class = "small" if inventory.token_estimate <= cfg.small_module_token_threshold else "large"
        arch.token_estimate = inventory.token_estimate
        _write_json(arch_path, arch.model_dump())
        archs[module] = arch.model_dump()
        logger.info("[S2] 模块架构完成: %s 规模=%s", module, arch.size_class)
    return {"module_archs": archs}


# ---------------- S3 关键路径 ----------------

@_tracked("critical_paths")
def critical_paths_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _critical_paths_path(cfg)
    if persisted.exists():
        logger.info("[S3] 命中断点，跳过关键路径分析: %s", persisted)
        return {"critical_paths": _read_json(persisted)}

    order = _module_order(cfg, ScanResult.model_validate(state["scan"]))
    archs = {k: v for k, v in (state.get("module_archs") or {}).items() if k in order}
    prompt = CRITICAL_PATH_PROMPT.format(
        global_arch=json.dumps(state["global_arch"], ensure_ascii=False),
        module_archs=json.dumps(archs, ensure_ascii=False),
    )
    report = _llm(prompt, CriticalPathReport, cfg)
    data = report.model_dump()
    _write_json(persisted, data)
    logger.info("[S3] 关键路径完成: %d 条", len(report.paths))
    return {"critical_paths": data}


# ---------------- S4 汇总校验（确定性跨模块检查） ----------------

@_tracked("merge")
def merge_node(state: ReviewState) -> dict[str, Any]:
    scan = ScanResult.model_validate(state["scan"])
    defined = {f.name for f in scan.functions}
    header_declared: dict[str, str] = {}
    root = Path(scan.project_root)
    for module in scan.modules:
        for file_info in module.files:
            if not file_info.path.endswith(".h"):
                continue
            text = (root / file_info.path).read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                if "(" in line and not line.strip().startswith(("#", "//", "*")):
                    match = re.search(r"\b([A-Za-z_]\w*)\s*\(", line)
                    if match and "=" not in line.split("(", 1)[0]:
                        header_declared.setdefault(match.group(1), f"{file_info.path}:{line_no}")

    candidates = []
    for name, location in sorted(header_declared.items()):
        if name not in defined:
            candidates.append(
                {
                    "category": "logic",
                    "title": f"头文件声明但未见实现: {name}",
                    "file": location,
                    "description": "该函数在头文件中声明，但扫描未在项目内找到定义（可能为外部库依赖或条件编译排除），需跨模块确认。",
                }
            )
    logger.info("[S4] 跨模块候选: %d 条", len(candidates))
    return {"cross_module_candidates": candidates}


# ---------------- 检视（单模块） ----------------

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
    result = _llm(prompt, RawFindings, cfg)
    findings = [f for f in result.findings if f.channel == "finding"]
    observations = list(result.observations) + [f for f in result.findings if f.channel != "finding"]
    return RawFindings(findings=findings, observations=observations)


def _review_module_raw(scan: ScanResult, global_arch: GlobalArchitecture, module: str, cfg: ReviewConfig) -> dict[str, Any]:
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
    if arch.size_class == "small":
        prompt = REVIEW_PROMPT.format(
            **common,
            checklist=_checklist_text(arch),
            source=c_scanner.module_source_text(scan, module),
        )
        raw_batches.append(_review_pass(prompt, cfg))
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
    if _module_flagged(scan, global_arch, module):
        prompt = FOCUSED_REVIEW_PROMPT.format(
            module_arch=common["module_arch"],
            source=_truncate(c_scanner.module_source_text(scan, module), 60000),
        )
        raw_batches.append(_review_pass(prompt, cfg))

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
    logger.info("[检视] %s 原始findings=%d observations=%d", module, len(merged), len(observations))
    return RawFindings(findings=merged[: cfg.max_findings_per_pass], observations=observations).model_dump()


@_tracked("review_module")
def review_module_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    global_arch = GlobalArchitecture.model_validate(state["global_arch"])
    index = state.get("review_index", 0)
    order = state["module_order"]
    module = order[index]

    if _findings_raw_path(cfg, module).exists():
        logger.info("[检视] 命中断点: %s", module)
        return {"module_raw": {module: _read_json(_findings_raw_path(cfg, module))}}

    started = time.monotonic()
    data = _review_module_raw(scan, global_arch, module, cfg)
    _write_json(_findings_raw_path(cfg, module), data)
    logger.info("[检视] %s 完成，耗时 %.1fs", module, time.monotonic() - started)
    return {"module_raw": {module: data}}


# ---------------- 反驳（单模块） ----------------

def _partial_verdicts_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "findings_partial.json"


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
    partial_path = _partial_verdicts_path(cfg, module)
    done: dict[str, dict] = _read_json(partial_path).get("verdicts", {}) if partial_path.exists() else {}
    results = []
    started = time.monotonic()
    for finding in raw.findings:
        if finding.id in done:
            results.append(done[finding.id])
            logger.info("[反驳] %s %s 命中逐条断点，跳过", module, finding.id)
            continue
        func_name, function_code = c_scanner.containing_function_code(scan, finding.file, finding.line)
        callers = c_scanner.upstream_callers(scan, func_name) if func_name else []
        prompt = REBUTTAL_PROMPT.format(
            finding=json.dumps(finding.model_dump(), ensure_ascii=False),
            function_code=function_code or "(未定位到所在函数)",
            callers="\n".join(callers) or "(调用图中无上游调用点，可能为入口函数或近似调用图未覆盖)",
        )
        verdict = _llm(prompt, Verdict, cfg)
        item = {"finding": finding.model_dump(), "verdict": verdict.model_dump()}
        results.append(item)
        done[finding.id] = item
        _write_json(partial_path, {"verdicts": done})
        logger.info(
            "[反驳] %s %s %s -> %s (置信度%.2f)",
            module,
            finding.id,
            finding.title[:30],
            verdict.conclusion,
            verdict.confidence,
        )
    payload = {
        "module": module,
        "results": results,
        "observations": [o.model_dump() for o in raw.observations],
    }
    _write_json(verified_path, payload)
    partial_path.unlink(missing_ok=True)
    stats = _stats(results, len(raw.observations))
    logger.info(
        "[反驳] %s 完成 耗时%.1fs confirmed=%d refuted=%d uncertain=%d",
        module,
        time.monotonic() - started,
        stats["confirmed"],
        stats["refuted"],
        stats["uncertain"],
    )
    return {"module_results": {module: payload}}


def _stats(results: list[dict], observations: int) -> dict[str, int]:
    counts = {"confirmed": 0, "refuted": 0, "uncertain": 0}
    for item in results:
        conclusion = item["verdict"]["conclusion"]
        if conclusion in counts:
            counts[conclusion] += 1
    counts["observations"] = observations
    return counts


# ---------------- 报告 ----------------

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _render_module_report(module: str, payload: dict, elapsed_hint: str = "") -> str:
    results = payload["results"]
    observations = payload.get("observations", [])
    lines = [f"# 模块检视报告: {module}", ""]
    if elapsed_hint:
        lines += [f"> {elapsed_hint}", ""]
    confirmed = [r for r in results if r["verdict"]["conclusion"] == "confirmed"]
    uncertain = [r for r in results if r["verdict"]["conclusion"] == "uncertain"]
    refuted = [r for r in results if r["verdict"]["conclusion"] == "refuted"]
    confirmed.sort(key=lambda r: SEVERITY_ORDER.get(r["verdict"]["severity"], 9))

    lines += ["## 统计", ""]
    lines += [
        f"- 原始 findings: {len(results)}；confirmed={len(confirmed)} refuted={len(refuted)} uncertain={len(uncertain)}；"
        f"observations={len(observations)}",
        f"- 过滤率: {len(refuted) / len(results) * 100:.0f}%" if results else "- 过滤率: N/A",
        "",
    ]

    lines += ["## Confirmed 问题", ""]
    if not confirmed:
        lines += ["（无）", ""]
    for r in confirmed:
        f, v = r["finding"], r["verdict"]
        lines += [
            f"### [{v['severity'].upper()}] {f['title']} ({v['confidence']:.0%}{'，契约依赖，已降级' if v.get('contract_dependent') else ''})",
            "",
            f"- 类别: {f['category']}；位置: {f['file']}:{f['line']}",
            f"- 描述: {f['description']}",
            f"- 调用链: {' -> '.join(f['call_chain']) if f['call_chain'] else '（未提供）'}",
            f"- 证据: {v.get('evidence_note') or '（无补充）'}",
            "",
        ]

    lines += ["## 待人工确认 (uncertain)", ""]
    if not uncertain:
        lines += ["（无）", ""]
    for r in uncertain:
        f, v = r["finding"], r["verdict"]
        lines += [
            f"### {f['title']} (置信度 {v['confidence']:.0%})",
            "",
            f"- 位置: {f['file']}:{f['line']}；类别: {f['category']}",
            f"- 描述: {f['description']}",
            f"- 反驳记录: {v['refutation_log']}",
            "",
        ]

    lines += ["## 已剔除 (refuted 留痕)", ""]
    if not refuted:
        lines += ["（无）", ""]
    for r in refuted:
        f, v = r["finding"], r["verdict"]
        lines += [f"- {f['title']} @{f['file']}:{f['line']} — 反证: {v['refutation_log'][:200]}"]
    lines += [""]

    lines += ["## 观察项 (observation，未入正式结论)", ""]
    if not observations:
        lines += ["（无）", ""]
    for o in observations:
        lines += [f"- [{o['category']}] {o['title']} @{o['file']}:{o['line']} — {o['description'][:160]}"]
    lines += [""]
    return "\n".join(lines)


@_tracked("report")
def report_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    started = time.monotonic()
    order = state["module_order"]
    sections = []
    stats_rows = []
    total = {"confirmed": 0, "refuted": 0, "uncertain": 0, "observations": 0}
    for module in order:
        payload = _read_json(_findings_verified_path(cfg, module))
        counts = _stats(payload["results"], len(payload.get("observations", [])))
        for key in total:
            total[key] += counts.get(key, 0)
        stats_rows.append(
            f"| {module} | {len(payload['results'])} | {counts['confirmed']} | {counts['refuted']} | "
            f"{counts['uncertain']} | {counts['observations']} |"
        )
        module_md = _render_module_report(module, payload)
        try:
            summary = stream_llm_once(
                REPORT_SUMMARY_PROMPT.format(report=_truncate(module_md, 8000)),
                first_token_timeout=cfg.first_token_timeout,
                chunk_timeout=cfg.chunk_timeout,
            )
            module_md += f"\n## 执行摘要\n\n{summary}\n"
        except Exception as exc:  # noqa: BLE001 - 摘要失败不阻塞报告落盘
            logger.warning("[报告] %s 摘要生成失败: %s", module, exc)
        (_module_dir(cfg, module) / "report.md").write_text(module_md, encoding="utf-8")
        sections.append(f"## 模块: {module}\n\n{module_md}")

    overall = ["# 项目检视总报告", "", "## 汇总统计", ""]
    overall += [
        "| 模块 | 原始 | confirmed | refuted | uncertain | observations |",
        "|---|---|---|---|---|---|",
        *stats_rows,
        "",
        f"合计: confirmed={total['confirmed']} refuted={total['refuted']} uncertain={total['uncertain']} observations={total['observations']}",
        "",
    ]
    if state.get("cross_module_candidates"):
        overall += ["## 跨模块候选（S4 预登记，待人工确认）", ""]
        for item in state["cross_module_candidates"]:
            overall += [f"- {item['title']} @{item['file']} — {item['description']}"]
        overall += [""]
    for section in sections:
        overall.append(section)
    report_text = "\n".join(overall)
    report_path = _output_dir(cfg) / "report.md"
    report_path.write_text(report_text, encoding="utf-8")

    elapsed = time.monotonic() - started
    logger.info(
        "[报告] 完成 %s 耗时%.1fs confirmed=%d refuted=%d uncertain=%d",
        report_path,
        elapsed,
        total["confirmed"],
        total["refuted"],
        total["uncertain"],
    )
    return {"report_path": str(report_path)}


@_tracked("advance")
def advance_node(state: ReviewState) -> dict[str, Any]:
    """图循环推进：指向下一模块。"""
    return {"review_index": state.get("review_index", 0) + 1}
