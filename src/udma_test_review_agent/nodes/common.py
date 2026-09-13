"""节点通用辅助：配置与产物路径、JSON 读写、统计。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..configuration import ReviewConfig
from ..log import get_logger
from ..manifest import MANIFEST_FILE
from ..module_map import id_sort_key, parse_selection
from ..schemas import GlobalArchitecture, ScanResult, ToolEvidenceReport
from ..state import ReviewState

logger = get_logger(__name__)


_CONFIG_CACHE: dict[tuple, ReviewConfig] = {}
_CONFIG_CACHE_MAX = 8
"""`_config` 的解析结果缓存。

`ReviewConfig.__post_init__` 会解析模型配置（读 YAML）、加载模块映射、校验被测目录
是否存在——而**每个节点、每轮循环**都会调用一次 `_config`，同一次运行内这些输入
完全相同。缓存后一次运行只解析一次。上限 8 仅为防止长驻进程（Web 用法）无限增长。
"""


def _config(state: ReviewState) -> ReviewConfig:
    """从 state 重建 ReviewConfig。

    **返回的对象按只读对待**：命中缓存时多个节点共享同一实例，改写它会污染后续节点。
    state["config"] 是 `asdict()` 产物（值均为可哈希的标量/元组），故可用内容作缓存键。
    """
    raw = state["config"]
    try:
        key = tuple(sorted(raw.items()))
        hash(key)
    except TypeError:  # 出现不可哈希取值时退回不缓存，正确性优先
        return ReviewConfig(**raw)
    cached = _CONFIG_CACHE.get(key)
    if cached is None:
        cached = ReviewConfig(**raw)
        if len(_CONFIG_CACHE) >= _CONFIG_CACHE_MAX:
            _CONFIG_CACHE.pop(next(iter(_CONFIG_CACHE)))
        _CONFIG_CACHE[key] = cached
    return cached


_AUTO_PARALLEL = 8
"""并发数配置为 0（自动）时的实际并发上限。"""


def _max_parallel(state: ReviewState) -> int:
    """模块循环的并发上限：读 state["config"]["max_parallel"]；0 或非法值回退 _AUTO_PARALLEL。"""
    raw = (state.get("config") or {}).get("max_parallel", 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _AUTO_PARALLEL
    return value if value > 0 else _AUTO_PARALLEL


def _batch(state: ReviewState) -> list[str]:
    """当前批次的模块切片（纯切片，**不**按失败模块过滤）。

    契约：六个循环节点与 `advance` 必须共用本函数，且**只有 `advance` 可以写
    `review_index`**；否则同一轮迭代内各节点会看到不同的批次。

    必须是纯切片：`advance` 按 `len(_batch(state))` 步进，若此处滤掉已失败模块，
    整批都失败时步长为 0，图将死循环。跳过失败模块是执行期（`_execute_batch`）的职责。
    """
    index = state.get("review_index", 0)
    order = state.get("module_order") or []
    return list(order[index : index + _max_parallel(state)])


def _execute_batch(
    node: str,
    state: ReviewState,
    run_one: Callable[[str], dict[str, Any]],
    *,
    channel: str | None,
) -> dict[str, Any]:
    """执行当前批次的模块：按模块隔离失败，并合并为**一次**状态写入。

    `run_one(module)` 返回 `{channel: {module: value}}`（`channel` 为 None 时返回 `{}`）。
    有效并发 <= 1 时在调用线程内联执行（不建线程池）；否则用线程池。
    单个模块抛异常只记入 `module_failures` 并继续其余模块，不中断整轮。
    """
    existing = dict(state.get("module_failures") or {})
    pending = [module for module in _batch(state) if module not in existing]

    def _run(module: str) -> tuple[str, dict[str, Any], Exception | None]:
        try:
            return module, run_one(module), None
        except Exception as exc:  # noqa: BLE001 - 单模块失败不终止整轮
            return module, {}, exc

    workers = min(_max_parallel(state), len(pending))
    logger.debug(
        "[%s] 批次=%s 待处理=%d 并发=%d", node, _batch(state), len(pending), workers
    )
    outcomes: list[tuple[str, dict[str, Any], Exception | None]] = []
    if workers <= 1:
        outcomes = [_run(module) for module in pending]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"mr-{node}") as pool:
            futures = [pool.submit(_run, module) for module in pending]
            # 按提交序取回，使合并与失败日志的顺序确定（完成时序仍不确定）
            outcomes = [future.result() for future in futures]

    merged: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for module, fragment, exc in outcomes:
        if exc is not None:
            detail = f"{node}: {type(exc).__name__}: {exc}"
            failures[module] = detail
            # 统一由驱动线程记录，保证堆栈完整、顺序确定
            logger.error("[%s] 模块 %s 失败: %s（跳过该模块后续阶段）", node, module, detail, exc_info=exc)
            continue
        if channel:
            merged.update(fragment.get(channel) or {})

    result: dict[str, Any] = {}
    if channel and merged:
        result[channel] = merged
    if failures:
        result["module_failures"] = {**existing, **failures}
    return result


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


def _manifest_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / MANIFEST_FILE


def _load_tool_evidence(cfg: ReviewConfig) -> ToolEvidenceReport:
    """读取工具证据产物；缺失或损坏时返回空报告（视同「本次没有工具证据」）。

    **不抛异常**：工具层是补充信息，产物不可用不该阻断检视、反驳与核对阶段。
    放在 common 而非工具节点里，是为了让检视与核对两侧都从同一处取用，
    避免节点之间互相 import。
    """
    path = _tool_evidence_path(cfg)
    if not path.exists():
        return ToolEvidenceReport()
    try:
        return ToolEvidenceReport.model_validate(_read_json(path))
    except (OSError, ValueError) as exc:
        logger.warning("[工具证据] 产物不可用，按无证据处理: %s (%s)", path, exc)
        return ToolEvidenceReport()


def _global_arch_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "global_architecture.json"


def _tool_evidence_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "tool_evidence.json"


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
