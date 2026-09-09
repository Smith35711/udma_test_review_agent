"""日志配置：控制台按等级过滤，文件始终记录 DEBUG；等级由 LOG_LEVEL 或 set_log_level 控制。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parents[2] / "output"
_LOG_FILE = _LOG_DIR / "llm_test.log"
_PACKAGE = "udma_test_review_agent"


def resolve_level(level: str | int | None) -> int:
    """将等级名/数值解析为 logging 级别；无法识别时回退 INFO。"""
    if isinstance(level, int):
        return level
    if not level:
        return logging.INFO
    value = getattr(logging, str(level).upper(), None)
    return value if isinstance(value, int) else logging.INFO


def _apply_console_level(logger: logging.Logger, level: int) -> None:
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.DEBUG)
        else:
            handler.setLevel(level)


def set_log_level(level: str | int) -> int:
    """设置控制台日志等级（文件始终记录 DEBUG），作用于包内全部 logger。"""
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
        _apply_console_level(logger, resolved)
    return resolved


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        console = logging.StreamHandler()
        console.setLevel(resolve_level(os.getenv("LOG_LEVEL")))
        console.setFormatter(formatter)
        file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(console)
        logger.addHandler(file_handler)
    return logger
