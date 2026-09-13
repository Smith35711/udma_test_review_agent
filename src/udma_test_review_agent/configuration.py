"""评审流程运行期配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .model_config import resolve_model
from .module_map import load_modules_map, parse_selection

_AGENT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ReviewConfig:
    project_root: str
    modules: str = "all"
    model_profile: str = ""
    small_module_line_threshold: int = 2000
    max_review_attempts: int = 3
    first_token_timeout: float = 60.0
    chunk_timeout: float = 30.0
    output_root: str = field(default="")
    max_findings_per_pass: int = 40
    # 模块检视循环的并发上限：0 = 自动（见 nodes/common.py::_AUTO_PARALLEL），N >= 1 为显式上限
    max_parallel: int = 0
    # 反驳给出 confirmed 但置信度低于此值时，由确定性闸门降级为 uncertain。
    # 依据：实测两条误报（count*sizeof 整型溢出、所有权风格问题）的置信度分别是 0.50/0.55，
    # 而真正致命的 use-after-free 是 0.95 —— confidence 原本算了却没有任何消费者。
    confirmed_confidence_threshold: float = 0.6
    # 位置校验：把「文件不在扫描范围/行号越界/空行或注释」的 finding 降级为 observation
    demote_invalid_location: bool = True
    # 采样温度：None 表示沿用模型条目自己的 temperature。
    # 此前流式路径根本不发送该参数，配置里的 temperature 对生产节点完全无效。
    temperature: float | None = None
    # 采样种子：None 表示不发送。仅用于压低重复运行之间的方差，不承诺输出确定。
    seed: int | None = None
    # 扫描排除目录。**这是唯一真相来源**，由扫描节点传给 c_scanner；
    # 原先 ReviewConfig 里有本字段而扫描器另有一份常量，配置写了不生效。
    excludes: tuple[str, ...] = (
        ".git",
        ".svn",
        "__pycache__",
        "build",
        "out",
        "dist",
        "node_modules",
        ".venv",
    )

    def __post_init__(self) -> None:
        if self.model_profile:
            resolve_model(self.model_profile)
        if self.max_parallel < 0:
            raise ValueError(f"并发数不能为负: {self.max_parallel}")
        if not 0.0 <= self.confirmed_confidence_threshold <= 1.0:
            raise ValueError(f"置信度阈值须在 0..1 之间: {self.confirmed_confidence_threshold}")
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise ValueError(f"采样温度须在 0..2 之间: {self.temperature}")
        modules_map = load_modules_map()
        parse_selection(self.modules, [m.id for m in modules_map.modules])
        if not self.output_root:
            self.output_root = "output/review"
        output_path = Path(self.output_root)
        if not output_path.is_absolute():
            self.output_root = str(_AGENT_ROOT / output_path)
        # 空路径会被 Path 视为当前目录，若不拦截会把 cwd 当成被测项目
        if not self.project_root.strip():
            raise ValueError("被测项目目录不能为空")
        project = Path(self.project_root)
        if not project.is_dir():
            raise ValueError(f"被测项目目录不存在: {project}")
        self.project_root = str(project.resolve())
