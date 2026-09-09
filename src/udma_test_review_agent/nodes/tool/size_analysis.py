"""模块尺寸分析节点（确定性）：统计代码行数与字符数，判定 small / large。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...schemas import ModuleSize, ScanResult
from ...state import ReviewState
from ..common import _config, _module_size_path, _read_json, _tracked, _write_json

logger = get_logger(__name__)


@_tracked("size_analysis")
def size_analysis_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    module = state["module_order"][state.get("review_index", 0)]

    persisted = _module_size_path(cfg, module)
    if persisted.exists():
        data = _read_json(persisted)
        logger.info("[尺寸] 命中断点: %s", module)
        return {"module_size": {module: data}}

    inventory = next(m for m in scan.modules if m.name == module)
    lines = sum(file_info.lines for file_info in inventory.files)
    chars = inventory.chars
    size_class = "small" if lines <= cfg.small_module_line_threshold else "large"
    size = ModuleSize(
        module=module,
        lines=lines,
        chars=chars,
        size_class=size_class,
        line_threshold=cfg.small_module_line_threshold,
    )
    _write_json(persisted, size.model_dump())
    logger.info(
        "[尺寸] %s 行=%d 字符=%d 阈值=%d 规模=%s 产物=%s",
        module,
        lines,
        chars,
        cfg.small_module_line_threshold,
        size_class,
        persisted,
    )
    return {"module_size": {module: size.model_dump()}}
