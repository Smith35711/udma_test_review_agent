"""LLM 节点通用调用封装：带重试的结构化调用。"""

from __future__ import annotations

import json
import time

from ...configuration import ReviewConfig
from ...llm import stream_llm_once
from ...log import get_logger

logger = get_logger(__name__)


def _llm(prompt: str, schema: type, cfg: ReviewConfig, retries: int = 2):
    last_exc: Exception | None = None
    schema_name = getattr(schema, "__name__", str(schema))
    total_attempts = retries + 1
    for attempt in range(1, retries + 2):
        logger.debug(
            "[LLM请求] schema=%s attempt=%d/%d prompt_chars=%d profile=%s 首token超时=%.0fs chunk超时=%.0fs",
            schema_name,
            attempt,
            total_attempts,
            len(prompt),
            cfg.model_profile or "(active)",
            cfg.first_token_timeout,
            cfg.chunk_timeout,
        )
        started = time.monotonic()
        try:
            result = stream_llm_once(
                prompt,
                schema,
                profile=cfg.model_profile or None,
                first_token_timeout=cfg.first_token_timeout,
                chunk_timeout=cfg.chunk_timeout,
            )
            logger.debug("[LLM响应] schema=%s attempt=%d 耗时=%.2fs", schema_name, attempt, time.monotonic() - started)
            return result
        except (json.JSONDecodeError, ValueError, RuntimeError, TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "[LLM] 调用失败(第%d次): %s: %s；%s",
                attempt,
                type(exc).__name__,
                str(exc)[:200],
                "重试" if attempt <= retries else "放弃",
            )
            time.sleep(2)
    raise last_exc  # type: ignore[misc]
