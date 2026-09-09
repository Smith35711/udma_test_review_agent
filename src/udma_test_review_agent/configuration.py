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

    def __post_init__(self) -> None:
        if self.model_profile:
            resolve_model(self.model_profile)
        modules_map = load_modules_map()
        parse_selection(self.modules, [m.id for m in modules_map.modules])
        if not self.output_root:
            self.output_root = "output/review"
        output_path = Path(self.output_root)
        if not output_path.is_absolute():
            self.output_root = str(_AGENT_ROOT / output_path)
        project = Path(self.project_root)
        if not project.is_dir():
            raise ValueError(f"被测项目目录不存在: {project}")
        self.project_root = str(project.resolve())
