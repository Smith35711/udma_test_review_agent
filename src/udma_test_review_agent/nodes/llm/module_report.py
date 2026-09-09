"""单模块 Markdown 格式化节点：确定性渲染 + LLM 执行摘要，落盘 <ID>/report.md。

文档结构：标题与规模 → 执行摘要 → 汇总（确定性统计）→ 已确认问题卡片 →
待人工确认卡片 → 观察项卡片 → 已否定问题卡片（与被确认卡片同格式，置于文末）。
四类条目统一采用标准卡片格式。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ...llm import stream_llm_once
from ...log import get_logger
from ...prompts.report import REPORT_SUMMARY_PROMPT
from ...state import ReviewState
from ..common import _config, _module_dir, _tracked, _truncate

logger = get_logger(__name__)

SEVERITY_ORDER = {"致命": 0, "严重": 1, "一般": 2, "提示": 3}


def _location_link(project_root: str, report_dir: Path, file: str, line: int) -> str:
    """生成可点击的 file:line 链接；无法计算相对路径时退化为行内代码。"""
    text = f"{file}:{line}"
    if not project_root:
        return f"`{text}`"
    try:
        rel = os.path.relpath(Path(project_root) / file, report_dir)
    except (OSError, ValueError):
        return f"`{text}`"
    return f"[{text}]({rel}#L{line})"


def _severity_counts(results: list[dict]) -> dict[str, int]:
    counts = {"致命": 0, "严重": 0, "一般": 0, "提示": 0}
    for item in results:
        severity = item["verdict"].get("severity", "")
        if severity in counts:
            counts[severity] += 1
    return counts


def _rebuttal_opinion(verdict: dict | None) -> str:
    if verdict is None:
        return "（观察项，未进入反驳流程）"
    return verdict.get("rebuttal_opinion") or verdict.get("refutation_log", "")


def _finding_card(
    index: int,
    finding: dict,
    verdict: dict | None,
    conclusion: str,
    project_root: str,
    report_dir: Path,
) -> list[str]:
    location = _location_link(project_root, report_dir, finding["file"], finding["line"])
    severity = (verdict or {}).get("severity") or finding.get("severity", "")
    chain = " → ".join(finding.get("call_chain") or [])
    evidence = finding.get("evidence", "")
    tail = f"{evidence}" + (f" ｜ {chain}" if chain else "")
    return [
        f"### {index} · [{severity}] {finding['title']}",
        "",
        f"> **位置** {location} ｜ **类别** {finding['category']} ｜ **结论** {conclusion}",
        "",
        f"**问题描述**：{finding.get('description', '')}",
        "",
        f"**根因分析**：{finding.get('root_cause', '')}",
        "",
        f"**修复方案**：{finding.get('suggestion', '')}",
        "",
        f"**影响**：{finding.get('impact', '')}",
        "",
        f"**证据与调用链**：{tail}",
        "",
        f"**反驳意见**：{_rebuttal_opinion(verdict)}",
        "",
        "---",
        "",
    ]


def render_module_report(
    module: str,
    payload: dict,
    size_info: dict | None,
    project_root: str,
    report_dir: Path,
    summary: str = "",
) -> str:
    results = payload["results"]
    observations = payload.get("observations", [])
    confirmed = [r for r in results if r["verdict"]["conclusion"] == "confirmed"]
    uncertain = [r for r in results if r["verdict"]["conclusion"] == "uncertain"]
    refuted = [r for r in results if r["verdict"]["conclusion"] == "refuted"]
    confirmed.sort(key=lambda r: SEVERITY_ORDER.get(r["verdict"].get("severity"), 9))
    uncertain.sort(key=lambda r: SEVERITY_ORDER.get(r["verdict"].get("severity"), 9))
    refuted.sort(key=lambda r: SEVERITY_ORDER.get(r["verdict"].get("severity"), 9))
    observations = sorted(observations, key=lambda o: SEVERITY_ORDER.get(o.get("severity"), 9))
    counts = _severity_counts(results)

    lines = [f"# {module} 检视问题说明", ""]
    meta = []
    if size_info:
        meta.append(f"规模 {size_info.get('lines', '?')} 行 / {size_info.get('chars', '?')} 字符")
        if size_info.get("size_class"):
            meta.append(size_info["size_class"])
    if meta:
        lines += ["> " + " · ".join(meta), ""]

    if summary:
        lines += ["## 执行摘要", "", summary, ""]

    lines += ["## 汇总（确定性统计）", ""]
    lines += [
        f"**致命 {counts['致命']} · 严重 {counts['严重']} · 一般 {counts['一般']} · 提示 {counts['提示']}** ｜ "
        f"confirmed {len(confirmed)} · uncertain {len(uncertain)} · refuted {len(refuted)} ｜ "
        f"observations {len(observations)}",
        "",
        "---",
        "",
    ]

    index = 0

    lines += ["## 已确认问题（confirmed）", ""]
    if not confirmed:
        lines += ["（无）", ""]
    for item in confirmed:
        index += 1
        lines += _finding_card(index, item["finding"], item["verdict"], "confirmed", project_root, report_dir)

    lines += ["## 待人工确认（uncertain）", ""]
    if not uncertain:
        lines += ["（无）", ""]
    for item in uncertain:
        index += 1
        lines += _finding_card(index, item["finding"], item["verdict"], "uncertain", project_root, report_dir)

    lines += ["## 观察项（observation）", ""]
    if not observations:
        lines += ["（无）", ""]
    for obs in observations:
        index += 1
        lines += _finding_card(index, obs, None, "observation", project_root, report_dir)

    lines += ["## 已否定问题（refuted，与被确认问题同格式）", ""]
    if not refuted:
        lines += ["（无）", ""]
    for item in refuted:
        index += 1
        lines += _finding_card(index, item["finding"], item["verdict"], "refuted", project_root, report_dir)

    return "\n".join(lines)


@_tracked("module_report")
def module_report_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    module = state["module_order"][state.get("review_index", 0)]
    report_path = _module_dir(cfg, module) / "report.md"
    if report_path.exists():
        logger.info("[模块报告] 命中断点: %s", module)
        return {}

    payload = state["module_results"][module]
    size_info = (state.get("module_size") or {}).get(module)
    started = time.monotonic()
    body = render_module_report(module, payload, size_info, cfg.project_root, report_path.parent)
    summary = ""
    try:
        summary = stream_llm_once(
            REPORT_SUMMARY_PROMPT.format(report=_truncate(body, 8000)),
            profile=cfg.model_profile or None,
            first_token_timeout=cfg.first_token_timeout,
            chunk_timeout=cfg.chunk_timeout,
        )
        logger.debug("[模块报告] %s 摘要字符=%d", module, len(summary))
    except Exception as exc:  # noqa: BLE001 - 摘要失败不阻塞报告落盘
        logger.warning("[模块报告] %s 摘要生成失败: %s", module, exc)
    module_md = render_module_report(module, payload, size_info, cfg.project_root, report_path.parent, summary=summary)
    report_path.write_text(module_md, encoding="utf-8")
    logger.info("[模块报告] %s 完成 耗时%.1fs 产物=%s", module, time.monotonic() - started, report_path)
    return {}
