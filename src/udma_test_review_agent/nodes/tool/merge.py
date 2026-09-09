"""S4 汇总校验节点（确定性跨模块检查）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...log import get_logger
from ...schemas import ScanResult
from ...state import ReviewState
from ..common import _tracked

logger = get_logger(__name__)


@_tracked("merge")
def merge_node(state: ReviewState) -> dict[str, Any]:
    scan = ScanResult.model_validate(state["scan"])
    defined = {f.name for f in scan.functions}
    header_declared: dict[str, str] = {}
    root = Path(scan.project_root)
    for module in scan.modules:
        for file_info in module.files:
            if not file_info.path.endswith(".h"):
                continue
            text = (root / file_info.path).read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                if "(" in line and not line.strip().startswith(("#", "//", "*")):
                    match = re.search(r"\b([A-Za-z_]\w*)\s*\(", line)
                    if match and "=" not in line.split("(", 1)[0]:
                        header_declared.setdefault(match.group(1), f"{file_info.path}:{line_no}")

    candidates = []
    for name, location in sorted(header_declared.items()):
        if name not in defined:
            candidates.append(
                {
                    "category": "logic",
                    "title": f"头文件声明但未见实现: {name}",
                    "file": location,
                    "description": "该函数在头文件中声明，但扫描未在项目内找到定义（可能为外部库依赖或条件编译排除），需跨模块确认。",
                }
            )
    logger.debug(
        "[S4] 头文件声明函数=%d 项目内定义=%d 未见实现候选=%d",
        len(header_declared),
        len(defined),
        len(candidates),
    )
    for candidate in candidates:
        logger.debug("[S4] 候选 %s @%s", candidate["title"], candidate["file"])
    logger.info("[S4] 跨模块候选: %d 条", len(candidates))
    return {"cross_module_candidates": candidates}
