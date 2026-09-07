"""日志配置：控制台 + 文件双输出，级别由 LOG_LEVEL 环境变量控制（默认 INFO）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parents[2] / "output"
_LOG_FILE = _LOG_DIR / "llm_test.log"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        for handler in (
            logging.StreamHandler(),
            logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        ):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
    return logger
