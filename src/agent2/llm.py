"""模型调用：流式接收、双重超时、心跳、结构化输出与采样参数。

对外只有一个入口 `stream_llm_once`——内部流式收流，外部一次性返回。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from typing import Any

import openai
from openai import OpenAI
from pydantic import BaseModel

from .log import get_logger
from .model_config import (
    ModelSpec,
    network_overridden,
    resolve_api_key,
    resolve_model,
)

logger = get_logger(__name__)

# ---------------- LLM 流式调用 ----------------

LLM_FIRST_TOKEN_TIMEOUT = 60.0
LLM_CHUNK_TIMEOUT = 30.0
HEARTBEAT_INTERVAL = 5.0
POLL_INTERVAL = 0.5
PROMPT_DIGEST_LEN = 60
BAR_WIDTH = 30
CLIENT_UA = "agent2/0.1"
_SESSION_ID = uuid.uuid4().hex


def _build_http_client(spec: ModelSpec) -> Any | None:
    """按配置构造底层传输；**没有改动任何默认行为时返回 `None`**。

    返回 `None` 表示「不干预」，调用方不传 `http_client`，于是 SDK 用它自带的
    `DefaultHttpxClient`——这正是引入网络配置之前的行为，逐字节不变。只有配置真的
    改动了出口（代理 / 直连名单 / 证书 / 建连超时 / 不读环境变量）时才另建客户端。

    传输类型必须取 `openai.DefaultHttpxClient`：它是 SDK 自己用的那个类，自带
    timeout / limits / follow_redirects 默认值。手写一个 `httpx.Client` 会把这些
    默认值一起换掉，属于没人要求过的隐性行为变更。
    """
    if not network_overridden(spec):
        return None
    kwargs: dict[str, Any] = {
        # trust_env 显式落定：本字段一处生效，就不再让环境变量在背后另做一套决定
        "trust_env": spec.proxy_from_env,
    }
    if spec.proxy:
        kwargs["proxy"] = spec.proxy
    if spec.no_proxy:
        # None 传输 = 该主机不走代理。httpx 无独立 no_proxy 参数，直连名单只能这样表达
        kwargs["mounts"] = {
            f"all://{host.strip()}": None for host in spec.no_proxy.split(",") if host.strip()
        }
    if spec.ca_bundle:
        kwargs["verify"] = spec.ca_bundle
    elif not spec.verify_ssl:
        kwargs["verify"] = False
    if spec.connect_timeout is not None:
        # 只覆盖 connect，其余沿用 SDK 默认（read/write/pool 600s）
        base = openai.DEFAULT_TIMEOUT
        kwargs["timeout"] = openai.Timeout(
            connect=spec.connect_timeout, read=base.read, write=base.write, pool=base.pool
        )
    logger.debug("[网络] 按配置构造传输: %s", {k: v for k, v in kwargs.items() if k != "mounts"})
    return openai.DefaultHttpxClient(**kwargs)


def _build_openai_client(spec: ModelSpec, base_url: str, api_key: str) -> OpenAI:
    headers = dict(spec.extra_headers)
    if "opencode.ai" in base_url:
        headers.setdefault("x-opencode-session", _SESSION_ID)
    headers.setdefault("User-Agent", CLIENT_UA)
    http_client = _build_http_client(spec)
    if http_client is None:
        return OpenAI(api_key=api_key, base_url=base_url, default_headers=headers)
    return OpenAI(
        api_key=api_key, base_url=base_url, default_headers=headers, http_client=http_client
    )


_PROVIDERS = {"openai": _build_openai_client}


def _build_client(spec: ModelSpec, base_url: str | None = None) -> OpenAI:
    builder = _PROVIDERS.get(spec.type)
    if builder is None:
        raise ValueError(f"不支持的模型类型: {spec.type}，可选: {sorted(_PROVIDERS)}")
    api_key = resolve_api_key(spec)
    if not api_key:
        raise ValueError(
            f"模型 {spec.name} 缺少 API Key：请在配置文件的 api_key 填写字面量或 ${{环境变量名}}"
        )
    resolved_base_url = base_url or spec.base_url
    logger.debug(
        "[客户端] name=%s type=%s base_url=%s 密钥=%s 附加头=%s",
        spec.name,
        spec.type,
        resolved_base_url,
        "已配置",
        list(spec.extra_headers),
    )
    return builder(spec, resolved_base_url, api_key)


def _prompt_digest(text: str) -> str:
    if len(text) <= PROMPT_DIGEST_LEN:
        return text
    return f"{text[:PROMPT_DIGEST_LEN]}...({len(text)}字)"


def _apply_json_constraint(
    prompt: str, schema: type[BaseModel], mode: str = "json_object"
) -> tuple[str, dict | None]:
    """将 schema 描述注入 prompt，并按 `mode` 返回响应约束（`None` = 不发送该参数）。

    schema 描述**任何模式下都会注入提示词**：`response_format` 只是额外的硬约束，
    去掉它不等于模型就知道要输出什么结构。`prompt_only` 正是「只有提示词」这一档。

    schema 目前原样注入。将来若引入「由流水线填写、模型无从得知」的字段（锚点、
    指纹、来源批次、校验状态之类），必须在这里把它们从注入的 schema 中剔除：
    否则模型会被要求编造它们——要么产出无意义的占位值，要么把注意力从真正的
    判断上引开。
    """
    schema_json = schema.model_json_schema()
    schema_desc = json.dumps(schema_json, ensure_ascii=False)
    constrained_prompt = (
        f"{prompt}\n\n要求：仅输出一个 JSON 对象，不要输出任何其他文本，"
        f"结构须符合以下 JSON Schema：\n{schema_desc}"
    )
    if mode == "prompt_only":
        return constrained_prompt, None
    if mode == "json_schema":
        return constrained_prompt, {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema_json, "strict": False},
        }
    return constrained_prompt, {"type": "json_object"}


_STRUCTURED_REJECTION_MARKERS = (
    "response_format",
    "json_object",
    "json_schema",
    "structured output",
    "structured_output",
)
"""端点拒绝结构化输出时，错误文本里通常出现的字样。

只有当错误**同时**是 4xx 且提到这些字样时才降级——否则任何一次 400 都会被误判成
「不支持结构化输出」，把一个真正的 schema 错误掩盖成静默降级。
"""


class StreamCallError(RuntimeError):
    """流式调用在工作线程里失败后，跨线程带回主线程的异常。

    **为什么不能裸抛原始异常、也不能只包成一个空壳的 RuntimeError**：
    两个下游判据各依赖一边。调用方的重试元组通常收 `RuntimeError`——包成别的
    类型会让所有 API 异常不再重试；而结构化输出降级判据要读底层异常的 `status_code`
    ——丢了它，降级永远不会触发。故这里同时保住两者：仍是 RuntimeError，
    并把底层异常的类型名、文本与状态码一并带出。
    """

    def __init__(self, cause: BaseException):
        super().__init__(f"流式调用异常: {type(cause).__name__}: {cause}")
        self.cause = cause
        self.status_code = getattr(cause, "status_code", None)


def _looks_like_structured_rejection(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None or not (400 <= int(status) < 500):
        return False
    text = f"{exc}".lower()
    return any(marker in text for marker in _STRUCTURED_REJECTION_MARKERS)


_CODE_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n?")
_TRAILING_FENCE_RE = re.compile(r"\n?\s*```\s*$")
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _extract_json(text: str) -> dict:
    """从模型输出文本中提取 JSON 对象。

    逐级放宽，按「实测见过的回复形态」排列：裸 JSON → 代码围栏 → 前后夹带说明
    文字 → 尾随逗号。每一级都只在上一级失败后才尝试，故正常输出不会走后面的弯路。
    """
    cleaned = text.strip().lstrip("﻿")  # BOM：某些网关会带上，直接 json.loads 会失败
    attempts: list[tuple[str, str]] = [("原样", cleaned)]

    fenced = _TRAILING_FENCE_RE.sub("", _CODE_FENCE_RE.sub("", cleaned))
    if fenced != cleaned:
        attempts.append(("去围栏", fenced.strip()))

    for label, candidate in list(attempts):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            attempts.append((f"{label}+截取", candidate[start : end + 1]))

    last_error: json.JSONDecodeError | None = None
    for label, candidate in attempts:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
    # 尾随逗号是最后一道：它已经不是合法 JSON，单独走一遍替换后重解析
    for label, candidate in attempts:
        repaired = _TRAILING_COMMA_RE.sub(r"\1", candidate)
        if repaired == candidate:
            continue
        try:
            logger.debug("[JSON提取] 容忍尾随逗号后解析成功（%s）", label)
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            last_error = exc
    logger.debug("[JSON提取] 各级放宽均失败 chars=%d 末次错误=%s", len(cleaned), last_error)
    raise last_error if last_error is not None else json.JSONDecodeError("无候选", cleaned, 0)


def _extract_delta(chunk: object) -> tuple[str, str]:
    """从流式 chunk 中取出 (正文增量, 思维链增量)。"""
    content = ""
    reasoning = ""
    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if not delta:
            continue
        content += delta.content or ""
        reasoning += getattr(delta, "reasoning_content", None) or ""
    return content, reasoning


def _extract_finish_reason(chunk: object) -> str:
    """从流式 chunk 中取出 finish_reason；本 chunk 没有则返回空串。

    这是唯一能区分「模型自然说完」与「被 max_tokens 截断」的信号：截断时正文
    是半截的，结构化输出会表现为 JSON 解析失败，若不记录就只能靠猜。
    """
    for choice in getattr(chunk, "choices", None) or []:
        value = getattr(choice, "finish_reason", None)
        if value:
            return str(value)
    return ""


def _as_int(value: object) -> int | None:
    """把端点给的计数转成 int；None 或非数字一律返回 None（按「端点未提供」处理）。"""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _usage_detail(usage: object) -> dict[str, int]:
    """取端点自行扩展的用量明细；端点没提供的不放进结果。

    **缺失与 0 不是一回事**：取不到时日志显示 `-`（该端点不提供这一项），而不是 0
    ——0 会被读成「确实一个思维链 token 都没花」「确实没有缓存命中」。

    字段名在各端点间并不统一，同一家的不同模型给出的子结构也可能不同（实测
    glm-5.3-flash 连 `completion_tokens_details` 整个子对象都没有）。按已知路径逐项取：
    - 思维链 token：`completion_tokens_details.reasoning_tokens`
    - prompt 缓存命中：DeepSeek 放在顶层 `prompt_cache_hit_tokens`，
      GLM 放在 `prompt_tokens_details.cached_tokens`
    """
    detail: dict[str, int] = {}
    if usage is None:
        return detail
    reasoning = _as_int(
        getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    )
    if reasoning is not None:
        detail["reasoning_tokens"] = reasoning
    cached = _as_int(
        getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
    )
    if cached is None:
        cached = _as_int(getattr(usage, "prompt_cache_hit_tokens", None))
    if cached is not None:
        detail["cached_tokens"] = cached
    return detail


def _distribution_bar(reasoning_len: int, content_len: int, width: int = BAR_WIDTH) -> str:
    """按思维链/正文字符占比生成分布条，'█' 为思维链、'░' 为正文。"""
    total = reasoning_len + content_len
    reasoning_blocks = round(width * reasoning_len / total) if total else 0
    return "█" * reasoning_blocks + "░" * (width - reasoning_blocks)


def _sampling_params(
    spec: ModelSpec, temperature: float | None, seed: int | None
) -> dict[str, object]:
    """组装请求的采样参数，逐字段按「显式入参 > 模型条目」取值。

    两个字段的**取值方向一致**（`None` = 该层未指定，向下取条目），但落到请求上的
    表现不同：temperature 总有值（条目有默认 0.7），seed 则可能在两层都未指定，
    此时**不发送该参数**，而不是发一个 0——0 是一个合法的种子，与「不固定」不是一回事。
    抽成独立函数是为了让这层优先级可被直接断言，而不必真的发一次请求。
    """
    params: dict[str, object] = {
        "temperature": spec.temperature if temperature is None else temperature
    }
    effective_seed = spec.seed if seed is None else seed
    if effective_seed is not None:
        # seed 只降低采样随机性，不保证输出确定（服务端实现与并发都可能引入差异），
        # 因此它是「方差的一个来源」而非「可复现性保证」。
        params["seed"] = effective_seed
    return params


def stream_llm_once(
    prompt: str,
    schema: type[BaseModel] | None = None,
    *,
    profile: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    first_token_timeout: float = LLM_FIRST_TOKEN_TIMEOUT,
    chunk_timeout: float = LLM_CHUNK_TIMEOUT,
    include_reasoning: bool = False,
    temperature: float | None = None,
    seed: int | None = None,
) -> str | tuple[str, str] | BaseModel:
    """唯一 LLM 对外接口：内部流式接收，外部一次性返回。

    默认接入 DeepSeek（deepseek-reasoner，支持思维链 reasoning_content）；
    可通过 model / base_url 覆盖为其他 OpenAI 兼容端点（如 opencode-go）。
    超时规则：首 token 等待超过 first_token_timeout，或相邻 chunk 间隔超过
    chunk_timeout 时抛出 TimeoutError；每收到一个 chunk 计时即重置。
    schema 为 None 时返回正文 str（include_reasoning 为 True 时返回
    (思维链, 正文) 二元组）；
    schema 为 Pydantic 模型时启用 JSON 结构化输出，返回校验后的实例。
    profile 指定模型配置中的 name；为空时依 LLM_PROFILE 环境变量或配置 active。
    temperature / seed 为 None 时取模型条目的同名字段（见 `_sampling_params`）；
    两者都为 None 的 seed 不发送该参数——不发 ≠ 发 0。

    本函数另行负责结构化输出的降级重试：端点以 4xx 拒绝 `response_format` 时，
    自动改用纯提示词约束重试一次并告警；其余异常原样抛出。真正的流式实现
    在 `_stream_once`。
    """
    try:
        return _stream_once(
            prompt,
            schema,
            profile=profile,
            model=model,
            base_url=base_url,
            first_token_timeout=first_token_timeout,
            chunk_timeout=chunk_timeout,
            include_reasoning=include_reasoning,
            temperature=temperature,
            seed=seed,
        )
    except BaseException as exc:  # noqa: BLE001 - 捕获只为识别结构化输出拒绝，其余原样抛出
        # 端点不接受 response_format 时降级重试一次：这类拒绝与模型本身无关，
        # 若直接抛出，整个流程会在每一步都失败——而它本可以只靠提示词约束跑完。
        if schema is None or not _looks_like_structured_rejection(exc):
            raise
        logger.warning(
            "[结构化输出] 端点拒绝 response_format，降级为纯提示词约束重试一次: %s。"
            "要免掉这次多余的重试，请在模型条目下配置 structured_output: prompt_only",
            exc,
        )
        return _stream_once(
            prompt,
            schema,
            profile=profile,
            model=model,
            base_url=base_url,
            first_token_timeout=first_token_timeout,
            chunk_timeout=chunk_timeout,
            include_reasoning=include_reasoning,
            temperature=temperature,
            seed=seed,
            prompt_only=True,
        )


def _stream_once(
    prompt: str,
    schema: type[BaseModel] | None,
    *,
    profile: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    first_token_timeout: float = LLM_FIRST_TOKEN_TIMEOUT,
    chunk_timeout: float = LLM_CHUNK_TIMEOUT,
    include_reasoning: bool = False,
    temperature: float | None = None,
    seed: int | None = None,
    prompt_only: bool = False,
) -> str | tuple[str, str] | BaseModel:
    """流式接收的实现本体。

    `prompt_only` 供降级重试使用，压过模型条目的 `structured_output` 取值。
    """
    spec = resolve_model(profile)
    model = model or spec.model
    base_url = base_url or spec.base_url

    response_format = None
    if schema is not None:
        mode = "prompt_only" if prompt_only else spec.structured_output
        prompt, response_format = _apply_json_constraint(prompt, schema, mode)
    logger.debug(
        "[调用解析] profile=%s model=%s base_url=%s schema=%s response_format=%s include_reasoning=%s prompt_chars=%d",
        profile or "(active)",
        model,
        base_url,
        schema.__name__ if schema else None,
        response_format.get("type") if response_format else None,
        include_reasoning,
        len(prompt),
    )
    logger.info(
        "发起调用 model=%s schema=%s prompt_digest=%r prompt_chars=%d "
        "first_token_timeout=%.0fs chunk_timeout=%.0fs",
        model,
        schema.__name__ if schema else None,
        _prompt_digest(prompt),
        len(prompt),
        first_token_timeout,
        chunk_timeout,
    )

    client = _build_client(spec, base_url=base_url)
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    # 采样参数按「显式入参 > 模型条目」组装后随请求发出；两层都未指定的 seed
    # 不发送该参数（见 `_sampling_params`）。
    sampling = _sampling_params(spec, temperature, seed)

    def _worker() -> None:
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                stream_options={"include_usage": True},
                **({"response_format": response_format} if response_format else {}),
                **sampling,
            )
            with stream:
                for chunk in stream:
                    events.put(("chunk", chunk))
        except Exception as exc:  # noqa: BLE001 - 异常透传至主线程
            logger.error("[流式异常] %s: %s", type(exc).__name__, exc)
            events.put(("error", exc))
        finally:
            events.put(("done", None))

    threading.Thread(target=_worker, daemon=True).start()

    start = time.monotonic()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    content_ts: float | None = None
    reasoning_ts: float | None = None
    usage = None
    finish_reason = ""
    chunk_count = 0
    last_activity = start
    last_heartbeat = start

    def _phase() -> str:
        if content_ts is not None:
            return "正文输出"
        if reasoning_ts is not None:
            return "思维链"
        return "首token等待"

    def _emit_heartbeat(now: float) -> None:
        nonlocal last_heartbeat
        logger.debug(
            "[心跳] 已接收 chunk=%d 思维链=%d字 正文=%d字，阶段=%s",
            chunk_count,
            sum(map(len, reasoning_parts)),
            sum(map(len, content_parts)),
            _phase(),
        )
        last_heartbeat = now

    while True:
        try:
            kind, payload = events.get(timeout=POLL_INTERVAL)
        except queue.Empty:
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                _emit_heartbeat(now)
            allowed = (
                first_token_timeout
                if content_ts is None and reasoning_ts is None
                else chunk_timeout
            )
            idle = now - last_activity
            if idle >= allowed:
                stage = (
                    "首token等待"
                    if content_ts is None and reasoning_ts is None
                    else "chunk间隔"
                )
                logger.error("[超时] %s超过%.0fs，已接收正文%d字", stage, allowed, sum(map(len, content_parts)))
                raise TimeoutError(f"{stage}超过 {allowed:.0f}s")
            continue

        if kind == "error":
            raise StreamCallError(payload) from payload  # type: ignore[arg-type]
        if kind == "done":
            break

        now = time.monotonic()
        chunk_count += 1
        # 只有最后一个 chunk 带 finish_reason，故逐个 chunk 取、保留最后一次非空值
        finish_reason = _extract_finish_reason(payload) or finish_reason
        delta_content, delta_reasoning = _extract_delta(payload)
        if delta_reasoning and reasoning_ts is None:
            reasoning_ts = now
            logger.info("[首token][思维链] 耗时 %.3fs", reasoning_ts - start)
        if delta_content and content_ts is None:
            content_ts = now
            logger.info("[首token][正文] 耗时 %.3fs", content_ts - start)
        last_activity = now
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            _emit_heartbeat(now)
        if delta_reasoning:
            reasoning_parts.append(delta_reasoning)
        if delta_content:
            content_parts.append(delta_content)
        if usage is None:
            usage = getattr(payload, "usage", None)

    text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    elapsed = time.monotonic() - start
    total = len(reasoning_text) + len(text)
    reasoning_pct = round(len(reasoning_text) * 100 / total) if total else 0
    # 用量与 finish 在 schema 校验之前就写进日志：校验失败会抛异常触发重试，而那一次
    # 调用消耗的 token 是实实在在花掉的，不该因为「这一版 JSON 不合法」就查不到。
    # 完成记录拆成三行，正对应端点返回的三块：流式过程指标 / usage / 两个内容通道。
    logger.info(
        "[完成] 总耗时 %.3fs [%s] 思维链=%d字(%d%%) 正文=%d字(%d%%) finish=%s chunk=%d",
        elapsed,
        _distribution_bar(len(reasoning_text), len(text)),
        len(reasoning_text),
        reasoning_pct,
        len(text),
        100 - reasoning_pct,
        finish_reason or "-",
        chunk_count,
    )
    if usage is None:
        logger.info("[用量] 端点未返回 usage，本次 token 用量不可知")
    else:
        detail = _usage_detail(usage)
        # 缺项时才点名，避免每次调用都挂一长串解释
        missing = [name for name in ("reasoning_tokens", "cached_tokens") if name not in detail]
        logger.info(
            "[用量] prompt_tokens=%d completion_tokens=%d total_tokens=%d "
            "reasoning_tokens=%s cached_tokens=%s%s",
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            detail.get("reasoning_tokens", "-"),
            detail.get("cached_tokens", "-"),
            f"（{'、'.join(missing)} 端点未提供）" if missing else "",
        )
    logger.info(
        "[输出摘要] 正文=%r%s",
        _prompt_digest(text),
        f" 思维链={_prompt_digest(reasoning_text)!r}" if reasoning_text else "",
    )

    if schema is not None:
        return _validate_schema_output(text, schema)
    if include_reasoning:
        return reasoning_text, text
    return text


def _validate_schema_output(text: str, schema: type[BaseModel]) -> BaseModel:
    logger.debug("[JSON原文] schema=%s chars=%d 截断=%r", schema.__name__, len(text), text[:2000])
    try:
        result = schema.model_validate(_extract_json(text))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("[JSON解析失败] schema=%s 原文=%r 错误=%s", schema.__name__, _prompt_digest(text), exc)
        raise
    fields = {k: _prompt_digest(str(v)) for k, v in result.model_dump().items()}
    logger.info("[JSON解析成功] schema=%s 字段=%s", schema.__name__, fields)
    return result
