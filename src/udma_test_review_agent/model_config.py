"""模型配置：从 YAML 加载多模型列表，解析当前启用模型与运行期覆盖。

配置分层，优先级由低到高：模型条目内置默认值 < `defaults` 块 <
`providers.<名>` 块 < `models[]` 条目。合并只在本模块的装载阶段发生，
产出与旧格式等价的 `list[ModelSpec]`，运行期消费方无需感知 provider 层。
"""

from __future__ import annotations

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
    {"type", "base_url", "api_key", "temperature", "max_retries", "timeout", "extra_headers"}
)

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class ModelConfigError(ValueError):
    """模型配置缺失或非法。"""


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


def load_models_config(path: str | os.PathLike[str] | None = None) -> ModelsConfig:
    """加载并校验模型配置文件；defaults / providers 在返回前已合并进各条目。"""
    config_path = _config_path(path)
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
    """模型配置总览：路径、配置 active、本次生效项及其来源、条目列表。"""

    path: str
    active: str
    effective: str
    source: str
    items: list[ModelSummary]


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
            has_api_key=bool(spec.api_key),
            active=spec.name == config.active,
        )
        for spec in config.models
    ]
    logger.debug(
        "[模型配置] 总览 path=%s active=%s effective=%s 来源=%s 模型数=%d",
        config_path,
        config.active,
        effective,
        source,
        len(items),
    )
    return ModelsOverview(
        path=str(config_path),
        active=config.active,
        effective=effective,
        source=source,
        items=items,
    )


def list_profiles(path: str | os.PathLike[str] | None = None) -> list[str]:
    return [spec.name for spec in load_models_config(path).models]
