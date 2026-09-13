"""外部工具证据节点（确定性）：在模块循环之前跑一次 gcc -fanalyzer 与 cppcheck。

放在 `scan` 之后：需要 `scan.json` 给出的 `.c` 清单、构建参数与项目根。

**尽力而为**：工具缺失、项目不可编译、超时都只记录原因，绝不让流程失败。
`ToolEvidenceReport.available/skipped` 如实记录本次跑了什么、跳过了什么——
一份「工具什么都没报」的结论，必须先能区分是「确实干净」还是「工具根本没跑起来」。
"""

from __future__ import annotations

import time
from typing import Any

from ...log import get_logger
from ...schemas import ScanResult, ToolEvidenceReport
from ...state import ReviewState
from ...tools.executors import static_analyzers
from ..common import _config, _tool_evidence_path, _write_json

logger = get_logger(__name__)


def tool_evidence_node(state: ReviewState) -> dict[str, Any]:
    """本节点只写文件，不向 state 写入（消费方按需从产物读取，与 architecture.json 一致）。"""
    cfg = _config(state)
    path = _tool_evidence_path(cfg)
    if path.exists():
        logger.info("[工具证据] 命中断点: %s", path)
        return {}

    scan = ScanResult.model_validate(state["scan"])
    started = time.monotonic()
    entries, available, skipped = static_analyzers.collect(scan)
    report = ToolEvidenceReport(available=available, skipped=skipped, entries=entries)
    _write_json(path, report.model_dump())

    mapped = sum(1 for e in entries if static_analyzers.map_rule(e.rule) is not None)
    logger.info(
        "[工具证据] 完成 耗时%.1fs 证据=%d（其中可注入 finding 的 %d）可用=%s 跳过=%d 产物=%s",
        time.monotonic() - started,
        len(entries),
        mapped,
        available or "无",
        len(skipped),
        path,
    )
    return {}
