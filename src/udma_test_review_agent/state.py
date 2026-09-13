"""评审流程图状态。"""

from __future__ import annotations

from typing import Any, TypedDict


class ReviewState(TypedDict, total=False):
    config: dict[str, Any]
    scan: dict[str, Any]
    global_arch: dict[str, Any]
    module_archs: dict[str, Any]
    critical_paths: dict[str, Any]
    cross_module_candidates: list[dict[str, Any]]
    module_order: list[str]
    module_size: dict[str, Any]
    module_raw: dict[str, Any]
    review_index: int
    module_results: dict[str, Any]
    report_path: str
    # 检视失败的模块 -> "<节点>: <异常类型>: <消息>"。必须在此声明：
    # LangGraph 会静默丢弃未声明的状态键，漏声明则失败隔离形同虚设且无任何报错。
    # 无 reducer，故由各节点读改写整个 dict 累积（同 module_archs 模式）。
    module_failures: dict[str, str]
