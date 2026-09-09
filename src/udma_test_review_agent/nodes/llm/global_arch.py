"""S1 全局架构分析节点（LLM）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...log import get_logger
from ...prompts.global_arch import GLOBAL_ARCH_PROMPT
from ...schemas import GlobalArchitecture, ScanResult
from ...state import ReviewState
from ..common import _config, _global_arch_path, _read_json, _tracked, _write_json
from ._base import _llm

logger = get_logger(__name__)


@_tracked("global_arch")
def global_arch_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _global_arch_path(cfg)
    if persisted.exists():
        arch = GlobalArchitecture.model_validate(_read_json(persisted))
        logger.info("[S1] 命中断点，跳过全局架构分析: %s", persisted)
        return {"global_arch": arch.model_dump()}

    scan = ScanResult.model_validate(state["scan"])
    root = Path(scan.project_root)
    inventory_lines = [
        f"{m.name} ({m.path}) 文件数={len(m.files)} 约{m.token_estimate}tokens: "
        + ", ".join(f.path for f in m.files)
        for m in scan.modules
    ]
    include_lines = [f"{e.src} -> {e.dst}" for e in scan.include_graph[:200]]
    header_chunks = []
    for module in scan.modules:
        entry = next((f.path for f in module.files if f.path.endswith(".h")), module.files[0].path)
        head = "\n".join((root / entry).read_text(encoding="utf-8", errors="replace").splitlines()[:60])
        header_chunks.append(f"--- {module.name} / {entry} ---\n{head}")
    prompt = GLOBAL_ARCH_PROMPT.format(
        inventory="\n".join(inventory_lines),
        includes="\n".join(include_lines) or "(无本地 include 依赖)",
        headers="\n\n".join(header_chunks),
    )
    logger.debug(
        "[S1] 输入 模块=%d include边=%d 入口首部块=%d prompt_chars=%d 产物=%s",
        len(scan.modules),
        len(scan.include_graph),
        len(header_chunks),
        len(prompt),
        persisted,
    )
    arch = _llm(prompt, GlobalArchitecture, cfg)
    _write_json(persisted, arch.model_dump())
    logger.info("[S1] 全局架构完成: 模块=%d 信任面=%d 并发热点=%d", len(arch.modules), len(arch.trust_entries), len(arch.concurrency_hotspots))
    return {"global_arch": arch.model_dump()}
