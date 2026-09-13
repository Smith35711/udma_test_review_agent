"""产物读写原语：原子写 + 容错读。

**为什么单独成模块**：断点续跑的判据是「产物文件是否存在」，于是「写产物」和
「读产物」这两个动作的正确性直接决定了整条流水线能不能重入。而需要它们的既有节点
（`nodes/**`），也有非节点模块（`manifest.py` 写运行清单）与脚本。若把这组原语
放在 `nodes/common.py`，`manifest.py` 就得反向依赖节点层——分层立刻倒置。

因此本模块只依赖标准库：不认识 schema、不认识配置、不认识产品语义。
上层（`nodes/common.py`）在其上叠加 schema 校验与配置相关的路径函数。

**两条约定**：

1. **写必须原子**。`write_text` 在进程被中断（Ctrl-C、OOM、超时杀）时会留下一个
   *存在但截断* 的文件——它满足「产物存在」，下次运行会把它当作有效断点，读取时
   抛 `JSONDecodeError` 使该模块永久失败，只能人工删文件恢复。先写同目录临时文件
   再 `os.replace` 顶替，使「文件出现」与「内容完整」成为同一个事件。
2. **读必须容错**。产物损坏、缺失、schema 过期都应降级为「本次没有这个产物」，
   触发该阶段重算，而不是把异常抛到批次层。读不到就重算，永远比崩掉安全。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .log import get_logger

logger = get_logger(__name__)


def write_text_atomic(path: Path, text: str) -> None:
    """原子写纯文本：先写同目录临时文件，`fsync` 后 `os.replace` 顶替目标。

    临时文件与目标同目录，保证 `os.replace` 是同文件系统内的原子操作；
    `fsync` 保证内容先于改名落盘（否则崩溃后可能得到一个空文件）。
    失败时清理临时文件再抛出，不给下次运行留下垃圾。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def write_json_atomic(path: Path, data: Any) -> None:
    """原子写 JSON。序列化在开临时文件**之前**完成：序列化失败不该留下临时文件。"""
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path) -> Any:
    """读取 JSON；**缺失或损坏一律返回 None**（视同「本次没有这个产物」）。

    损坏必须降级为「不存在」而不是抛异常：一个截断的产物既不能复用，也不该让整轮
    中止——它应该触发该阶段重算。所有调用方都按 `None` 分支处理。
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[产物] 不可读，按缺失处理（将重算该阶段）: %s (%s)", path, exc)
        return None


def read_text(path: Path, default: str = "") -> str:
    """读取纯文本；缺失或不可读时返回 `default`。与 `read_json` 同一约定。"""
    if not path.is_file():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("[产物] 不可读，按缺失处理: %s (%s)", path, exc)
        return default
