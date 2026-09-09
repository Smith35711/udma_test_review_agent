"""模型配置：从 YAML 加载多模型列表，解析当前启用模型与运行期覆盖。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .log import get_logger

logger = get_logger(__name__)

_AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _AGENT_ROOT / "config" / "models.yaml"
CONFIG_PATH_ENV = "MODEL_CONFIG"
PROFILE_ENV = "LLM_PROFILE"
SUPPORTED_TYPES = {"openai"}

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


class ModelSpec(BaseModel):
    """单个模型配置。"""

    name: str
    type: str = "openai"
    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.7
    max_retries: int = 2
    timeout: float = 60.0
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("api_key", "base_url", mode="before")
    @classmethod
    def _expand_string(cls, value: object) -> object:
        return _expand_env(value) if isinstance(value, str) else value

    @field_validator("extra_headers", mode="before")
    @classmethod
    def _expand_headers(cls, value: object) -> object:
        if isinstance(value, dict):
            return {k: _expand_env(v) if isinstance(v, str) else v for k, v in value.items()}
        return value

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


def load_models_config(path: str | os.PathLike[str] | None = None) -> ModelsConfig:
    """加载并校验模型配置文件。"""
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
    try:
        config = ModelsConfig.model_validate(data)
    except ModelConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转为可读配置错误
        raise ModelConfigError(f"模型配置校验失败: {config_path}: {exc}") from exc
    logger.debug(
        "[模型配置] 加载 path=%s 模型数=%d active=%s 列表=%s",
        config_path,
        len(config.models),
        config.active,
        [m.name for m in config.models],
    )
    return config


def resolve_model(
    profile: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> ModelSpec:
    """解析当前启用模型：显式 profile > 环境变量 LLM_PROFILE > 配置 active。"""
    config = load_models_config(path)
    target = profile or os.getenv(PROFILE_ENV) or config.active
    spec = config.get(target)
    logger.debug(
        "[模型配置] 解析 profile=%s 来源=%s -> name=%s model=%s type=%s base_url=%s 密钥=%s",
        target,
        "参数" if profile else ("环境变量" if os.getenv(PROFILE_ENV) else "active"),
        spec.name,
        spec.model,
        spec.type,
        spec.base_url,
        "已配置" if spec.api_key else "未配置(回退)",
    )
    return spec


def list_profiles(path: str | os.PathLike[str] | None = None) -> list[str]:
    return [spec.name for spec in load_models_config(path).models]
