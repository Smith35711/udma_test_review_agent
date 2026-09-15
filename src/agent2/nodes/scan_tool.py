"""模块扫描（确定性）：把待检视目录切成模块并统计规模，总表落盘。

对应需求规格 §4.1，本节点不调用模型。
"""

from __future__ import annotations

from pathlib import Path

from ..artifacts import write_json
from ..log import get_logger
from ..state import ScanState

logger = get_logger(__name__)

C_SUFFIXES = (".c", ".h")

_ASCII_CHARS_PER_TOKEN = 4
"""预估 Token 的折算比例，只求量级正确，不与某个具体分词器对齐。

ASCII 与非 ASCII 分开算：C 源码里夹杂的中文注释按 1 字符/token 计。若一律按
4 字符/token 折算，带中文注释的模块会被低估数倍，而这个数字的用途正是与上下文
预算相比。
"""


def estimate_tokens(text: str) -> int:
    ascii_count = sum(1 for char in text if char.isascii())
    return round(ascii_count / _ASCII_CHARS_PER_TOKEN) + (len(text) - ascii_count)


def scan_project(project_root: Path) -> list[dict]:
    """按「同目录 + 同文件名主干」把 .c/.h 配成模块，按路径字典序编号 M01、M02……

    以「所在目录 + 主干」为模块键，而不是只用文件名主干：不同子目录下的同名文件
    属于不同模块，只用主干会误合并。
    """
    grouped: dict[str, list[Path]] = {}
    for path in sorted(project_root.rglob("*")):
        if path.is_file() and path.suffix in C_SUFFIXES:
            name = str(path.relative_to(project_root).with_suffix(""))
            grouped.setdefault(name, []).append(path)

    modules: list[dict] = []
    for index, name in enumerate(sorted(grouped), start=1):
        files = grouped[name]
        texts = [path.read_text(encoding="utf-8", errors="replace") for path in files]
        joined = "".join(texts)
        modules.append(
            {
                "id": f"M{index:02d}",
                "name": name,
                "files": [str(path.relative_to(project_root)) for path in files],
                "lines": sum(len(text.splitlines()) for text in texts),
                "chars": len(joined),
                "estimated_tokens": estimate_tokens(joined),
            }
        )
    return modules


def scan_tool(state: ScanState) -> dict:
    config = state["config"]
    modules = scan_project(Path(config.project_root))
    write_json(config.inventory_path, modules)
    logger.info(
        "扫描完成: 模块数=%d 合计行数=%d 总表=%s",
        len(modules),
        sum(module["lines"] for module in modules),
        config.inventory_path,
    )
    return {"modules": modules, "inventory_path": str(config.inventory_path)}
