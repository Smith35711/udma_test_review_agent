"""项目总报告节点（确定性）：汇总各模块报告与统计，落盘 report.md。"""

from __future__ import annotations

import time
from typing import Any

from ...log import get_logger
from ...manifest import load_manifest
from ...state import ReviewState
from ..common import (
    _config,
    _findings_verified_path,
    _load_json,
    _load_text,
    _manifest_path,
    _module_dir,
    _module_size_path,
    _output_dir,
    _stats,
    _write_text,
)
from ..llm.module_report import SUMMARY_FILE, render_module_report

logger = get_logger(__name__)


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
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
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

    logger.info(
        "[报告] 完成 %s 耗时%.1fs confirmed=%d refuted=%d uncertain=%d",
        report_path,
        time.monotonic() - started,
        total["confirmed"],
        total["refuted"],
        total["uncertain"],
    )
    if failures:
        logger.error("[报告] %d/%d 个模块评审失败: %s", len(failures), len(order), sorted(failures))
        return {"report_path": str(report_path), "module_failures": failures}
    return {"report_path": str(report_path)}
