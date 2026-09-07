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
    module_raw: dict[str, Any]
    review_index: int
    module_results: dict[str, Any]
    report_path: str
