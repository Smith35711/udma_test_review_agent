"""模块映射配置：文件级模块标识（M01…）的人工映射与选择解析。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

from .log import get_logger

logger = get_logger(__name__)

_AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODULES_PATH = _AGENT_ROOT / "config" / "modules.yaml"
MODULES_PATH_ENV = "MODULES_CONFIG"
_ID_RE = re.compile(r"^[A-Za-z]\d{1,4}$")


class ModuleMapError(ValueError):
    """模块映射配置缺失或非法。"""


class ModuleEntry(BaseModel):
    id: str
    path: str

    @field_validator("id", "path", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError(f"非法模块标识: {value}（应形如 M01）")
        return value


class ModulesMap(BaseModel):
    modules: list[ModuleEntry]

    @model_validator(mode="after")
    def _check(self) -> ModulesMap:
        if not self.modules:
            raise ValueError("modules 列表不能为空")
        ids = [m.id for m in self.modules]
        dup_ids = sorted({i for i in ids if ids.count(i) > 1})
        if dup_ids:
            raise ValueError(f"模块标识重复: {dup_ids}")
        paths = [m.path for m in self.modules]
        dup_paths = sorted({p for p in paths if paths.count(p) > 1})
        if dup_paths:
            raise ValueError(f"模块路径重复: {dup_paths}")
        return self


def id_sort_key(module_id: str) -> tuple:
    """模块标识排序键：按其中数字段做数值比较，如 M2 < M10。"""
    return tuple(int(part) if part.isdigit() else part for part in re.findall(r"\d+|\D+", module_id))


def _config_path(path: str | os.PathLike[str] | None = None) -> Path:
    raw = path or os.getenv(MODULES_PATH_ENV) or DEFAULT_MODULES_PATH
    return Path(raw)


def load_modules_map(path: str | os.PathLike[str] | None = None) -> ModulesMap:
    """加载并校验模块映射文件。"""
    config_path = _config_path(path)
    if not config_path.is_file():
        raise ModuleMapError(
            f"模块映射文件不存在: {config_path}（可用环境变量 {MODULES_PATH_ENV} 指定）"
        )
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ModuleMapError(f"模块映射文件读取失败: {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModuleMapError(f"模块映射根节点须为映射: {config_path}")
    try:
        modules_map = ModulesMap.model_validate(data)
    except ModuleMapError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转为可读配置错误
        raise ModuleMapError(f"模块映射校验失败: {config_path}: {exc}") from exc
    logger.debug(
        "[模块映射] 加载 path=%s 数量=%d ids=%s",
        config_path,
        len(modules_map.modules),
        [m.id for m in modules_map.modules],
    )
    return modules_map


def parse_selection(selector: str, available_ids: list[str]) -> list[str]:
    """解析模块选择：all | M01 | M01,M03 | M01-M05；返回按标识升序去重列表。"""
    text = (selector or "").strip()
    if not text:
        raise ModuleMapError("模块选择不能为空（all 或 M01,M03-M05）")
    ordered = sorted(available_ids, key=id_sort_key)
    if text.lower() == "all":
        logger.debug("[模块选择] all -> %s", ordered)
        return ordered
    index = {mid: i for i, mid in enumerate(ordered)}
    selected: list[str] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            start, end = start.strip(), end.strip()
            if start not in index or end not in index:
                raise ModuleMapError(f"区间端点不存在: {token}，可选: {ordered}")
            lo, hi = sorted((index[start], index[end]))
            selected.extend(ordered[lo : hi + 1])
        else:
            if token not in index:
                raise ModuleMapError(f"模块标识不存在: {token}，可选: {ordered}")
            selected.append(token)
    seen: set[str] = set()
    unique = [mid for mid in selected if not (mid in seen or seen.add(mid))]
    result = sorted(unique, key=id_sort_key)
    logger.debug("[模块选择] selector=%s -> %s", selector, result)
    return result
