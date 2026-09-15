"""产物读写：流程以落盘产物作为阶段间的交接面，读写集中在这里。

读取一律容错：文件缺失或损坏时返回默认值。检视流程宁可把该阶段当作"没有产物"
重算或跳过，也不该因为一个读不出来的中间文件把整轮打断。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .log import get_logger

logger = get_logger(__name__)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在或内容损坏时返回 `default`。"""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("产物不可读，按缺失处理: %s (%s)", path, exc)
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_text(path: Path, default: str = "") -> str:
    if not path.is_file():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("产物不可读，按缺失处理: %s (%s)", path, exc)
        return default


def read_source_slices(root: str | Path, ranges: list[dict]) -> str:
    """按行区间读取源码片段，每段抬头标注它在原文件中的起始行。

    抬头里的起始行是给模型的**换算锚点**：子块检视只看到模块的一部分，而报告里的
    链接指向原文件的绝对行号——没有这个锚点，模型给出的行号会整体偏移。
    """
    base = Path(root)
    blocks = []
    for item in ranges:
        lines = read_text(base / item["file"]).splitlines()
        body = "\n".join(lines[item["start"] - 1 : item["end"]])
        blocks.append(f"=== {item['file']}（自第 {item['start']} 行起）===\n{body}")
    return "\n".join(blocks)


def read_source_files(root: str | Path, names: list[str]) -> str:
    """按给定顺序读取若干源文件，每段前加 `=== 文件名 ===` 抬头。

    抬头是给模型定位用的：行号按各文件自身计数，没有抬头就无法判断某一行属于哪个文件。
    """
    base = Path(root)
    blocks = []
    for name in names:
        text = read_text(base / name, default="（文件不可读）")
        blocks.append(f"=== {name} ===\n{text}")
    return "\n".join(blocks)
