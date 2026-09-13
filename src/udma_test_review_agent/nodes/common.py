"""节点通用辅助：配置与产物路径、JSON 读写、统计。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from ..artifact_io import read_json as _load_json
from ..artifact_io import read_text as _load_text
from ..artifact_io import write_json_atomic as _write_json
from ..artifact_io import write_text_atomic as _write_text
from ..configuration import ReviewConfig
from ..log import get_logger
from ..manifest import MANIFEST_FILE
from ..module_map import id_sort_key, parse_selection
from ..schemas import GlobalArchitecture, ModuleInventory, ScanResult, ToolEvidenceReport
from ..state import ReviewState

logger = get_logger(__name__)

_M = TypeVar("_M", bound=BaseModel)

__all__ = [
    "_config",
    "_execute_batch",
    "_inventory",
    "_load_artifact",
    "_load_json",
    "_load_text",
    "_output_dir",
    "_write_json",
    "_write_text",
]
"""对外暴露的名字。产物读写原语（`_load_json`/`_write_json`/…）只是 `artifact_io`
在节点层的别名——实现已上移到包级的 `artifact_io`，因为 `manifest.py` 等非节点模块
也需要原子写，放在这里会造成反向依赖。保留同名别名，使节点侧调用点不必关心分层。"""


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


def _load_artifact(path: Path, schema: type[_M]) -> _M | None:
    """读取并经 schema 校验的产物；缺失、损坏或不合 schema 均返回 None。

    这是「产物即接口」在读取侧的落点：调用方拿到 None 即表示「断点不成立，重算」，
    不必自己判断文件是否存在、是否可解析、是否过期格式。
    """
    data = _load_json(path)
    if data is None:
        return None
    try:
        return schema.model_validate(data)
    except ValueError as exc:
        logger.warning("[产物] 不符合 schema，按缺失处理（将重算该阶段）: %s (%s)", path, exc)
        return None


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
    return _load_artifact(_tool_evidence_path(cfg), ToolEvidenceReport) or ToolEvidenceReport()


def _global_arch_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "global_architecture.json"


def _tool_evidence_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "tool_evidence.json"


def _critical_paths_path(cfg: ReviewConfig) -> Path:
    return _output_dir(cfg) / "critical_paths.json"


def _project_findings_path(cfg: ReviewConfig) -> Path:
    """项目级机器可读产物：供 CI 消费、后续基线与抑制使用。"""
    return _output_dir(cfg) / "findings.json"


def _metrics_path(cfg: ReviewConfig) -> Path:
    """过程指标产物：token 用量、各阶段耗时、过滤率（设计文档 P6）。"""
    return _output_dir(cfg) / "metrics.json"


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


def _inventory(scan: ScanResult, module: str) -> ModuleInventory:
    """按模块标识取扫描清单，缺失时给出可诊断的错误。

    全流程以 `module_id` 做 key（`_module_order` 也用 `module_id`），但原先多处写成
    `next(m for m in scan.modules if m.name == module)`——匹配的是**另一个字段**，
    且缺失时抛裸 `StopIteration`，看不出是标识写错还是扫描产物过期。此处同时接受
    `name` 仅为兼容旧产物，主键仍是 `module_id`。
    """
    for inventory in scan.modules:
        if inventory.module_id == module or inventory.name == module:
            return inventory
    raise KeyError(f"扫描产物中没有模块 {module}；可用模块: {[m.module_id for m in scan.modules]}")


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


def _belongs_to_module(file: str, module_files: set[str]) -> bool:
    """某条全局架构条目是否属于本模块。

    两种命中方式：完整相对路径相等，或 basename 在**本模块内唯一**命中（模型常把
    `src/parser.c` 写成 `parser.c`）。检视上下文与高风险判定（`review.py::_module_flagged`）
    必须共用本函数——两处规则不一致时，会出现「提示词里给了这条热点、却不认为本模块
    高风险」或反之的静默错配。
    """
    path = str(file or "").replace("\\", "/").strip()
    if not path:
        return False
    if path in module_files:
        return True
    base = PurePosixPath(path).name
    return sum(1 for f in module_files if PurePosixPath(f).name == base) == 1


def _entry_is_located(entry: Any) -> bool:
    """信任面/并发热点条目是否**已被代码证实**（有行号且有代码依据）。

    这是「已核实」与「推测」的唯一判据，`_global_context_for_module` 的分组与
    `review.py::_module_flagged` 的高风险判定必须共用它——否则会出现「提示词把它当推测、
    高风险判定却拿它触发专项检视」这类静默错配。
    """
    line = int(getattr(entry, "line", 0) or 0)
    evidence = str(getattr(entry, "evidence", "") or "").strip()
    return line > 0 and bool(evidence)


def _global_context_for_module(global_arch: GlobalArchitecture, module: str, module_files: set[str]) -> str:
    """渲染本模块相关的信任面/并发热点/惯例，**已核实与推测分开呈现**。

    推测条目（模型未给出可核对的行级依据）不得与已核实条目混排：下游检视需要先知道
    哪些结论是事实、哪些只是待证实的假设，否则一条凭函数名猜出来的「并发热点」会被
    当作既有事实，直接生成假阳性。此处用显式标注代替静默过滤——丢弃会让上游的问题
    不可见，混排会让下游误信，标注是唯一既保真又不误导的选择。
    """
    located: list[str] = []
    speculative: list[str] = []
    for kind, entries in (("信任面", global_arch.trust_entries), ("并发", global_arch.concurrency_hotspots)):
        for entry in entries:
            if not _belongs_to_module(entry.file, module_files):
                continue
            evidence = str(getattr(entry, "evidence", "") or "").strip()
            line = int(getattr(entry, "line", 0) or 0)
            text = f"{entry.file}:{line} {entry.kind} {entry.description}"
            if _entry_is_located(entry):
                located.append(f"[{kind}] {text} ｜依据：{evidence}")
            else:
                missing = "行号" if line <= 0 else "代码依据"
                speculative.append(f"[{kind}] {text}（缺{missing}，未经证实）")
    lines = located
    if speculative:
        lines += [
            "以下条目**未提供可核对的行级代码依据，属于推测**：必须先在本模块源码中证实其成立，"
            "证不成立则不得据此出 finding；不得把它们当作既有事实引用。"
        ]
        lines += [f"  {s}" for s in speculative]
    lines += [f"[惯例] {c}" for c in global_arch.conventions]
    return "\n".join(lines).strip() or "(无直接相关条目)"


def _critical_paths_for_module(paths: list[dict[str, Any]], module_files: set[str]) -> str:
    """渲染与本模块相关的关键路径，供检视提示词使用。

    `critical_paths` 此前是一处**断头路**：S3 生成后没有任何下游消费者，而设计文档
    要求 S4 的上下文包包含「本模块架构摘要 + 检查项 + 相关关键路径」。关键路径承载的
    是跨模块数据流与并发时序——单个模块的源码里根本看不见、却决定缺陷是否可达的
    那部分信息，恰恰是契约类与并发类断言能否成立的关键。
    """
    related: list[dict[str, Any]] = []
    for path in paths or []:
        steps = path.get("steps") or []
        if any(_belongs_to_module(str(s.get("file", "")), module_files) for s in steps):
            related.append(path)
    if not related:
        return "(本模块不涉及已识别的关键路径)"
    lines = []
    for path in related:
        chain = " → ".join(
            f"{s.get('file', '')}:{s.get('line', 0)} {s.get('description', '')}".strip()
            for s in (path.get("steps") or [])
        )
        lines.append(f"[{path.get('category', '')}] {path.get('title', '')}：{chain}")
        lines.append(f"  风险：{path.get('risk', '')}")
    return "\n".join(lines)


def _stats(results: list[dict], observations: int) -> dict[str, int]:
    counts = {"confirmed": 0, "refuted": 0, "uncertain": 0}
    for item in results:
        conclusion = item["verdict"]["conclusion"]
        if conclusion in counts:
            counts[conclusion] += 1
    counts["observations"] = observations
    return counts
