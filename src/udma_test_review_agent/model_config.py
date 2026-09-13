"""模型配置：从 YAML 加载多模型列表，解析当前启用模型与运行期覆盖。

配置分层，优先级由低到高：模型条目内置默认值 < `defaults` 块 <
`providers.<名>` 块 < `models[]` 条目。合并只在本模块的装载阶段发生，
产出与旧格式等价的 `list[ModelSpec]`，运行期消费方无需感知 provider 层。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .log import get_logger

logger = get_logger(__name__)

_AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _AGENT_ROOT / "config" / "models.yaml"
CONFIG_PATH_ENV = "MODEL_CONFIG"
PROFILE_ENV = "LLM_PROFILE"
SUPPORTED_TYPES = {"openai"}
INHERITABLE_FIELDS = frozenset(
    {"type", "base_url", "api_key", "temperature", "seed", "max_retries", "timeout", "extra_headers"}
)

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

ROLES = ("arch", "review", "rebuttal", "summary")
"""可路由的节点角色。

- `arch`：全局架构、模块架构、关键路径（归纳类，长输入的粗活）
- `review`：模块检视（效果的决定性环节）
- `rebuttal`：反驳（独立证伪，需要与检视不同的视角）
- `summary`：模块执行摘要（纯文字加工，最廉价的一环）

**角色名是闭集并会被校验**：写错一个字母（`reviews`）若被静默接受，路由会一声不响地
不生效，而用户以为自己已经换过模型了——这类「配置写了没反应」正是本工程着力消除的
失败模式。未配置的角色回退到默认解析链（`--model` > `LLM_PROFILE` > `active`）。
"""


_OPENCODE_CONFIG_DEFAULT = Path.home() / ".config" / "opencode" / "opencode.json"
"""opencode 全局配置的默认路径，作为密钥的最后一级回退来源。"""


class ModelConfigError(ValueError):
    """模型配置缺失或非法。"""


def _load_opencode_providers(config_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """读取 opencode 全局配置的 `provider` 表；读取失败一律返回空字典。"""
    path = Path(config_path or os.getenv("OPENCODE_CONFIG", _OPENCODE_CONFIG_DEFAULT))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    providers = data.get("provider") if isinstance(data, dict) else None
    return providers if isinstance(providers, dict) else {}


def _provider_options(block: object) -> dict[str, str]:
    """取单个 provider 块的 `options` 中的 apiKey/baseURL。"""
    options = block.get("options") if isinstance(block, dict) else None
    if not isinstance(options, dict):
        return {}
    return {key: options[key] for key in ("apiKey", "baseURL") if options.get(key)}


def load_deepseek_config(config_path: str | os.PathLike | None = None) -> dict[str, str]:
    """读取 opencode 全局配置中的 deepseek provider 选项（apiKey/baseURL）。

    保留为兼容入口；通用逻辑见 `load_provider_config`。
    """
    return _provider_options(_load_opencode_providers(config_path).get("deepseek"))


def load_provider_config(
    base_url: str = "",
    provider: str = "",
    config_path: str | os.PathLike | None = None,
) -> dict[str, str]:
    """按 provider 名或端点 URL 从 opencode 全局配置里匹配 provider 选项。

    名称与 URL 两条路都要：`models.yaml` 里的 provider 名（如 `opencode`）未必等于
    opencode.json 里的键（如 `opencode-go`），但两者的 `baseURL` 一定指向同一端点。
    先按名精确匹配，再按 URL 归一化比对；都匹配不到则回退 deepseek 块（历史行为）。
    """
    providers = _load_opencode_providers(config_path)
    if provider and provider in providers:
        options = _provider_options(providers[provider])
        if options.get("apiKey"):
            return options
    want = str(base_url or "").rstrip("/")
    if want:
        for block in providers.values():
            options = _provider_options(block)
            if options.get("apiKey") and str(options.get("baseURL") or "").rstrip("/") == want:
                return options
    return _provider_options(providers.get("deepseek"))


def resolve_api_key(spec: ModelSpec) -> str:
    """解析模型实际可用的 API Key：条目字面量 > 环境变量 > opencode 全局配置。

    **这是唯一副本，运行期与 `--list-models` 共用。** 此前 `describe_models` 只看
    `spec.api_key`，而运行期 `llm.py` 还会继续回退到 `DEEPSEEK_API_KEY` /
    `LLM_API_KEY` / `~/.config/opencode/opencode.json`——于是配置文件里用
    `${DEEPSEEK_API_KEY}` 引用（展开后为空）的模型，列表显示「未配置」却能正常跑，
    用户会据此误判该换模型。密钥本体只在运行期读取，本函数不打印任何取值。

    全局配置的匹配按 `spec` 的 provider 名与 `base_url` 进行，故 `opencode-go`
    这类「配置里写 `${OPENCODE_API_KEY}`、密钥实际在 opencode.json 里」的端点也能取到。
    """
    if spec.api_key:
        return spec.api_key
    env_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
    if env_key:
        return env_key
    return load_provider_config(base_url=spec.base_url, provider=spec.provider).get("apiKey", "")


def _expand_env(value: str) -> str:
    """将 ${VAR} / ${VAR:-默认值} 替换为环境变量取值。"""

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        env_value = os.getenv(name)
        if env_value is not None:
            return env_value
        return default or ""

    return _ENV_REF_RE.sub(repl, value)


def _is_blank(value: object) -> bool:
    """是否为「未填写」：YAML 里 `timeout:` 得到 None，`base_url: ""` 得到空串。

    两者都视为未声明（否则手滑的空行会把下层取值顶成 None 并报出下层字段错），
    但 0 / False / {} 这类合法取值不算空。
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _drop_blank(values: dict[str, Any]) -> dict[str, Any]:
    """剔除「未填写」的字段，使下层取值得以沿用。"""
    return {key: value for key, value in values.items() if not _is_blank(value)}


def _expand_str(value: object) -> object:
    """对字符串做 ${VAR} / ${VAR:-默认值} 展开，非字符串原样返回。"""
    return _expand_env(value) if isinstance(value, str) else value


def _expand_headers(value: object) -> object:
    """对请求头字典逐值做环境变量展开，非字典原样返回。"""
    if isinstance(value, dict):
        return {k: _expand_env(v) if isinstance(v, str) else v for k, v in value.items()}
    return value


def _format_errors(exc: Exception) -> str:
    """把 Pydantic 校验错误整理为「字段: 原因」拼接的可读文本。"""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    return "；".join(
        f"{'.'.join(str(part) for part in item.get('loc', ())) or '(根)'}: {item.get('msg', '')}"
        for item in exc.errors()
    )


class ModelSpec(BaseModel):
    """单个模型配置：解析后的最终形态，运行期唯一被消费的形状。"""

    name: str
    type: str = "openai"
    model: str
    base_url: str
    api_key: str = ""
    provider: str = ""
    temperature: float = 0.7
    seed: int | None = None
    """采样种子。`None` = 不发送该参数（供应商自由采样）。

    与 `temperature` 同为「条目级采样参数」，优先级也一样：调用方显式传入的
    （`ReviewConfig.seed`）压过条目，条目没写则由 `llm.py` 决定不发。
    它压低的只是采样随机性，**不承诺输出确定**——服务端实现与并发仍会引入差异，
    故它只减少方差的一个来源，不能当作可复现性保证。
    """

    max_retries: int = 2
    timeout: float = 60.0
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("api_key", "base_url", mode="before")
    @classmethod
    def _expand_string(cls, value: object) -> object:
        return _expand_str(value)

    @field_validator("extra_headers", mode="before")
    @classmethod
    def _expand_headers(cls, value: object) -> object:
        return _expand_headers(value)

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if value not in SUPPORTED_TYPES:
            raise ValueError(f"不支持的模型类型: {value}，可选: {sorted(SUPPORTED_TYPES)}")
        return value


class ProviderSpec(BaseModel):
    """provider 块与 defaults 块：同一端点（或全体模型）的公共字段。

    数值字段一律以 None 表示「未声明」，取用时须 model_dump(exclude_unset=True)，
    否则 provider 块未写的字段会注入具体取值并遮蔽 defaults 块的配置。
    本类不做 ${VAR} 展开：展开只在合并后的 ModelSpec 上统一做一次，
    避免环境变量取值本身含 ${...} 时出现二次展开。
    """

    type: str = "openai"
    base_url: str = ""
    api_key: str = ""
    temperature: float | None = None
    seed: int | None = None
    max_retries: int | None = None
    timeout: float | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if value not in SUPPORTED_TYPES:
            raise ValueError(f"不支持的模型类型: {value}，可选: {sorted(SUPPORTED_TYPES)}")
        return value


class ModelsConfig(BaseModel):
    """模型配置根对象。"""

    active: str = ""
    models: list[ModelSpec]
    roles: dict[str, str] = Field(
        default_factory=dict,
        description=f"节点角色 -> 模型 name 的路由表；可选角色见 {ROLES}。未配置的角色回退默认解析链",
    )

    @model_validator(mode="after")
    def _check(self) -> ModelsConfig:
        if not self.models:
            raise ValueError("models 列表不能为空")
        names = [m.name for m in self.models]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"模型名称重复: {duplicates}")
        if not self.active:
            self.active = names[0]
        if self.active not in names:
            raise ValueError(f"active 指向不存在的模型: {self.active}，可选: {names}")
        unknown_roles = sorted(set(self.roles) - set(ROLES))
        if unknown_roles:
            raise ValueError(
                f"roles 含未知角色: {unknown_roles}，可选: {list(ROLES)}"
                "（角色名写错若不报错，路由会静默失效）"
            )
        bad_targets = sorted({v for v in self.roles.values() if v not in names})
        if bad_targets:
            raise ValueError(f"roles 指向不存在的模型: {bad_targets}，可选: {names}")
        return self

    def get(self, name: str) -> ModelSpec:
        for spec in self.models:
            if spec.name == name:
                return spec
        raise ModelConfigError(f"未找到模型配置: {name}，可选: {[m.name for m in self.models]}")

    @property
    def active_model(self) -> ModelSpec:
        return self.get(self.active)


def _config_path(path: str | os.PathLike[str] | None = None) -> Path:
    raw = path or os.getenv(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH
    return Path(raw)


def _check_block_fields(block: dict[str, Any], label: str, config_path: Path) -> None:
    """校验 defaults / provider 块的键集合，禁止 provider、name、model 混入。"""
    unknown = sorted(set(block) - INHERITABLE_FIELDS)
    if unknown:
        raise ModelConfigError(
            f"{label} 含不支持的字段: {config_path}: {unknown}，"
            f"可选: {sorted(INHERITABLE_FIELDS)}（provider / name / model 不可写在此处）"
        )


def _merge_entries(data: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """把 defaults / providers 逐级合并进 models，返回与旧格式等价的字典。

    优先级由低到高：模型条目内置默认值 < defaults < providers.<名> < models[] 条目。
    未声明 provider 的条目按旧格式处理，只叠加 defaults；空值视为「未填写」沿用下层。
    """
    providers_raw = data.get("providers") or {}
    defaults_raw = data.get("defaults") or {}
    if not isinstance(providers_raw, dict):
        raise ModelConfigError(f"providers 须为映射: {config_path}: providers")
    if not isinstance(defaults_raw, dict):
        raise ModelConfigError(f"defaults 须为映射: {config_path}: defaults")

    _check_block_fields(defaults_raw, "defaults 块", config_path)
    try:
        defaults_dict = _drop_blank(
            ProviderSpec.model_validate(defaults_raw).model_dump(exclude_unset=True)
        )
    except ValidationError as exc:
        raise ModelConfigError(
            f"defaults 块非法: {config_path}: defaults: {_format_errors(exc)}"
        ) from exc

    providers: dict[str, ProviderSpec] = {}
    for name, block in providers_raw.items():
        if not isinstance(block, dict):
            raise ModelConfigError(f"provider 块须为映射: {config_path}: providers.{name}")
        _check_block_fields(block, f"provider 块 providers.{name}", config_path)
        try:
            providers[str(name)] = ProviderSpec.model_validate(block)
        except ValidationError as exc:
            raise ModelConfigError(
                f"provider 块非法: {config_path}: providers.{name}: {_format_errors(exc)}"
            ) from exc

    models_raw = data.get("models")
    if not isinstance(models_raw, list):
        raise ModelConfigError(f"models 须为列表: {config_path}: models")

    merged_models: list[dict[str, Any]] = []
    for index, entry in enumerate(models_raw):
        if not isinstance(entry, dict):
            raise ModelConfigError(f"模型条目须为映射: {config_path}: models[{index}]")
        merged: dict[str, Any] = dict(defaults_dict)
        # extra_headers 单独记录：其余字段整体覆盖，请求头须逐级按键合并，
        # 否则 provider 写了 extra_headers 就会把 defaults 的请求头整体顶掉
        default_headers = dict(merged.get("extra_headers") or {})
        provider_headers: dict[str, Any] = {}
        provider_name = entry.get("provider")
        if provider_name is not None:
            if not isinstance(provider_name, str):
                raise ModelConfigError(
                    f"provider 名须为字符串: {config_path}: models[{index}].provider={provider_name!r}"
                )
            if provider_name not in providers:
                raise ModelConfigError(
                    f"provider 未定义: {config_path}: models[{index}].provider={provider_name!r}，"
                    f"可选: {sorted(providers) or '（配置中未声明 providers 块）'}"
                )
            provider_fields = _drop_blank(
                providers[provider_name].model_dump(exclude_unset=True)
            )
            provider_headers = dict(provider_fields.get("extra_headers") or {})
            merged.update(provider_fields)
        entry_headers = entry.get("extra_headers") or {}
        if not isinstance(entry_headers, dict):
            raise ModelConfigError(
                f"extra_headers 须为映射: {config_path}: models[{index}].extra_headers"
            )
        # 请求头三级浅合并：同名键由上层覆盖，下层其余键保留
        merged["extra_headers"] = {
            **default_headers,
            **provider_headers,
            **{k: v for k, v in entry_headers.items() if v is not None},
        }
        overridden: list[str] = []
        for key, value in entry.items():
            # 继承类字段的空值视为「未填写」，沿用 provider / defaults；name、model 等不在此列
            if key == "extra_headers" or (key in INHERITABLE_FIELDS and _is_blank(value)):
                continue
            if key in merged and merged[key] != value:
                overridden.append(key)
            merged[key] = value
        if overridden:
            logger.debug(
                "[模型配置] 条目覆盖 provider 字段: models[%d] name=%s 覆盖=%s",
                index,
                entry.get("name") or merged.get("name"),
                sorted(overridden),
            )
        if not merged.get("base_url"):
            origin = f"provider {provider_name!r} 中未提供" if provider_name else "未填写"
            raise ModelConfigError(
                f"模型条目缺少必填字段 base_url: {config_path}: models[{index}]"
                f"（{origin}），请在 provider 或该条目中补齐"
            )
        merged_models.append(merged)

    logger.debug(
        "[模型配置] provider 合并 providers=%s defaults=%s 模型数=%d",
        sorted(providers),
        sorted(defaults_dict),
        len(merged_models),
    )
    result = dict(data)
    result.pop("providers", None)
    result.pop("defaults", None)
    result["models"] = merged_models
    return result


_CONFIG_CACHE: dict[str, tuple[tuple[int, int], ModelsConfig]] = {}
_CONFIG_CACHE_MAX = 8
"""`load_models_config` 的结果缓存，键为路径、失效依据为 `(mtime_ns, size)`。

`resolve_model` 会被**每一次 LLM 调用**调用一次（`stream_llm_once` 开头），每次都要
读 YAML、跑两级合并与校验；加上角色路由后还会再多一次。运行期配置是不变的，
这些重复解析纯属浪费。用文件时间戳做失效依据，改配置仍会立即生效。

**缓存对象按只读对待**：多个调用方共享同一实例，改写它会污染后续调用。
"""


def reset_config_cache() -> None:
    """清空模型配置缓存。供测试在改写配置文件/环境变量后强制重新解析。"""
    _CONFIG_CACHE.clear()


def load_models_config(path: str | os.PathLike[str] | None = None) -> ModelsConfig:
    """加载并校验模型配置文件；defaults / providers 在返回前已合并进各条目。

    结果按文件时间戳缓存（见 `_CONFIG_CACHE`），返回的对象按只读对待。
    """
    config_path = _config_path(path)
    key = str(config_path)
    try:
        stat = config_path.stat()
        stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if stamp is not None:
        cached = _CONFIG_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    config = _load_models_config(config_path)
    if stamp is not None:
        if len(_CONFIG_CACHE) >= _CONFIG_CACHE_MAX:
            _CONFIG_CACHE.pop(next(iter(_CONFIG_CACHE)))
        _CONFIG_CACHE[key] = (stamp, config)
    return config


def _load_models_config(config_path: Path) -> ModelsConfig:
    if not config_path.is_file():
        raise ModelConfigError(
            f"模型配置文件不存在: {config_path}（可用环境变量 {CONFIG_PATH_ENV} 指定）"
        )
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ModelConfigError(f"模型配置文件读取失败: {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelConfigError(f"模型配置根节点须为映射: {config_path}")
    data = _merge_entries(data, config_path)
    try:
        config = ModelsConfig.model_validate(data)
    except ModelConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转为可读配置错误
        raise ModelConfigError(f"模型配置校验失败: {config_path}: {_format_errors(exc)}") from exc
    logger.debug(
        "[模型配置] 加载 path=%s 模型数=%d active=%s 列表=%s",
        config_path,
        len(config.models),
        config.active,
        [m.name for m in config.models],
    )
    return config


def _resolve_target(config: ModelsConfig, profile: str | None = None) -> tuple[str, str]:
    """解析生效模型名及其来源：显式参数 > 环境变量 LLM_PROFILE > 配置 active。"""
    env_profile = os.getenv(PROFILE_ENV)
    if profile:
        return profile, "参数"
    if env_profile:
        return env_profile, f"环境变量 {PROFILE_ENV}"
    return config.active, "active"


def resolve_model(
    profile: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> ModelSpec:
    """解析当前启用模型：显式 profile > 环境变量 LLM_PROFILE > 配置 active。"""
    config = load_models_config(path)
    target, source = _resolve_target(config, profile)
    spec = config.get(target)
    logger.debug(
        "[模型配置] 解析 profile=%s 来源=%s -> name=%s model=%s type=%s provider=%s base_url=%s 密钥=%s",
        target,
        source,
        spec.name,
        spec.model,
        spec.type,
        spec.provider or "-",
        spec.base_url,
        "已配置" if spec.api_key else "未配置(回退)",
    )
    return spec


def resolve_role(
    role: str,
    path: str | os.PathLike[str] | None = None,
) -> str:
    """取角色对应的模型 name；未配置该角色时返回空串。

    **返回空串而不是回退到 active**：调用方需要区分「该角色被显式指定了模型」与
    「没配，走默认链」。前者要压过 `LLM_PROFILE` 环境变量（那是全局默认，不该
    无声地作废整套路由），后者必须让位给它。把回退做在这里就丢掉了这个区别。
    """
    if not role:
        return ""
    config = load_models_config(path)
    return config.roles.get(role, "")


class ModelSummary(BaseModel):
    """单个模型的展示摘要：供 CLI 与 Web 展示，不含密钥本体。"""

    name: str
    model: str
    provider: str = ""
    type: str = "openai"
    base_url: str = ""
    has_api_key: bool = False
    active: bool = False


class ModelsOverview(BaseModel):
    """模型配置总览：路径、配置 active、本次生效项及其来源、条目列表、角色路由。"""

    path: str
    active: str
    effective: str
    source: str
    items: list[ModelSummary]
    roles: dict[str, str] = Field(default_factory=dict, description="角色 -> 模型 name；未配置的角色不在表中")


def describe_models(
    path: str | os.PathLike[str] | None = None,
    profile: str | None = None,
) -> ModelsOverview:
    """列出全部模型及其来源 provider，并标记配置 active 与本次生效项。

    `active` 为配置文件中的 active；`effective` 为综合参数与环境变量后的
    实际生效项（来源见 `source`）。两者都填模型 name，与 provider 名无关。
    """
    config_path = _config_path(path)
    config = load_models_config(config_path)
    effective, source = _resolve_target(config, profile)
    items = [
        ModelSummary(
            name=spec.name,
            model=spec.model,
            provider=spec.provider,
            type=spec.type,
            base_url=spec.base_url,
            has_api_key=bool(resolve_api_key(spec)),
            active=spec.name == config.active,
        )
        for spec in config.models
    ]
    logger.debug(
        "[模型配置] 总览 path=%s active=%s effective=%s 来源=%s 模型数=%d 角色=%s",
        config_path,
        config.active,
        effective,
        source,
        len(items),
        config.roles,
    )
    return ModelsOverview(
        path=str(config_path),
        active=config.active,
        effective=effective,
        source=source,
        items=items,
        roles=dict(config.roles),
    )


def list_profiles(path: str | os.PathLike[str] | None = None) -> list[str]:
    return [spec.name for spec in load_models_config(path).models]
