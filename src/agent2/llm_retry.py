"""带重试的模型调用：大模块分支专用。

**为什么大模块需要它**：一次调用要处理二十来条意见时，模型的思考时间会到分钟级，
中途出现超过 chunk 间隔阈值的静默是常态而非异常——实测一次 137 秒的调用里，
末段就有 30 秒以上的静默。而大模块一轮要跑十几次调用，任何一次失败都会让整模块
拿不到报告（报告节点在最后，前面所有结果一起作废）。守在这里比守在出口划算。

**小模块路径不接这一层**：它的调用规模小、已实测稳定，不为没出现过的问题加分支。
这也是本模块单独存在、而不是改造 `stream_llm_once` 的原因。

采样参数、结构化输出、超时与心跳全部沿用 `stream_llm_once`，这里只负责重试：
超时、连接类异常、以及 schema 解析失败都算可重试——它们都可能是单次抖动。
"""

from __future__ import annotations

import time

from .llm import StreamCallError, stream_llm_once
from .log import get_logger

logger = get_logger(__name__)

DEFAULT_RETRIES = 2

PROBE_CHUNK_TIMEOUT = 90.0
"""探测反驳的 chunk 间隔上限。比默认的 30s 宽——那一条意见一次、思考时间本来就长，
30s 的静默在长输入下是正常的思考间隔，不是断流。"""

_RETRY_BACKOFF = 3.0


def call_llm(config, prompt: str, schema=None, *, retries: int = DEFAULT_RETRIES, chunk_timeout: float | None = None):
    """调用模型，失败重试；重试仍失败则抛出最后一次的异常。

    `config` 传 `RunConfig`：模型、超时等选项从它取，保证与大模块其余环节同源。
    """
    options = dict(config.llm_options())
    if chunk_timeout is not None:
        options["chunk_timeout"] = chunk_timeout

    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            return stream_llm_once(prompt, schema, **options)
        except (TimeoutError, StreamCallError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "模型调用失败（第 %d/%d 次）: %s: %s；%s",
                attempt, retries + 1, type(exc).__name__, str(exc)[:180],
                "重试" if attempt <= retries else "放弃",
            )
            if attempt <= retries:
                time.sleep(_RETRY_BACKOFF * attempt)
    raise last_error  # type: ignore[misc]
