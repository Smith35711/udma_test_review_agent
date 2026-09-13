"""单模块 Markdown 格式化节点：确定性渲染 + LLM 执行摘要，落盘 <ID>/report.md。

文档结构：标题与规模 → 执行摘要 → 汇总（确定性统计）→ 已确认问题卡片 →
待人工确认卡片 → 观察项表 → 已否定问题卡片。

**按锚点归并出卡**：同一语句上可能并存多个都成立的不同缺陷（实测 `parser.c:13` 上
三个），逐条出卡会让 23 行的模块渲染出 15 张卡片。归并后同一语句只占一张卡，
卡内列出各条主张及其各自结论——既降噪又不丢信息（不合并主张本身，见 `findings_util`）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ... import findings_util as fu
from ...configuration import ReviewConfig
from ...log import get_logger
from ...prompts.report import REPORT_SUMMARY_PROMPT
from ...state import ReviewState
from ..common import _config, _execute_batch, _module_dir, _truncate, _write_text
from ._base import _llm

logger = get_logger(__name__)

SEVERITY_ORDER = {"致命": 0, "严重": 1, "一般": 2, "提示": 3}

SUMMARY_FILE = "summary.md"
"""模块摘要副本。总报告按自己的链接基准重渲染卡片，需要单独取用摘要文本。"""


def _location_link(project_root: str, link_base: Path, file: str, line: int) -> str:
    """生成可点击的 file:line 链接；`link_base` 是**该 Markdown 将被阅读时所在目录**。

    这个参数必须由调用方按消费方传入：模块报告在 `<模块>/report.md`，总报告在
    `<输出根>/report.md`，两者到被测源码的相对层级不同。原实现固定按模块目录计算，
    而 `report.py` 又把模块 Markdown **原样内联**进总报告——模块报告自身的链接是对的，
    内联后每一条都错两级。故总报告改为用正确的基准重新渲染，而非内联。
    """
    text = f"{file}:{line}"
    if not project_root or not file:
        return f"`{text}`"
    try:
        rel = os.path.relpath(Path(project_root) / file, link_base)
    except (OSError, ValueError):
        return f"`{text}`"
    return f"[{text}]({rel}#L{line})"


def _severity_counts(results: list[dict]) -> dict[str, int]:
    counts = {"致命": 0, "严重": 0, "一般": 0, "提示": 0}
    for item in results:
        severity = fu.norm_severity(item["verdict"].get("severity"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _rebuttal_opinion(verdict: dict | None) -> str:
    if verdict is None:
        return "（观察项，未进入反驳流程）"
    return verdict.get("rebuttal_opinion") or verdict.get("refutation_log", "")


def _anchor_of(finding: dict) -> str:
    return str(finding.get("anchor") or f"{finding.get('file', '?')}:{finding.get('line', 0)}")


def _claim_block(ordinal: str, finding: dict, verdict: dict | None) -> list[str]:
    """单条主张的内容块（描述/根因/修复/影响/证据）。"""
    chain = " → ".join(finding.get("call_chain") or [])
    evidence = finding.get("evidence", "")
    tail = f"{evidence}" + (f" ｜ {chain}" if chain else "")
    kind = fu.defect_kind(finding)
    lines = [f"**[{ordinal}] {finding.get('title', '')}**", ""]
    lines += [f"- **问题描述**：{finding.get('description', '')}", f"- **根因分析**：{finding.get('root_cause', '')}"]
    lines += [f"- **修复方案**：{finding.get('suggestion', '')}", f"- **影响**：{finding.get('impact', '')}"]
    lines += [f"- **证据与调用链**：{tail or '（无）'}", f"- **反驳意见**：{_rebuttal_opinion(verdict)}"]
    if kind.startswith("family:"):
        kind = f"{finding.get('category', '')}（未归类）"
    lines += [f"- **缺陷种类**：{kind}", ""]
    tool = finding.get("tool_evidence") or []
    if tool:
        rendered = "；".join(
            f"{t.get('tool')} `{t.get('rule')}` @{t.get('file')}:{t.get('line')}（{t.get('severity')}）"
            if isinstance(t, dict)
            else str(t)
            for t in tool
        )
        lines += [f"- **工具印证**：{rendered}", ""]
    return lines


def _finding_card(
    index: int,
    group: list[dict],
    conclusion: str,
    project_root: str,
    link_base: Path,
) -> list[str]:
    """一张卡片 = 一个锚点。组内多条主张按各自结论分行列出。"""
    head = group[0]
    # `group` 的元素是 `{finding, verdict}` 包装层，file/line 在 `finding` 里。
    # 直接读包装层会静默拿到空串，卡片位置退化成 `` `:0` ``——链接看着在、指向却是空的。
    head_finding = head.get("finding") or {}
    location = _location_link(
        project_root, link_base, head_finding.get("file", ""), int(head_finding.get("line") or 0)
    )
    severity = fu.worst_severity(
        (item.get("verdict") or {}).get("severity") or item.get("severity") for item in group
    )
    categories = "、".join(sorted({fu.category_family(item["finding"].get("category")) for item in group}))
    lines = [
        f"### {index} · [{severity}] {head['finding'].get('title', '')}",
        "",
        f"> **位置** {location} ｜ **类别** {categories} ｜ **结论** {conclusion}"
        + (f" ｜ **本行共 {len(group)} 条主张**" if len(group) > 1 else ""),
        "",
    ]
    inconsistent = [i for i in group if i["verdict"].get("inconsistent")]
    if inconsistent:
        lines += [
            "> ⚠️ **同一主张存在互相冲突的结论**，已降为 uncertain 并保留全部分歧原文，需人工裁决。",
            "",
        ]
    for ordinal, item in enumerate(group, 1):
        if len(group) > 1:
            lines += [f"#### 主张 {index}.{ordinal}", ""]
        lines += _claim_block(f"{index}.{ordinal}" if len(group) > 1 else str(index), item["finding"], item["verdict"])
    lines += ["---", ""]
    return lines


def _observation_table(observations: list[dict], project_root: str, link_base: Path) -> list[str]:
    """观察项用紧凑表格呈现。

    原来观察项与被确认问题用**完全相同的卡片格式**渲染，唯一区别只是「反驳意见」一行
    写着「未进入反驳流程」。实测观察项数量（19）与 findings（21）相当，读者必须逐张
    卡片才能分辨哪些经过验证——表格让「未经核验」这件事在视觉上就无法忽略。
    """
    if not observations:
        return ["（无）", ""]
    lines = [
        "> 以下条目**未进入反驳流程**，证据链不完整，仅供人工排查线索，不作为结论。",
        "",
        "| # | 位置 | 类别 | 严重度 | 问题 |",
        "|---|---|---|---|---|",
    ]
    for idx, obs in enumerate(observations, 1):
        location = _location_link(project_root, link_base, obs.get("file", ""), int(obs.get("line") or 0))
        title = str(obs.get("title", "")).replace("|", "\\|")
        description = str(obs.get("description", "")).replace("|", "\\|")
        note = f"{title}——{description}" if description else title
        lines.append(
            f"| {idx} | {location} | {fu.category_family(obs.get('category'))} | "
            f"{fu.norm_severity(obs.get('severity'))} | {note} |"
        )
    lines += [""]
    return lines


def _grouped_cards(
    items: list[dict], conclusion: str, start: int, project_root: str, link_base: Path
) -> tuple[list[str], int]:
    """按锚点归并出卡，返回 (markdown 行, 下一张卡片的序号)。"""
    lines: list[str] = []
    index = start
    if not items:
        return ["（无）", ""], index
    findings = [item["finding"] for item in items]
    for group_indices in fu.group_by_anchor(findings):
        index += 1
        group = [items[i] for i in group_indices]
        group.sort(key=lambda i: fu.severity_rank((i.get("verdict") or {}).get("severity") or i["finding"].get("severity")))
        lines += _finding_card(index, group, conclusion, project_root, link_base)
    return lines, index


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
    observations = sorted(observations, key=lambda o: fu.severity_rank(o.get("severity")))
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
    ]
    gaps = payload.get("checklist_gaps") or []
    if gaps:
        lines += [
            f"> ⚠️ **{len(gaps)} 条上游已定位的检查项本轮无任何回应**（可能漏检，请人工复核）：",
            "",
        ]
        lines += [f"> - `{gap}`" for gap in gaps]
        lines += [""]
    lines += ["---", ""]

    index = 0
    lines += ["## 已确认问题（confirmed）", ""]
    cards, index = _grouped_cards(confirmed, "confirmed", index, project_root, report_dir)
    lines += cards

    lines += ["## 待人工确认（uncertain）", ""]
    cards, index = _grouped_cards(uncertain, "uncertain", index, project_root, report_dir)
    lines += cards

    lines += ["## 观察项（observation）", ""]
    lines += _observation_table(observations, project_root, report_dir)

    lines += ["## 已否定问题（refuted）", ""]
    cards, index = _grouped_cards(refuted, "refuted", index, project_root, report_dir)
    lines += cards

    return "\n".join(lines)


def _module_report_one(cfg: ReviewConfig, state: ReviewState, module: str) -> dict[str, Any]:
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
        # 走 `_llm` 而非直接调 `stream_llm_once`：摘要同样需要重试与超时封装，
        # 且 `_llm` 是「LLM 唯一出口」这一定位在节点侧的落点（schema=None 即纯文本）。
        summary = _llm(REPORT_SUMMARY_PROMPT.format(report=_truncate(body, 8000)), None, cfg, role="summary")
        logger.debug("[模块报告] %s 摘要字符=%d", module, len(summary))
    except Exception as exc:  # noqa: BLE001 - 摘要失败不阻塞报告落盘
        logger.warning("[模块报告] %s 摘要生成失败: %s", module, exc)
    module_md = render_module_report(module, payload, size_info, cfg.project_root, report_path.parent, summary=summary)
    _write_text(report_path, module_md)
    # 摘要另存一份：总报告需要用**自己的**链接基准重新渲染各模块卡片（见 report.py），
    # 若只把模块 Markdown 内联过去，其中的相对链接会全部指错目录。
    _write_text(_module_dir(cfg, module) / SUMMARY_FILE, summary)
    logger.info("[模块报告] %s 完成 耗时%.1fs 产物=%s", module, time.monotonic() - started, report_path)
    return {}


def module_report_node(state: ReviewState) -> dict[str, Any]:
    """本节点无状态产物（只写文件），故 channel=None。"""
    cfg = _config(state)
    return _execute_batch(
        "module_report", state, lambda module: _module_report_one(cfg, state, module), channel=None
    )
