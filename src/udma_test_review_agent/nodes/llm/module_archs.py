"""S2 模块架构分析节点（LLM）。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...prompts.module_arch import MODULE_ARCH_PROMPT
from ...schemas import ChecklistItem, GlobalArchitecture, ModuleArchitecture, ScanResult
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import (
    _config,
    _global_context_for_module,
    _inventory,
    _load_artifact,
    _module_arch_path,
    _module_order,
    _truncate,
    _write_json,
)
from ._base import _llm

logger = get_logger(__name__)


def _ground_checklist(scan: ScanResult, arch: ModuleArchitecture) -> tuple[ModuleArchitecture, int]:
    """核对检查项的位置：**位置不成立时只清掉位置，不丢弃检查项本身**。

    检查项的价值主要在内容，位置是给下游的加速器。编造的行号若原样带下去，
    会变成检视阶段要去核对的假线索，还会在 `checklist_gaps` 里制造永久性的假漏检记录；
    而整条丢弃又会丢掉本来有效的检查内容。故只降级位置（转为「通例」），保留条目。
    """
    cache: dict[str, list[str]] = {}
    fixed = 0
    items = []
    for item in arch.checklist:
        if not (item.file and item.line):
            items.append(item)
            continue
        location = c_scanner.classify_location(scan, item.file, item.line, cache=cache)
        if location["status"] == "invalid":
            fixed += 1
            logger.warning(
                "[S2] 检查项位置不可核对，已降级为通例: %s:%s（%s）｜%s",
                item.file,
                item.line,
                location["reason"],
                item.item[:60],
            )
            items.append(item.model_copy(update={"file": "", "line": 0}))
            continue
        data = item.model_dump()
        data["file"] = location["path"] or data["file"]
        if not str(data.get("evidence") or "").strip() and location["line_text"].strip():
            data["evidence"] = f"{item.line}| {location['line_text'].strip()}"
        items.append(ChecklistItem.model_validate(data))
    return arch.model_copy(update={"checklist": items}), fixed


def module_archs_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    global_arch = GlobalArchitecture.model_validate(state["global_arch"])
    order = _module_order(cfg, scan)
    logger.debug("[S2] 待分析模块顺序=%s", order)
    archs: dict[str, Any] = dict(state.get("module_archs") or {})
    for module in order:
        arch_path = _module_arch_path(cfg, module)
        cached = _load_artifact(arch_path, ModuleArchitecture)
        if cached is not None:
            archs[module] = cached.model_dump()
            logger.info("[S2] 命中断点: %s", module)
            continue
        inventory = _inventory(scan, module)
        module_files = {f.path for f in inventory.files}
        source = c_scanner.module_source_text(scan, module)
        prompt = MODULE_ARCH_PROMPT.format(
            module=module,
            global_context=_global_context_for_module(global_arch, module, module_files),
            source=_truncate(source, 120000),
        )
        logger.debug(
            "[S2] %s token估算=%d 源字符=%d prompt_chars=%d 产物=%s",
            module,
            inventory.token_estimate,
            len(source),
            len(prompt),
            arch_path,
        )
        arch, fixed = _ground_checklist(scan, _llm(prompt, ModuleArchitecture, cfg))
        _write_json(arch_path, arch.model_dump())
        archs[module] = arch.model_dump()
        located = sum(1 for c in arch.checklist if c.file and c.line)
        logger.info(
            "[S2] 模块架构完成: %s 检查项=%d（已定位 %d，位置降级 %d）",
            module,
            len(arch.checklist),
            located,
            fixed,
        )
    return {"module_archs": archs}
