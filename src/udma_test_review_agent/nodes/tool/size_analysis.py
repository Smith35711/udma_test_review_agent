"""模块尺寸分析节点（确定性）：统计代码行数与字符数，判定 small / large。"""

from __future__ import annotations

from typing import Any

from ...configuration import ReviewConfig
from ...log import get_logger
from ...schemas import ModuleSize, ScanResult
from ...state import ReviewState
from ..common import _config, _execute_batch, _inventory, _load_artifact, _module_size_path, _write_json

logger = get_logger(__name__)


def _size_analysis_one(cfg: ReviewConfig, scan: ScanResult, module: str) -> dict[str, Any]:
    persisted = _module_size_path(cfg, module)
    # 断点复用前先经 schema 校验：损坏产物按「不存在」处理并重算，而不是让它
    # 带着无法解析的内容进入下游。
    cached = _load_artifact(persisted, ModuleSize)
    if cached is not None:
        logger.info("[尺寸] 命中断点: %s", module)
        return {"module_size": {module: cached.model_dump()}}

    inventory = _inventory(scan, module)
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


def size_analysis_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    return _execute_batch(
        "size_analysis", state, lambda module: _size_analysis_one(cfg, scan, module), channel="module_size"
    )
