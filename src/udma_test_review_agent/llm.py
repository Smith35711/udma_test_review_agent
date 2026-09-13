from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import OpenAI
from pydantic import BaseModel, Field

from .log import get_logger
# load_deepseek_config 一并转出，保持 `llm.load_deepseek_config` 的既有可达性
from .model_config import ModelSpec, load_deepseek_config, resolve_api_key, resolve_model
from .stats import LlmCall, get_stats

logger = get_logger(__name__)


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
    resolved_api_key = api_key or resolve_api_key(spec)
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


def _strip_deterministic(node: object) -> object:
    """从 JSON Schema 中剔除标记为 `deterministic` 的属性。

    这些字段（锚点、指纹、来源批次、校验状态）由流水线填写，模型无从得知。
    若留在注入的 schema 里，模型会被要求编造它们——要么产出无意义的占位值，
    要么把注意力从真正的判断上引开。递归处理 `$defs` 与嵌套对象。
    """
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            dropped = [name for name, spec in properties.items() if isinstance(spec, dict) and spec.get("deterministic")]
            for name in dropped:
                properties.pop(name)
            required = node.get("required")
            if isinstance(required, list):
                node["required"] = [name for name in required if name not in dropped]
        for value in node.values():
            _strip_deterministic(value)
    elif isinstance(node, list):
        for value in node:
            _strip_deterministic(value)
    return node


def _apply_json_constraint(prompt: str, schema: type[BaseModel]) -> tuple[str, dict]:
    """将 schema 描述注入 prompt，并返回 json_object 响应约束。"""
    schema_desc = json.dumps(_strip_deterministic(schema.model_json_schema()), ensure_ascii=False)
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
    temperature: float | None = None,
    seed: int | None = None,
    role: str = "",
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
    temperature 为 None 时取模型条目的 temperature；seed 为 None 时不发送该参数。
    role 只用于**过程统计的分组**（模型路由由调用方解析后经 profile 传入），
    使「哪个阶段花了多少 token」可回答。

    本函数是记账外壳：真正的流式实现在 `_stream_once`。拆开是因为失败也必须记账，
    而失败路径（超时、schema 校验不过）恰恰发生在实现内部深处——把 try 放在
    实现外面，meta 里已经填好的 token 用量才不会随异常一起丢掉。
    """
    meta: dict[str, Any] = {}
    started = time.monotonic()
    try:
        result = _stream_once(
            prompt,
            schema,
            meta,
            profile=profile,
            model=model,
            base_url=base_url,
            first_token_timeout=first_token_timeout,
            chunk_timeout=chunk_timeout,
            include_reasoning=include_reasoning,
            temperature=temperature,
            seed=seed,
        )
    except BaseException as exc:  # noqa: BLE001 - 记账后原样抛出，不改变调用方语义
        _record_call(role, schema, meta, started, exc)
        raise
    _record_call(role, schema, meta, started, None)
    return result


def _record_call(
    role: str,
    schema: type[BaseModel] | None,
    meta: dict[str, Any],
    started: float,
    exc: BaseException | None,
) -> None:
    """把一次调用的过程数据记进运行统计。**本函数不抛异常**：统计是旁路。"""
    try:
        get_stats().record_llm(
            LlmCall(
                role=role,
                spec=str(meta.get("spec", "")),
                model=str(meta.get("model", "")),
                schema=getattr(schema, "__name__", "") or "",
                ok=exc is None,
                elapsed=time.monotonic() - started,
                prompt_chars=int(meta.get("prompt_chars", 0) or 0),
                reasoning_chars=int(meta.get("reasoning_chars", 0) or 0),
                content_chars=int(meta.get("content_chars", 0) or 0),
                prompt_tokens=int(meta.get("prompt_tokens", 0) or 0),
                completion_tokens=int(meta.get("completion_tokens", 0) or 0),
                total_tokens=int(meta.get("total_tokens", 0) or 0),
                has_usage=bool(meta.get("has_usage")),
                error="" if exc is None else f"{type(exc).__name__}: {exc}",
            )
        )
    except Exception as stats_exc:  # noqa: BLE001
        logger.debug("[统计] 记账失败（不影响本次调用）: %s", stats_exc)


def _stream_once(
    prompt: str,
    schema: type[BaseModel] | None,
    meta: dict[str, Any],
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
    """流式接收的实现本体；过程数据写进入参 `meta`（就地填充，不返回值）。

    `meta` 用出参而非返回值，是为了让「流式已结束、schema 校验失败」的调用也能
    留下 token 用量：那种情况会抛异常，但用量已经产生。
    """
    spec = resolve_model(profile)
    model = model or spec.model
    base_url = base_url or spec.base_url

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

    client = _build_client(spec, base_url=base_url)
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    # 采样参数此前被完全忽略：配置里的 temperature 只走 create_llm 那条 langchain 路径，
    # 而生产节点全部走本函数，等于实际使用供应商默认值。显式透传后配置才真正生效。
    sampling: dict[str, object] = {"temperature": spec.temperature if temperature is None else temperature}
    if seed is not None:
        # seed 只降低采样随机性，不保证输出确定（服务端实现与并发都可能引入差异），
        # 因此它是「方差的一个来源」而非「可复现性保证」。
        sampling["seed"] = seed

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
        if delta_content:
            content_parts.append(delta_content)
        if usage is None:
            usage = getattr(payload, "usage", None)

    text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    elapsed = time.monotonic() - start
    total = len(reasoning_text) + len(text)
    reasoning_pct = round(len(reasoning_text) * 100 / total) if total else 0
    # 过程数据先落 meta，再走 schema 校验：校验失败会抛异常触发重试，而那一次调用
    # 消耗的 token 是实实在在花掉的，不该因为「这一版 JSON 不合法」就不记账。
    meta.update(
        {
            "spec": spec.name,
            "model": model,
            "prompt_chars": len(prompt),
            "reasoning_chars": len(reasoning_text),
            "content_chars": len(text),
            "has_usage": usage is not None,
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }
    )
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
