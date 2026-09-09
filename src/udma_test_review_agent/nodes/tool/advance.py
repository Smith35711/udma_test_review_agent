"""图循环推进节点（确定性）。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...state import ReviewState
from ..common import _tracked

logger = get_logger(__name__)


@_tracked("advance")
def advance_node(state: ReviewState) -> dict[str, Any]:
    """图循环推进：指向下一模块。"""
    index = state.get("review_index", 0)
    logger.debug("[推进] review_index %d -> %d 模块总数=%d", index, index + 1, len(state.get("module_order", [])))
    return {"review_index": index + 1}
