"""S0 仓库扫描节点（确定性工具）。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...module_map import load_modules_map
from ...schemas import ScanResult
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import _config, _module_order, _read_json, _scan_persist_path, _tracked, _write_json

logger = get_logger(__name__)


@_tracked("scan")
def scan_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _scan_persist_path(cfg)
    logger.debug(
        "[S0] 配置 project=%s output=%s modules=%s 小模块行数阈值=%d model=%s 断点文件=%s",
        cfg.project_root,
        cfg.output_root,
        cfg.modules,
        cfg.small_module_line_threshold,
        cfg.model_profile or "(active)",
        persisted,
    )
    scan: ScanResult | None = None
    if persisted.exists():
        scan = ScanResult.model_validate(_read_json(persisted))
        if all(m.module_id for m in scan.modules):
            logger.info("[S0] 命中断点，跳过扫描: %s", persisted)
        else:
            logger.warning("[S0] 扫描产物缺少 module_id（旧格式），重新扫描: %s", persisted)
            scan = None
    if scan is None:
        modules_map = load_modules_map()
        module_ids = {m.path: m.id for m in modules_map.modules}
        logger.debug("[S0] 模块映射 %s", module_ids)
        scan = c_scanner.scan_project(cfg.project_root, module_ids)
        _write_json(persisted, scan.model_dump())
    logger.debug(
        "[S0] 扫描结果 模块=%d 函数=%d include边=%d 调用边=%d 构建参数=%d",
        len(scan.modules),
        len(scan.functions),
        len(scan.include_graph),
        len(scan.call_graph),
        len(scan.build_flags),
    )
    return {
        "scan": scan.model_dump(),
        "module_order": _module_order(cfg, scan),
        "module_size": {},
        "review_index": 0,
        "module_results": {},
    }
