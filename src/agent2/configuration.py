"""运行期配置：命令行参数解析后的唯一形态，随图状态传到每个节点。

产物目录结构也集中在这里（`module_dir` 等），避免各节点各写一份路径拼接。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .llm import LLM_CHUNK_TIMEOUT, LLM_FIRST_TOKEN_TIMEOUT

_AGENT_ROOT = Path(__file__).resolve().parents[2]

STAGE_NAMES = ("arch", "review")
"""可执行的阶段：arch = 架构分析，review = 代码检视。"""

SMALL_MODULE_LINE_LIMIT = 2000
"""小模块的行数上限（`.c` + `.h` 之和）。

小于等于它的走小模块分支，超过的走大模块分支——后者本期留空，命中即跳过。
"""

_ARTIFACT_NAMES = {
    # 每阶段的产物成对：raw 是模型原始产出，checked 是通过确定性格式校验后的结果
    "raw_findings": "findings_raw.json",
    "checked_findings": "findings_checked.json",
    "raw_rebuttals": "rebuttals_raw.json",
    "checked_rebuttals": "rebuttals_checked.json",
    "report": "report.md",
    "error": "error.json",
    # 大模块分支专用：符号表 → 调用图 → 架构方案 → 分块 → 探测取证记录
    "symbols": "symbols.json",
    "call_graph": "call_graph.json",
    "architecture": "architecture.json",
    "chunks": "chunks.json",
    "probe_rounds": "probe_rounds.json",
}


@dataclass
class RunConfig:
    """一次运行的全部配置。字段名与命令行参数保持一致，读代码时不必两套词汇互译。"""

    project_root: str = ""
    """待检视目录，构造时解析为绝对路径。"""
    output_root: str = ""
    """产物根目录；留空则为 `agent2/output`。"""
    stages: tuple[str, ...] = STAGE_NAMES
    modules: tuple[str, ...] = ()
    """待检视模块编号；空元组表示全部模块。"""
    concurrency: int = 0
    """0 = 自动（取 `min(模块数, 4)`），1 = 串行，N > 1 = 并行上限。"""
    model: str = ""
    """模型配置中的 name；留空则依 `LLM_PROFILE` 环境变量与配置 active。"""
    first_token_timeout: float = LLM_FIRST_TOKEN_TIMEOUT
    chunk_timeout: float = LLM_CHUNK_TIMEOUT

    def __post_init__(self) -> None:
        if not self.project_root.strip():
            raise ValueError("待检视目录不能为空")
        project = Path(self.project_root)
        if not project.is_dir():
            raise ValueError(f"待检视目录不存在: {project}")
        self.project_root = str(project.resolve())

        unknown = [name for name in self.stages if name not in STAGE_NAMES]
        if unknown or not self.stages:
            raise ValueError(f"未知阶段: {unknown or '(空)'}，可选: {list(STAGE_NAMES)}")

        if self.concurrency < 0:
            raise ValueError(f"并发数不能为负: {self.concurrency}")
        if self.first_token_timeout <= 0 or self.chunk_timeout <= 0:
            raise ValueError("超时时间必须为正数")

        output = Path(self.output_root) if self.output_root else _AGENT_ROOT / "output"
        if not output.is_absolute():
            output = _AGENT_ROOT / output
        self.output_root = str(output)

    # ---- 产物路径：目录结构的唯一真相来源 ----

    @property
    def references_dir(self) -> Path:
        return Path(self.output_root) / "references"

    @property
    def inventory_path(self) -> Path:
        """模块清单总表的落盘位置。"""
        return self.references_dir / "modules.json"

    @property
    def dependencies_path(self) -> Path:
        """模块间依赖（检视用的背景信息）的落盘位置。"""
        return self.references_dir / "dependencies.json"

    def module_dir(self, module_id: str) -> Path:
        """该模块的产物目录，中间产物与最终产物都放在这里。"""
        return Path(self.output_root) / module_id

    def artifact_path(self, module_id: str, kind: str) -> Path:
        return self.module_dir(module_id) / _ARTIFACT_NAMES[kind]

    def chunk_findings_path(self, module_id: str, index: int) -> Path:
        """子块检视的逐块产物。

        **一块一个文件**：子块是并发跑的，写同一个文件会互相截断
        （`Path.write_text` 没有锁，也没人做原子替换）。
        """
        return self.module_dir(module_id) / f"chunk_{index + 1}_findings.json"

    def worker_count(self, module_count: int) -> int:
        """本次实际并发数。"""
        if self.concurrency > 0:
            return self.concurrency
        return max(min(module_count, 4), 1)

    def llm_options(self) -> dict:
        """传给 `stream_llm_once` 的公共调用参数。"""
        return {
            "profile": self.model or None,
            "first_token_timeout": self.first_token_timeout,
            "chunk_timeout": self.chunk_timeout,
        }
