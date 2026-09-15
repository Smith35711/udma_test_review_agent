"""日志配置：控制台与文件按同一等级过滤（默认 INFO），格式含当前执行阶段。

- 控制台与文件日志等级一致，默认 INFO，可由 `LOG_LEVEL` 或 `set_log_level` 调整。
  DEBUG 含逐条 LLM 流式心跳与中间状态，默认关闭——它会淹没阶段结论，使人（与
  事后分析日志的模型）难以从日志里读出流程走到哪、结果是什么。
- 每条日志前带 `[阶段]`：阶段名由调度方在执行边界写入 `current_stage`，`-`
  表示该条日志不属于任何阶段（如启动期）。本模块不规定由谁写入——任何在边界处
  `set`、并在 `finally` 里 `reset` 的调度器都可以。用 ContextVar 而非线程局部
  变量，是为了让并发的工作线程各自带上所属阶段：线程池不会自动复制上下文，
  故由提交任务的线程捕获、在每个工作线程内设置。
- 日志文件默认写在 `agent2/output/agent2.log`（`get_logger` 负责创建该目录），
  与控制台同等级；命令行 `--log-file` 经 `set_log_file` 改写。要事后复盘被 INFO
  滤掉的过程细节，需显式把等级调成 DEBUG。
- `current_stage` 存的是**当前处理单元**：扫描阶段填阶段名，模块循环里填模块编号
  （如 `M01`）。并发跑模块时由各工作线程各自设置，故各模块的日志不串台；节点身份
  看日志行里的 `%(name)s`，即 `agent2.nodes.<节点名>`。
"""

from __future__ import annotations

import contextvars
import logging
import os
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parents[2] / "output"
_LOG_FILE = _LOG_DIR / "agent2.log"
_PACKAGE = "agent2"

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(stage)s] %(name)s %(message)s"

current_stage: contextvars.ContextVar[str] = contextvars.ContextVar("current_stage", default="-")
"""当前执行阶段名。`-` 表示尚未进入任何阶段（如启动阶段）。

写入方是调度边界（如包住节点的计时代码），读取方只有本模块的格式化链路。
"""


def _fill_context(record: logging.LogRecord) -> None:
    record.stage = current_stage.get()


class _StageFilter(logging.Filter):
    """把上下文里的当前阶段名注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        _fill_context(record)
        return True


class _ContextFormatter(logging.Formatter):
    """格式化前补齐 `stage` —— 这是元数据缺失的最后一道兜底。

    只在 handler 上挂过滤器是不够的：任何拿到本项目的格式化串、却没把过滤器一起
    复制过去的 handler（自建 handler、测试里重建 FileHandler 等）都会在格式化时抛
    `KeyError: 'stage'`。logging 会吞掉该异常并把**整条日志丢弃**，只在 stderr 留
    一段 traceback——也就是说，故障表现为「日志凭空少了几行」，而不是「格式配错
    了」，极难联想到根因。
    """

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "stage"):
            _fill_context(record)
        return super().format(record)


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


def _agent_loggers() -> list[logging.Logger]:
    """包内已创建的全部 logger，含包自身的根 logger。"""
    loggers = [logging.getLogger(_PACKAGE)]
    loggers += [
        obj
        for name, obj in logging.Logger.manager.loggerDict.items()
        if isinstance(obj, logging.Logger) and name.startswith(f"{_PACKAGE}.")
    ]
    return loggers


def set_log_level(level: str | int) -> int:
    """设置控制台与文件日志等级，作用于包内全部 logger。"""
    resolved = resolve_level(level)
    os.environ["LOG_LEVEL"] = logging.getLevelName(resolved)
    for logger in _agent_loggers():
        logger.setLevel(logging.DEBUG)
        _apply_levels(logger, resolved)
    return resolved


def _file_handler(level: int) -> logging.FileHandler:
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(_ContextFormatter(_LOG_FORMAT))
    handler.addFilter(_StageFilter())
    return handler


def set_log_file(path: str | os.PathLike[str]) -> None:
    """改写日志文件路径，供命令行 `--log-file` 使用。

    对已存在的文件处理器一并迁移：各模块在导入时就创建 logger 并绑定处理器，
    命令行解析参数晚于此，若只改常量，先导入的模块会把日志写进旧路径。
    """
    global _LOG_FILE
    _LOG_FILE = Path(path).resolve()
    for logger in _agent_loggers():
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                level = handler.level
                logger.removeHandler(handler)
                handler.close()
                logger.addHandler(_file_handler(level))


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        level = resolve_level(os.getenv("LOG_LEVEL"))
        logger.setLevel(logging.DEBUG)
        formatter = _ContextFormatter(_LOG_FORMAT)
        stage_filter = _StageFilter()
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        console.addFilter(stage_filter)
        logger.addHandler(console)
        logger.addHandler(_file_handler(level))
    return logger
