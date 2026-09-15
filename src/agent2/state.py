"""图状态。字段语义为**后写覆盖**（无 reducer）：节点只返回自己改动的键。

`config` 直接放 `RunConfig` 对象而非它的字典形式：本流程不用 checkpointer，
状态无需可序列化，传对象让节点里读配置保持 `cfg.first_token_timeout` 的写法。
"""

from __future__ import annotations

from typing import TypedDict

from .configuration import RunConfig


class ScanState(TypedDict, total=False):
    """扫描图的状态。"""

    config: RunConfig
    modules: list[dict]
    inventory_path: str
    dependencies: dict
    """模块编号 → 该模块用到的、定义在项目其他模块里的符号（背景信息文本）。"""


class ReviewState(TypedDict, total=False):
    """单模块检视图的状态。"""

    config: RunConfig
    module: dict
    """模块清单中的条目：id / name / files / lines / chars / estimated_tokens。"""
    background: str
    """检视用的背景信息，本期恒为空串，等架构分析的补充节点产出后再填。"""
    findings: list[dict]
    """通过格式校验并编号（f1…fN）的检视意见。"""
    rebuttal_items: list[dict]
    """反驳节点本轮返回的原始条目，未经校验。"""
    rebuttals: list[dict]
    """通过格式校验并合并后的反驳结论，按 id 去重。"""
    omission_rounds: int
    """已交回反驳节点补做的轮数。"""
    rebuttal_pending: bool
    """是否还有未经反驳的条目，供条件边决定回边还是继续。"""
    failed: bool
    fail_reason: str
    report_path: str

    # ---- 仅大模块分支使用（小模块流程里这些键不存在）----
    symbols: dict
    """符号表：对外接口 / 内部函数 / 全局变量。"""
    call_graph: dict
    """调用图：节点、边、强连通分量。"""
    architecture: dict
    """模型的形态判断与分解方案。"""
    chunks: list[dict]
    """通过校验的逻辑子块。"""
    plan_rounds: int
    """分解方案被退回重做的轮数。"""
    plan_pending: bool
    """方案是否还需退回重做，供条件边决定回边还是继续。"""
    plan_feedback: str
    """退回时带给模型的具体原因。"""
    incomplete_chunks: list[str]
    """没能拿到结果的子块。模块仍会出报告，但失败要如实记录与呈现。"""
    plan_violations: list[str]
    """最终方案仍未满足的软约束。写进报告，不藏。"""
    probe_queries: list[dict]
    """待执行的证据请求。"""
    probe_evidence: list[dict]
    """已取回的证据，逐轮累积。"""
    probe_rounds: int
    """已完成的取证轮数。"""
