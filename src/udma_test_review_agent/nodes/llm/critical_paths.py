"""S3 关键路径分析节点（LLM）。"""

from __future__ import annotations

import json
from typing import Any

from ...log import get_logger
from ...prompts.critical_paths import CRITICAL_PATH_PROMPT
from ...schemas import CriticalPathReport, ScanResult
from ...state import ReviewState
from ..common import _config, _critical_paths_path, _module_order, _read_json, _tracked, _write_json
from ._base import _llm

logger = get_logger(__name__)


@_tracked("critical_paths")
def critical_paths_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _critical_paths_path(cfg)
    if persisted.exists():
        logger.info("[S3] 命中断点，跳过关键路径分析: %s", persisted)
        return {"critical_paths": _read_json(persisted)}

    order = _module_order(cfg, ScanResult.model_validate(state["scan"]))
    archs = {k: v for k, v in (state.get("module_archs") or {}).items() if k in order}
    logger.debug(
        "[S3] 参与模块=%d 全局架构字符=%d 模块架构字符=%d 产物=%s",
        len(archs),
        len(json.dumps(state["global_arch"], ensure_ascii=False)),
        len(json.dumps(archs, ensure_ascii=False)),
        persisted,
    )
    prompt = CRITICAL_PATH_PROMPT.format(
        global_arch=json.dumps(state["global_arch"], ensure_ascii=False),
        module_archs=json.dumps(archs, ensure_ascii=False),
    )
    report = _llm(prompt, CriticalPathReport, cfg)
    data = report.model_dump()
    _write_json(persisted, data)
    logger.info("[S3] 关键路径完成: %d 条", len(report.paths))
    return {"critical_paths": data}
