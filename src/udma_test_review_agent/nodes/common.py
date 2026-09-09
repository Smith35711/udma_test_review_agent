"""节点通用辅助：事件发布、生命周期装饰器、配置与产物路径、JSON 读写、统计。"""

from __future__ import annotations

import json
from functools import wraps
from pathlib import Path
from typing import Any

from ..configuration import ReviewConfig
from ..control import run_control
from ..log import get_logger
from ..module_map import id_sort_key, parse_selection
from ..schemas import GlobalArchitecture, ScanResult
from ..state import ReviewState

logger = get_logger(__name__)


def _emit(event: dict[str, Any]) -> None:
    """向流式消费者发布自定义事件；无消费者时静默。"""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(event)
    except Exception:  # noqa: BLE001 - 非流式调用无 writer
        pass


def _tracked(name: str):
    """节点装饰器：发布 start/done/error 生命周期事件。"""

    def deco(func):
        @wraps(func)
        def wrapper(state: ReviewState) -> dict[str, Any]:
            run_control.checkpoint()
            _emit({"type": "node", "name": name, "status": "start"})
            try:
                result = func(state)
            except Exception as exc:
                _emit({"type": "node", "name": name, "status": "error", "message": str(exc)[:300]})
                raise
            _emit({"type": "node", "name": name, "status": "done"})
            return result

        return wrapper

    return deco


def _config(state: ReviewState) -> ReviewConfig:
    return ReviewConfig(**state["config"])


def _output_dir(cfg: ReviewConfig, module: str = "") -> Path:
    path = Path(cfg.output_root) / module if module else Path(cfg.output_root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _truncate(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...(截断，共{len(text)}字)"


def _scan_persist_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "scan.json"


def _global_arch_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "global_architecture.json"


def _critical_paths_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "critical_paths.json"


def _module_dir(cfg: ReviewConfig, module: str) -> Path:
    return _output_dir(cfg, module)


def _module_arch_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "architecture.json"


def _module_size_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "size.json"


def _findings_raw_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "findings_raw.json"


def _findings_verified_path(cfg: ReviewConfig, module: str) -> Path:
    return _module_dir(cfg, module) / "findings_verified.json"


def _module_order(cfg: ReviewConfig, scan: ScanResult) -> list[str]:
    available = [m.module_id for m in scan.modules]
    selected = set(parse_selection(cfg.modules, available))
    order = [
        m.module_id
        for m in sorted(scan.modules, key=lambda m: id_sort_key(m.module_id))
        if m.module_id in selected
    ]
    logger.debug("[模块顺序] selector=%s 全部=%s 选定=%s", cfg.modules, available, order)
    return order


def _global_context_for_module(global_arch: GlobalArchitecture, module: str, module_files: set[str]) -> str:
    lines = []
    for entry in global_arch.trust_entries:
        if any(entry.file.startswith(f.rsplit("/", 1)[0]) or entry.file in module_files for f in module_files):
            lines.append(f"[信任面] {entry.file}:{entry.line} {entry.kind} {entry.description}")
    for spot in global_arch.concurrency_hotspots:
        if spot.file in module_files or any(spot.file.endswith(f.split("/")[-1]) for f in module_files):
            lines.append(f"[并发] {spot.file}:{spot.line} {spot.kind} {spot.description}")
    convention = "\n".join(f"[惯例] {c}" for c in global_arch.conventions)
    return ("\n".join(lines) + "\n" + convention).strip() or "(无直接相关条目)"


def _stats(results: list[dict], observations: int) -> dict[str, int]:
    counts = {"confirmed": 0, "refuted": 0, "uncertain": 0}
    for item in results:
        conclusion = item["verdict"]["conclusion"]
        if conclusion in counts:
            counts[conclusion] += 1
    counts["observations"] = observations
    return counts
