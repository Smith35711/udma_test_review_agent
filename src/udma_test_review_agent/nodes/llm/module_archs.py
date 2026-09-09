"""S2 模块架构分析节点（LLM）。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...prompts.module_arch import MODULE_ARCH_PROMPT
from ...schemas import GlobalArchitecture, ModuleArchitecture, ScanResult
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import (
    _config,
    _global_context_for_module,
    _module_arch_path,
    _module_order,
    _read_json,
    _tracked,
    _truncate,
    _write_json,
)
from ._base import _llm

logger = get_logger(__name__)


@_tracked("module_archs")
def module_archs_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    scan = ScanResult.model_validate(state["scan"])
    global_arch = GlobalArchitecture.model_validate(state["global_arch"])
    order = _module_order(cfg, scan)
    logger.debug("[S2] 待分析模块顺序=%s", order)
    archs: dict[str, Any] = dict(state.get("module_archs") or {})
    for module in order:
        arch_path = _module_arch_path(cfg, module)
        if arch_path.exists():
            archs[module] = _read_json(arch_path)
            logger.info("[S2] 命中断点: %s", module)
            continue
        inventory = next(m for m in scan.modules if m.name == module)
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
        arch = _llm(prompt, ModuleArchitecture, cfg)
        _write_json(arch_path, arch.model_dump())
        archs[module] = arch.model_dump()
        logger.info("[S2] 模块架构完成: %s", module)
    return {"module_archs": archs}
