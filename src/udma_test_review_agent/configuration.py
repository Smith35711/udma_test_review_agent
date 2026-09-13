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
    excludes: tuple[str, ...] = (".git", "build", "temp")
    small_module_line_threshold: int = 2000
    max_review_attempts: int = 3
    first_token_timeout: float = 60.0
    chunk_timeout: float = 30.0
    output_root: str = field(default="")
    max_findings_per_pass: int = 40
    # 模块检视循环的并发上限：0 = 自动（见 nodes/common.py::_AUTO_PARALLEL），N >= 1 为显式上限
    max_parallel: int = 0

    def __post_init__(self) -> None:
        if self.model_profile:
            resolve_model(self.model_profile)
        if self.max_parallel < 0:
            raise ValueError(f"并发数不能为负: {self.max_parallel}")
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
