"""LLM 节点通用调用封装：带重试的结构化调用与按角色的模型路由。"""

from __future__ import annotations

import json
import time

from ...configuration import ReviewConfig
from ...llm import stream_llm_once
from ...log import get_logger
from ...model_config import ModelConfigError, resolve_role

logger = get_logger(__name__)

_ROLE_WARNED: set[str] = set()
"""已就「角色路由解析失败」告警过的角色。配置损坏时每个模块都会调一次 LLM，
不去重的话同一条告警会刷屏，真正的错误反而被埋掉。"""


def _profile_for(cfg: ReviewConfig, role: str) -> str:
    """解析本次调用用哪个模型条目：`--model` > 角色路由 > `LLM_PROFILE` > `active`。

    返回空串表示「没有显式指定」，交给 `stream_llm_once` 走环境变量与 active。
    角色路由压过 `LLM_PROFILE`：后者是全局默认项，而角色是配置文件里逐阶段的显式
    指定；让一个全局默认无声作废整套路由，是「配置写了没反应」的又一变体。
    `--model` 压过一切——它是命令行上最强的即时意图。
    """
    if cfg.model_profile:
        return cfg.model_profile
    if not role:
        return ""
    try:
        return resolve_role(role)
    except ModelConfigError as exc:
        if role not in _ROLE_WARNED:
            _ROLE_WARNED.add(role)
            logger.warning("[模型路由] 角色 %s 解析失败，回退默认模型: %s", role, exc)
        return ""


def _llm(prompt: str, schema: type, cfg: ReviewConfig, retries: int = 2, role: str = ""):
    """调用 LLM。`role` 用于模型路由与过程统计分组，取值见 `model_config.ROLES`。"""
    last_exc: Exception | None = None
    schema_name = getattr(schema, "__name__", str(schema))
    total_attempts = retries + 1
    profile = _profile_for(cfg, role)
    for attempt in range(1, retries + 2):
        logger.debug(
            "[LLM请求] schema=%s attempt=%d/%d prompt_chars=%d role=%s profile=%s 首token超时=%.0fs chunk超时=%.0fs "
            "temperature=%s seed=%s",
            schema_name,
            attempt,
            total_attempts,
            len(prompt),
            role or "-",
            profile or "(active)",
            cfg.first_token_timeout,
            cfg.chunk_timeout,
            "条目默认" if cfg.temperature is None else cfg.temperature,
            "条目默认" if cfg.seed is None else cfg.seed,
        )
        started = time.monotonic()
        try:
            result = stream_llm_once(
                prompt,
                schema,
                profile=profile or None,
                first_token_timeout=cfg.first_token_timeout,
                chunk_timeout=cfg.chunk_timeout,
                temperature=cfg.temperature,
                seed=cfg.seed,
                role=role,
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
