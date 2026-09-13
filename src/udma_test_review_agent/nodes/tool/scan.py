"""S0 仓库扫描节点（确定性工具）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ... import manifest, versioning
from ...log import get_logger
from ...module_map import load_modules_map
from ...schemas import ScanResult
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import (
    _config,
    _load_artifact,
    _manifest_path,
    _module_order,
    _output_dir,
    _scan_persist_path,
    _write_json,
)

logger = get_logger(__name__)


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
    # 断点失效判定：先比对本轮输入指纹与上次落盘的清单。不一致说明既有产物是在
    # 别的源码/模型/提示词下产生的，继续复用会让一份报告混着两套来源，故整体归档重算。
    current = manifest.build_manifest(cfg)
    manifest_path = _manifest_path(cfg)
    stored = manifest.load_manifest(manifest_path)
    if stored is None or stored.digest != current.digest:
        changes = "；".join(manifest.describe_changes(stored, current))
        run_ts = versioning.run_timestamp()
        # 归档必须在写入新清单**之前**：archive_all 的清单里含 run_manifest.json，
        # 先写后归档会把刚生成的新清单一起卷进 history，当前目录反而没有清单可用。
        moved = versioning.archive_all(_output_dir(cfg), run_ts)
        if moved:
            logger.warning(
                "[S0] 输入指纹已变化，归档 %d 个既有产物（run=%s），全量重算: %s", moved, run_ts, changes
            )
        else:
            logger.info("[S0] 输入指纹已变化，无既有产物可归档，全量重算: %s", changes)
    else:
        logger.debug("[S0] 输入指纹一致 digest=%s 源码文件=%d", current.digest, len(current.sources))
    manifest.save_manifest(manifest_path, current)

    scan: ScanResult | None = _load_artifact(persisted, ScanResult)
    if scan is not None:
        if not all(m.module_id for m in scan.modules):
            logger.warning("[S0] 扫描产物缺少 module_id（旧格式），重新扫描: %s", persisted)
            scan = None
        elif Path(scan.project_root) != Path(cfg.project_root):
            # 断点只比对产物是否「存在」而不比对来源：换一个 --project 复用同一
            # output_root 时，会把上一个项目的扫描结果当成新项目的，后续所有阶段
            # 都基于错误的文件清单推进，且完全无声。
            logger.warning(
                "[S0] 扫描产物来自其他项目，重新扫描: 产物=%s 当前=%s", scan.project_root, cfg.project_root
            )
            scan = None
        else:
            logger.info("[S0] 命中断点，跳过扫描: %s", persisted)
    if scan is None:
        modules_map = load_modules_map()
        module_ids = {m.path: m.id for m in modules_map.modules}
        logger.debug("[S0] 模块映射 %s", module_ids)
        scan = c_scanner.scan_project(cfg.project_root, module_ids, exclude_dirs=cfg.excludes)
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
        # 以下字段在本次图执行内由后续节点累积，此处显式清空以完整定义「一轮运行从
        # 空状态开始」这一契约；漏掉 module_raw 会让带残留状态重入时静默复用旧检视结果。
        "module_size": {},
        "module_raw": {},
        "module_results": {},
        "review_index": 0,
        "module_failures": {},
    }
