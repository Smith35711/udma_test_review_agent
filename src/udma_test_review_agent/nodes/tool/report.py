"""项目总报告节点（确定性）：汇总各模块报告与统计，落盘 report.md。"""

from __future__ import annotations

import time
from typing import Any

from ...log import get_logger
from ...state import ReviewState
from ..common import (
    _config,
    _findings_verified_path,
    _module_dir,
    _output_dir,
    _read_json,
    _stats,
    _tracked,
)

logger = get_logger(__name__)


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
        module_report_path = _module_dir(cfg, module) / "report.md"
        module_md = (
            module_report_path.read_text(encoding="utf-8")
            if module_report_path.exists()
            else f"# 模块检视报告: {module}\n\n（模块报告缺失）"
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

    logger.info(
        "[报告] 完成 %s 耗时%.1fs confirmed=%d refuted=%d uncertain=%d",
        report_path,
        time.monotonic() - started,
        total["confirmed"],
        total["refuted"],
        total["uncertain"],
    )
    return {"report_path": str(report_path)}
