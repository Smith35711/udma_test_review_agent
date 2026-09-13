"""S1 全局架构分析节点（LLM）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any



from ...log import get_logger
from ...prompts.global_arch import GLOBAL_ARCH_PROMPT
from ...schemas import GlobalArchitecture, ScanResult
from ...state import ReviewState
from ...tools.executors import c_scanner
from ..common import _config, _global_arch_path, _load_artifact, _write_json

from ._base import _llm

logger = get_logger(__name__)


def _ground_entries(scan: ScanResult, arch: GlobalArchitecture) -> tuple[GlobalArchitecture, dict[str, int]]:
    """用扫描产物核对信任面/并发热点：路径归一、**编造的位置丢弃**、缺依据的降为推测。

    全局架构是本工程唯一一处「模型给出的事实性断言被下游当作既有事实」的地方，
    而它此前没有任何确定性核对——`scan`、尺寸判定、反驳核对都有，唯独这里没有。
    实证代价：模型仅凭函数名 `release_twice` 就写出一条并发热点，而样例工程里没有
    任何线程或同步原语；该条目随后进入每个模块的检视上下文与高风险判定，
    制造出一整类「多线程并发写同一缓冲」的假阳性。

    三档处置：
    - 行号 > 0 且位置可核对 → 保留；`evidence` 为空时**用源码原文补齐**
      （确定性取得的依据，比模型自述可靠）；
    - 行号 > 0 但位置不存在（文件不在扫描范围、行号越界、空行/注释）→ **丢弃**并告警：
      它声称了一个具体而错误的位置，比承认没有位置更有害；
    - 行号缺失 → 保留为**推测**，由 `_global_context_for_module` 显式标注、
      要求检视阶段先证实。不静默丢弃，也不与已核实条目混排。
    """
    cache: dict[str, list[str]] = {}
    counts = {"located": 0, "speculative": 0, "dropped": 0}

    def _fix(entries: list[Any], label: str) -> list[Any]:
        kept: list[Any] = []
        for entry in entries:
            if int(entry.line or 0) <= 0:
                counts["speculative"] += 1
                kept.append(entry)
                continue
            location = c_scanner.classify_location(scan, entry.file, entry.line, cache=cache)
            if location["status"] == "invalid":
                counts["dropped"] += 1
                logger.warning(
                    "[S1] 丢弃无依据的%s条目 %s:%s（%s）", label, entry.file, entry.line, location["reason"]
                )
                continue
            data = entry.model_dump()
            data["file"] = location["path"] or data["file"]
            if not str(data.get("evidence") or "").strip():
                text = location["line_text"].strip()
                if text:
                    data["evidence"] = f"{entry.line}| {text}"
            counts["located"] += 1
            kept.append(type(entry).model_validate(data))
        return kept

    grounded = arch.model_copy(
        update={
            "trust_entries": _fix(arch.trust_entries, "信任面"),
            "concurrency_hotspots": _fix(arch.concurrency_hotspots, "并发"),
        }
    )
    return grounded, counts


def global_arch_node(state: ReviewState) -> dict[str, Any]:
    cfg = _config(state)
    persisted = _global_arch_path(cfg)
    cached = _load_artifact(persisted, GlobalArchitecture)
    if cached is not None:
        logger.info("[S1] 命中断点，跳过全局架构分析: %s", persisted)
        return {"global_arch": cached.model_dump()}

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
    arch, counts = _ground_entries(scan, _llm(prompt, GlobalArchitecture, cfg))
    _write_json(persisted, arch.model_dump())
    logger.info(
        "[S1] 全局架构完成: 模块=%d 信任面=%d 并发热点=%d ｜ 已核实=%d 推测=%d 丢弃=%d",
        len(arch.modules),
        len(arch.trust_entries),
        len(arch.concurrency_hotspots),
        counts["located"],
        counts["speculative"],
        counts["dropped"],
    )
    if counts["speculative"]:
        logger.warning(
            "[S1] %d 条信任面/并发热点缺少行级代码依据，已标记为推测，检视阶段须先证实",
            counts["speculative"],
        )
    return {"global_arch": arch.model_dump()}
