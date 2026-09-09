from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import OpenAI
from pydantic import BaseModel, Field

from .log import get_logger
from .model_config import ModelSpec, resolve_model

logger = get_logger(__name__)

_OPENCODE_CONFIG_DEFAULT = Path.home() / ".config" / "opencode" / "opencode.json"


def load_deepseek_config(config_path: str | os.PathLike | None = None) -> dict[str, str]:
    path = Path(config_path or os.getenv("OPENCODE_CONFIG", _OPENCODE_CONFIG_DEFAULT))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    try:
        options = data["provider"]["deepseek"]["options"]
    except (KeyError, TypeError):
        return {}
    return {key: options[key] for key in ("apiKey", "baseURL") if options.get(key)}


def _resolve_api_key(spec: ModelSpec) -> str:
    if spec.api_key:
        return spec.api_key
    cfg = load_deepseek_config()
    return os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY") or cfg.get("apiKey", "")


def create_llm(
    profile: str | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    timeout: float | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> BaseChatModel:
    spec = resolve_model(profile)
    resolved_api_key = api_key or _resolve_api_key(spec)
    if not resolved_api_key:
        raise ValueError(
            f"模型 {spec.name} 缺少 API Key：请在配置文件的 api_key 填写字面量或 ${{环境变量名}}"
        )

    return ChatOpenAI(
        model=model or spec.model,
        temperature=temperature if temperature is not None else spec.temperature,
        max_retries=max_retries if max_retries is not None else spec.max_retries,
        timeout=timeout if timeout is not None else spec.timeout,
        base_url=base_url or spec.base_url,
        api_key=resolved_api_key,
    )


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def generate_text(
    llm: BaseChatModel,
    user_text: str,
    system_text: str | None = None,
) -> str:
    messages: list[BaseMessage] = []
    if system_text:
        messages.append(SystemMessage(content=system_text))
    messages.append(HumanMessage(content=user_text))

    chunks: list[str] = []
    for chunk in llm.stream(messages):
        text = _content_to_text(chunk.content)
        if text:
            chunks.append(text)
    return "".join(chunks)


# ---------------- LLM 流式调用 (DeepSeek) ----------------

LLM_FIRST_TOKEN_TIMEOUT = 60.0
LLM_CHUNK_TIMEOUT = 30.0
HEARTBEAT_INTERVAL = 5.0
POLL_INTERVAL = 0.5
PROMPT_DIGEST_LEN = 60
BAR_WIDTH = 30
CLIENT_UA = "udma-test-review-agent/0.1"
_SESSION_ID = uuid.uuid4().hex


def _build_openai_client(spec: ModelSpec, base_url: str, api_key: str) -> OpenAI:
    headers = dict(spec.extra_headers)
    if "opencode.ai" in base_url:
        headers.setdefault("x-opencode-session", _SESSION_ID)
    headers.setdefault("User-Agent", CLIENT_UA)
    return OpenAI(api_key=api_key, base_url=base_url, default_headers=headers)


_PROVIDERS = {"openai": _build_openai_client}


def _build_client(spec: ModelSpec, base_url: str | None = None) -> OpenAI:
    builder = _PROVIDERS.get(spec.type)
    if builder is None:
        raise ValueError(f"不支持的模型类型: {spec.type}，可选: {sorted(_PROVIDERS)}")
    api_key = _resolve_api_key(spec)
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


def _apply_json_constraint(prompt: str, schema: type[BaseModel]) -> tuple[str, dict]:
    """将 schema 描述注入 prompt，并返回 json_object 响应约束。"""
    schema_desc = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    constrained_prompt = (
        f"{prompt}\n\n要求：仅输出一个 JSON 对象，不要输出任何其他文本，"
        f"结构须符合以下 JSON Schema：\n{schema_desc}"
    )
    return constrained_prompt, {"type": "json_object"}


def _extract_json(text: str) -> dict:
    """从模型输出文本中提取 JSON 对象，容忍代码围栏与前后缀文本。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            logger.debug("[JSON提取] 未找到 JSON 对象 chars=%d", len(cleaned))
            raise
        logger.debug("[JSON提取] 回退截取子串 起=%d 止=%d chars=%d", start, end, end - start + 1)
        return json.loads(cleaned[start : end + 1])


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


def _distribution_bar(reasoning_len: int, content_len: int, width: int = BAR_WIDTH) -> str:
    """按思维链/正文字符占比生成分布条，'█' 为思维链、'░' 为正文。"""
    total = reasoning_len + content_len
    reasoning_blocks = round(width * reasoning_len / total) if total else 0
    return "█" * reasoning_blocks + "░" * (width - reasoning_blocks)


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
    observer: object = None,
) -> str | tuple[str, str] | BaseModel:
    """唯一 LLM 对外接口：内部流式接收，外部一次性返回。

    默认接入 DeepSeek（deepseek-reasoner，支持思维链 reasoning_content）；
    可通过 model / base_url 覆盖为其他 OpenAI 兼容端点（如 opencode-go）。
    超时规则：首 token 等待超过 first_token_timeout，或相邻 chunk 间隔超过
    chunk_timeout 时抛出 TimeoutError；每收到一个 chunk 计时即重置。
    schema 为 None 时返回正文 str（include_reasoning 为 True 时返回
    (思维链, 正文) 二元组）；
    schema 为 Pydantic 模型时启用 JSON 结构化输出，返回校验后的实例。
    observer 为可选回调 (kind, data)，接收 request/chunk/done 事件。
    profile 指定模型配置中的 name；为空时依 LLM_PROFILE 环境变量或配置 active。
    """
    spec = resolve_model(profile)
    model = model or spec.model
    base_url = base_url or spec.base_url

    def _notify(kind: str, data: dict) -> None:
        if observer is not None:
            try:
                observer(kind, data)
            except Exception:  # noqa: BLE001 - 观察者异常不影响主流程
                pass

    response_format = None
    if schema is not None:
        prompt, response_format = _apply_json_constraint(prompt, schema)
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
    _notify("request", {
        "model": model,
        "schema": schema.__name__ if schema else None,
        "prompt_digest": _prompt_digest(prompt),
        "prompt_chars": len(prompt),
        "first_token_timeout": first_token_timeout,
        "chunk_timeout": chunk_timeout,
    })

    client = _build_client(spec, base_url=base_url)
    events: queue.Queue[tuple[str, object]] = queue.Queue()

    def _worker() -> None:
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                stream_options={"include_usage": True},
                **({"response_format": response_format} if response_format else {}),
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
            raise RuntimeError("流式调用异常") from payload  # type: ignore[arg-type]
        if kind == "done":
            break

        now = time.monotonic()
        chunk_count += 1
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
            _notify("chunk", {"phase": "reasoning", "text": delta_reasoning})
        if delta_content:
            content_parts.append(delta_content)
            _notify("chunk", {"phase": "content", "text": delta_content})
        if usage is None:
            usage = getattr(payload, "usage", None)

    text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    elapsed = time.monotonic() - start
    _notify("done", {
        "elapsed": round(elapsed, 3),
        "reasoning_chars": len(reasoning_text),
        "content_chars": len(text),
        "total_tokens": usage.total_tokens if usage is not None else None,
    })
    total = len(reasoning_text) + len(text)
    reasoning_pct = round(len(reasoning_text) * 100 / total) if total else 0
    if usage is not None:
        logger.info(
            "[完成] 总耗时 %.3fs [%s] 思维链=%d字(%d%%) 正文=%d字(%d%%) 输出摘要=%r "
            "prompt_tokens=%d completion_tokens=%d total_tokens=%d",
            elapsed,
            _distribution_bar(len(reasoning_text), len(text)),
            len(reasoning_text),
            reasoning_pct,
            len(text),
            100 - reasoning_pct,
            _prompt_digest(text),
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )
    else:
        logger.info(
            "[完成] 总耗时 %.3fs [%s] 思维链=%d字(%d%%) 正文=%d字(%d%%) 输出摘要=%r（未获取usage）",
            elapsed,
            _distribution_bar(len(reasoning_text), len(text)),
            len(reasoning_text),
            reasoning_pct,
            len(text),
            100 - reasoning_pct,
            _prompt_digest(text),
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


class ReviewVerdict(BaseModel):
    """测试用例评审结论 schema。"""

    score: int = Field(description="评审得分，0 到 100 的整数")
    passed: bool = Field(description="是否通过评审")
    summary: str = Field(description="一句话评审结论")
