"""图循环推进节点（确定性）。"""

from __future__ import annotations

from typing import Any

from ...log import get_logger
from ...state import ReviewState
from ..common import _batch

logger = get_logger(__name__)


def advance_node(state: ReviewState) -> dict[str, Any]:
    """图循环推进：按当前批次大小步进。

    `review_index` 是**批游标**而非模块游标。`_batch` 为纯切片，故只要游标未越界，
    步长必 >= 1，游标严格递增、图必然终止。
    """
    index = state.get("review_index", 0)
    step = len(_batch(state))
    logger.debug(
        "[推进] review_index %d -> %d 步长=%d 模块总数=%d",
        index,
        index + step,
        step,
        len(state.get("module_order", [])),
    )
    return {"review_index": index + step}
