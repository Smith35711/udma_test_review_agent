"""项目总报告节点（确定性）：汇总各模块报告与统计，落盘 report.md 与机器可读产物。

落三份产物，各有各的消费者：
- `report.md`：人读。含过程指标与分级明细。
- `findings.json`：机器读。项目级扁平化条目，带稳定 `identity`，供 CI 门禁与后续
  基线抑制使用；退出码判定也读它（`scripts/run_review.py`）。
- `metrics.json`：过程指标。token 用量、阶段耗时、条目漏斗——回答「这份结论是怎么
  来的、花了多少」，设计文档 P6 把它列为结论可信度依据。
"""

from __future__ import annotations

import time
from typing import Any

from ... import findings_util as fu
from ...configuration import ReviewConfig
from ...log import get_logger
from ...manifest import load_manifest
from ...schemas import RunManifest
from ...state import ReviewState
from ...stats import get_stats
from ..common import (
    _config,
    _findings_verified_path,
    _load_json,
    _load_text,
    _manifest_path,
    _metrics_path,
    _module_dir,
    _module_size_path,
    _output_dir,
    _project_findings_path,
    _stats,
    _write_json,
    _write_text,
)
from ..llm.module_report import SUMMARY_FILE, render_module_report

logger = get_logger(__name__)

EXPORT_SCHEMA_VERSION = 1
"""`findings.json` / `metrics.json` 的格式版本。加了不兼容字段就升版本，消费方据此判断能否解析。"""

_SEVERITIES = ("致命", "严重", "一般", "提示")

FUNNEL_KEYS = (
    "model_findings",
    "model_observations",
    "location_invalid",
    "tool_added",
    "tool_rejected",
    "after_dedup",
    "capped",
    "obs_redundant",
    "kept_findings",
    "kept_observations",
)
"""`ReviewFunnel` 的字段名，用于把各模块漏斗相加成项目级漏斗。"""


def _export_finding(module: str, item: dict[str, Any]) -> dict[str, Any]:
    """把一条 finding+verdict 摊平成机器可读记录。

    `identity` 是 P3 建立的**稳定缺陷身份**（锚点 + 闭集缺陷种类）：`anchor` 里含源文本
    指纹，源码一改就变，因此它天然区分「同一个缺陷又跑了一遍」与「代码变了」。
    CI 做基线抑制要靠它跨运行匹配，而不是靠 `f1` 这类当次运行的序号。
    """
    finding = item.get("finding") or {}
    verdict = item.get("verdict") or {}
    anchor, kind = fu.claim_key(finding)
    return {
        "module": module,
        "id": finding.get("id", ""),
        "identity": f"{anchor}|{kind}",
        "anchor": anchor,
        "anchor_fingerprint": finding.get("anchor_fingerprint", ""),
        "file": finding.get("file", ""),
        "line": int(finding.get("line") or 0),
        "category": finding.get("category", ""),
        "category_family": fu.category_family(finding.get("category")),
        "defect_kind": kind,
        "severity": fu.norm_severity(verdict.get("severity") or finding.get("severity")),
        "title": finding.get("title", ""),
        "conclusion": verdict.get("conclusion", "uncertain"),
        "confidence": verdict.get("confidence", 0.0),
        "verdict_source": verdict.get("verdict_source", "model"),
        "contract_dependent": bool(verdict.get("contract_dependent")),
        "inconsistent": bool(verdict.get("inconsistent")),
        "demoted_reason": verdict.get("demoted_reason", ""),
        "evidence_status": finding.get("evidence_status", ""),
        "source": finding.get("source", "llm"),
        "source_pass": finding.get("source_pass", ""),
        "merged_from": list(finding.get("merged_from") or []),
        "tool_evidence": [e for e in (finding.get("tool_evidence") or []) if isinstance(e, dict)],
    }


def _export_observation(module: str, obs: dict[str, Any]) -> dict[str, Any]:
    anchor, kind = fu.claim_key(obs)
    return {
        "module": module,
        "id": obs.get("id", ""),
        "identity": f"{anchor}|{kind}",
        "anchor": anchor,
        "file": obs.get("file", ""),
        "line": int(obs.get("line") or 0),
        "category": obs.get("category", ""),
        "category_family": fu.category_family(obs.get("category")),
        "defect_kind": kind,
        "severity": fu.norm_severity(obs.get("severity")),
        "title": obs.get("title", ""),
        "evidence_status": obs.get("evidence_status", ""),
        "source_pass": obs.get("source_pass", ""),
    }


def _severity_histogram(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in _SEVERITIES}
    for row in rows:
        counts[row["severity"]] = counts.get(row["severity"], 0) + 1
    return counts


def _worst_confirmed(rows: list[dict[str, Any]]) -> str:
    confirmed = [row["severity"] for row in rows if row["conclusion"] == "confirmed"]
    return fu.worst_severity(confirmed) if confirmed else ""


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _metrics_section(metrics: dict[str, Any], module_rows: list[dict[str, Any]]) -> list[str]:
    """报告里的过程指标段（设计文档 P6：过滤率、token 用量、阶段耗时）。"""
    llm = metrics["llm"]
    lines = ["## 过程指标", ""]
    if llm["calls"]:
        usage = (
            f"prompt {llm['prompt_tokens']} / completion {llm['completion_tokens']} / "
            f"合计 {llm['total_tokens']} tokens"
            if llm["usage_missing"] == 0
            else (
                f"合计 {llm['total_tokens']} tokens（其中 {llm['usage_missing']} 次调用"
                "未返回 usage，实际用量应更高）"
            )
        )
        lines += [
            f"- LLM 调用 {llm['calls']} 次（失败 {llm['failed']} 次），{usage}，"
            f"累计等待 {llm['elapsed']:.1f}s",
            "",
            "| 角色 | 模型 | 调用 | 失败 | tokens | 耗时(s) |",
            "|---|---|---|---|---|---|",
        ]
        for row in llm["by_role"]:
            lines.append(
                f"| {row['role']} | {row['model'] or row['spec']} | {row['calls']} | {row['failed']} | "
                f"{row['total_tokens']} | {row['elapsed']:.1f} |"
            )
        lines += [""]
    else:
        lines += ["- 本次运行未发起 LLM 调用（全部命中断点）", ""]

    stages = metrics["stages"]
    if stages:
        lines += ["| 阶段 | 执行次数 | 累计耗时(s) | 单次最慢(s) |", "|---|---|---|---|"]
        for row in stages:
            lines.append(
                f"| {row['stage']} | {row['runs']} | {row['elapsed']:.1f} | {row['slowest']:.1f} |"
            )
        lines += [""]

    filt = metrics["filter"]
    lines += [
        "**过滤率**（设计文档 P6：refuted 占比异常升高说明检视阶段噪音大，需回查提示词或上下文注入）",
        "",
        f"- findings 结论分布：confirmed {filt['confirmed']} / refuted {filt['refuted']} / "
        f"uncertain {filt['uncertain']}（共 {filt['findings_total']}），"
        f"**refuted 占比 {filt['refuted_rate']:.1%}**",
        f"- observations 占比 **{filt['observation_rate']:.1%}**"
        f"（observations {filt['observations']} / findings+observations "
        f"{filt['findings_total'] + filt['observations']}）——未经反驳流程核验的比例，越低越可信",
        f"- 上游已定位但本轮无回应的检查项：{filt['checklist_gaps']} 条",
        "",
    ]
    funnel = filt["funnel"]
    if any(funnel.values()):
        lines += [
            "| 模块 | 模型 findings | 位置无效降级 | 工具补漏 | 去重后 | 超限丢弃 | 最终 | observations |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in module_rows:
            f = row["funnel"]
            lines.append(
                f"| {row['module']} | {f['model_findings']} | {f['location_invalid']} | "
                f"{f['tool_added']} | {f['after_dedup']} | {f['capped']} | {f['kept_findings']} | "
                f"{f['kept_observations']} |"
            )
        lines += [""]
    return lines


def _build_exports(
    cfg: ReviewConfig,
    order: list[str],
    failures: dict[str, str],
    run_manifest: RunManifest | None,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """读取各模块产物，产出 (findings.json 内容, metrics.json 内容, 模块指标行)。

    与报告渲染共用同一批读盘结果：两者若各读一遍，一旦某个模块的产物在两次读之间
    被改动，报告与机器产物就会互相矛盾。
    """
    findings: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    module_rows: list[dict[str, Any]] = []
    funnel_total = dict.fromkeys(FUNNEL_KEYS, 0)
    for module in order:
        if module in failures:
            continue
        payload = _load_json(_findings_verified_path(cfg, module))
        if payload is None:
            continue
        for item in payload.get("results", []):
            findings.append(_export_finding(module, item))
        for obs in payload.get("observations", []):
            observations.append(_export_observation(module, obs))
        for gap in payload.get("checklist_gaps") or []:
            gaps.append({"module": module, "item": gap})
        funnel = payload.get("funnel") or {}
        row = {key: int(funnel.get(key) or 0) for key in FUNNEL_KEYS}
        for key, value in row.items():
            funnel_total[key] += value
        module_rows.append({"module": module, "funnel": row})

    conclusions = {"confirmed": 0, "refuted": 0, "uncertain": 0}
    for row in findings:
        conclusions[row["conclusion"]] = conclusions.get(row["conclusion"], 0) + 1
    total = len(findings)
    worst = _worst_confirmed(findings)
    stats = get_stats().snapshot()
    filter_block = {
        "findings_total": total,
        **conclusions,
        "refuted_rate": _rate(conclusions["refuted"], total),
        "observations": len(observations),
        "observation_rate": _rate(len(observations), total + len(observations)),
        "checklist_gaps": len(gaps),
        "funnel": funnel_total,
    }
    severity_counts = _severity_histogram(findings)
    export = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "project_root": cfg.project_root,
        "model_profile": cfg.model_profile or "",
        "manifest_digest": run_manifest.digest if run_manifest else "",
        "counts": {
            **conclusions,
            "observations": len(observations),
            "checklist_gaps": len(gaps),
        },
        "severity_counts": severity_counts,
        # 退出码判定的依据：只统计 confirmed——uncertain 是要人看的，不该让构建失败。
        "worst_confirmed_severity": worst,
        "failed_modules": dict(failures),
        "findings": findings,
        "observations": observations,
        "checklist_gaps": gaps,
    }
    metrics = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "project_root": cfg.project_root,
        "manifest_digest": run_manifest.digest if run_manifest else "",
        "wall_clock": stats["wall_clock"],
        "llm": stats["llm"],
        "stages": stats["stages"],
        "filter": filter_block,
        "modules": module_rows,
    }
    return export, metrics, module_rows


def report_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    started = time.monotonic()
    order = state["module_order"]
    # 循环内失败的模块在 state 中留痕；本节点兜底把「无有效产物」的模块也归入其中，
    # 否则读不到 findings_verified.json 会抛 FileNotFoundError 使整轮中止（失败隔离失效）。
    failures = dict(state.get("module_failures") or {})
    sections = []
    stats_rows = []
    output_dir = _output_dir(cfg)
    run_manifest = load_manifest(_manifest_path(cfg))
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    total = {"confirmed": 0, "refuted": 0, "uncertain": 0, "observations": 0}
    all_gaps: list[tuple[str, str]] = []
    for module in order:
        if module in failures:
            logger.warning("[报告] %s 评审失败，未计入统计: %s", module, failures[module])
            continue
        payload = _load_json(_findings_verified_path(cfg, module))
        if payload is None:
            detail = f"report: 缺少可用产物 {_findings_verified_path(cfg, module)}"
            failures[module] = detail
            logger.error("[报告] %s", detail)
            continue
        try:
            counts = _stats(payload["results"], len(payload.get("observations", [])))
        except (KeyError, TypeError) as exc:
            # 阶段未报错却无有效产物（被中断或写入截断），按失败处理而非中止整轮
            detail = f"report: {type(exc).__name__}: {exc}"
            failures[module] = detail
            logger.error("[报告] %s 产物不可用: %s", module, detail, exc_info=exc)
            continue
        for key in total:
            total[key] += counts.get(key, 0)
        all_gaps += [(module, gap) for gap in (payload.get("checklist_gaps") or [])]
        stats_rows.append(
            f"| {module} | {len(payload['results'])} | {counts['confirmed']} | {counts['refuted']} | "
            f"{counts['uncertain']} | {counts['observations']} |"
        )
        # **按总报告自己的链接基准重新渲染**，而不是内联模块报告 Markdown：
        # 模块报告里的相对链接是按 <模块>/ 目录算的，原样内联到 <输出根>/report.md
        # 会让每一条链接都指错目录。摘要从副本取回，内容不丢。
        size_info = _load_json(_module_size_path(cfg, module))
        summary = _load_text(_module_dir(cfg, module) / SUMMARY_FILE)
        module_md = render_module_report(
            module, payload, size_info, cfg.project_root, output_dir, summary=summary
        )
        logger.debug(
            "[报告] %s results=%d confirmed=%d refuted=%d uncertain=%d observations=%d",
            module,
            len(payload["results"]),
            counts["confirmed"],
            counts["refuted"],
            counts["uncertain"],
            counts["observations"],
        )
        sections.append(f"## 模块: {module}\n\n{module_md}")

    export, metrics, module_rows = _build_exports(cfg, order, failures, run_manifest, generated_at)

    overall = ["# 项目检视总报告", "", "## 本次运行", ""]
    # 溯源：原报告完全没有记录用了什么模型、什么阈值、看了哪份源码，
    # 事后无法回答「这份结论是怎么来的」，也无法判断是否可与上一版比较。
    overall += [
        f"- 被测项目：`{cfg.project_root}`",
        f"- 模块选择：{cfg.modules} ｜ 模块数 {len(order)} ｜ 并发上限 {cfg.max_parallel or '自动'}",
        f"- 模型配置：{cfg.model_profile or '(active)'} ｜ 采样温度 {cfg.temperature if cfg.temperature is not None else '条目默认'} ｜ "
        f"种子 {cfg.seed if cfg.seed is not None else '未固定'}",
        f"- 阈值：小模块行数 {cfg.small_module_line_threshold} ｜ 单次上限 {cfg.max_findings_per_pass} ｜ "
        f"确认置信度 {cfg.confirmed_confidence_threshold}",
        f"- 输入指纹：`{run_manifest.digest if run_manifest else '不可用'}`"
        + (f" ｜ 源码文件 {len(run_manifest.sources)} 个" if run_manifest else ""),
        f"- 生成时间：{generated_at}",
        "",
    ]
    overall += ["## 汇总统计", ""]
    overall += [
        "| 模块 | 原始 | confirmed | refuted | uncertain | observations |",
        "|---|---|---|---|---|---|",
        *stats_rows,
        "",
        f"合计: confirmed={total['confirmed']} refuted={total['refuted']} uncertain={total['uncertain']} observations={total['observations']}",
        "",
    ]
    overall += _metrics_section(metrics, module_rows)
    if all_gaps:
        # 漏检记账汇总：这些是上游已经定位到具体语句、检视阶段却没有任何回应的检查项。
        # 放在总报告最显眼的位置，是因为它们**最像漏报**——人工复核应从这里开始。
        overall += [
            f"## ⚠️ 已定位但无回应的检查项（{len(all_gaps)} 条，优先人工复核）",
            "",
            "> 上游架构阶段已把这些检查项定位到具体 file:line，检视阶段既未出 finding 也未出"
            " observation。可能是漏检，也可能是核对后判定不成立——需人工区分。",
            "",
        ]
        overall += [f"- `{module}` {gap}" for module, gap in all_gaps]
        overall += [""]
    if failures:
        overall += [f"> 注意：{len(failures)}/{len(order)} 个模块评审失败，未计入上述统计。", ""]
    if state.get("cross_module_candidates"):
        overall += ["## 跨模块候选（S4 预登记，待人工确认）", ""]
        for item in state["cross_module_candidates"]:
            overall += [f"- {item['title']} @{item['file']} — {item['description']}"]
        overall += [""]
    for section in sections:
        overall.append(section)
    if failures:
        overall += ["", "## 未完成模块（评审失败）", "", "| 模块 | 原因 |", "|---|---|"]
        overall += [f"| {module} | {reason} |" for module, reason in sorted(failures.items())]
        overall += [""]
    report_text = "\n".join(overall)
    report_path = _output_dir(cfg) / "report.md"
    _write_text(report_path, report_text)

    _write_json(_project_findings_path(cfg), export)
    _write_json(_metrics_path(cfg), metrics)

    logger.info(
        "[报告] 完成 %s 耗时%.1fs confirmed=%d refuted=%d uncertain=%d",
        report_path,
        time.monotonic() - started,
        total["confirmed"],
        total["refuted"],
        total["uncertain"],
    )
    logger.info(
        "[报告] 过程指标: LLM %d 次 %d tokens ｜ refuted 占比 %.1f%% ｜ observations 占比 %.1f%% ｜ 产物 %s",
        metrics["llm"]["calls"],
        metrics["llm"]["total_tokens"],
        100 * metrics["filter"]["refuted_rate"],
        100 * metrics["filter"]["observation_rate"],
        _metrics_path(cfg),
    )
    if failures:
        logger.error("[报告] %d/%d 个模块评审失败: %s", len(failures), len(order), sorted(failures))
        return {"report_path": str(report_path), "module_failures": failures}
    return {"report_path": str(report_path)}
