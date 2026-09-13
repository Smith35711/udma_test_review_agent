"""日志配置：控制台与文件按同一等级过滤（默认 INFO），格式含当前执行节点。

- 控制台与文件日志等级一致，默认 INFO，可由 `LOG_LEVEL` 或 `set_log_level` 调整。
  DEBUG 含逐条 LLM 流式心跳与中间状态，默认关闭——它会淹没阶段结论，使人（与
  事后分析日志的模型）难以从日志里读出流程走到哪、结果是什么。
- 每条日志前带 `[节点名]`：节点名由 `stats.timed_stage` 在图执行边界写入
  `current_stage` 上下文变量，日志过滤器注入到记录里。并发执行的模块循环线程
  由 `ThreadPoolExecutor` 复制父上下文，故同样带上所属节点。
"""

from __future__ import annotations

import contextvars
import logging
import os
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parents[2] / "output"
_LOG_FILE = _LOG_DIR / "llm_test.log"
_PACKAGE = "udma_test_review_agent"

current_stage: contextvars.ContextVar[str] = contextvars.ContextVar("current_stage", default="-")
"""当前执行节点名。`-` 表示尚未进入任何节点（如启动阶段）。"""


class _StageFilter(logging.Filter):
    """把上下文里的当前节点名注入日志记录，供格式化串 `%(stage)s` 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.stage = current_stage.get()
        return True


def resolve_level(level: str | int | None) -> int:
    """将等级名/数值解析为 logging 级别；无法识别时回退 INFO。"""
    if isinstance(level, int):
        return level
    if not level:
        return logging.INFO
    value = getattr(logging, str(level).upper(), None)
    return value if isinstance(value, int) else logging.INFO


def _apply_levels(logger: logging.Logger, level: int) -> None:
    """把控制台与文件处理器统一设为 `level`，保持两边一致。"""
    for handler in logger.handlers:
        handler.setLevel(level)


def set_log_level(level: str | int) -> int:
    """设置控制台与文件日志等级，作用于包内全部 logger。"""
    resolved = resolve_level(level)
    os.environ["LOG_LEVEL"] = logging.getLevelName(resolved)
    loggers = [logging.getLogger(_PACKAGE)]
    loggers += [
        obj
        for name, obj in logging.Logger.manager.loggerDict.items()
        if isinstance(obj, logging.Logger) and name.startswith(f"{_PACKAGE}.")
    ]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        _apply_levels(logger, resolved)
    return resolved


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        level = resolve_level(os.getenv("LOG_LEVEL"))
        logger.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(stage)s] %(name)s %(message)s"
        )
        stage_filter = _StageFilter()
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        console.addFilter(stage_filter)
        file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(stage_filter)
        logger.addHandler(console)
        logger.addHandler(file_handler)
    return logger
