"""运行清单：记录本次运行的全部输入指纹，供断点失效判定使用。

**为什么需要它**：断点续跑只看「产物文件是否存在」，而产物不知道自己是在什么输入下
产生的。所以改动源码后重跑，`scan.json`/`architecture.json` 会被继续复用——架构描述
的是旧代码，findings 来自新代码，一份报告里混着两套来源；换 `--model` 或改提示词同理。
本模块把这些输入压成指纹落盘，重跑时比对，不一致即判定产物陈旧。

**粒度是全局的，这是刻意的**：只要任一输入变了，所有下游产物都可能受影响（改一个
.c 会动到调用图与跨模块契约），而按字段建依赖表一旦漏掉一条边就是静默的错误复用。
宁可多算，不可混用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .configuration import ReviewConfig
from .log import get_logger
from .model_config import resolve_model
from .module_map import _config_path as modules_config_path
from .schemas import FINGERPRINT_FIELDS, RunManifest
from .tools.executors.c_scanner import C_SUFFIXES

logger = get_logger(__name__)

MANIFEST_FILE = "run_manifest.json"
_HASH_LEN = 16
_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:_HASH_LEN]


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:_HASH_LEN]
    except OSError:
        return "missing"


def source_hashes(cfg: ReviewConfig) -> dict[str, str]:
    """被测项目内全部 .c/.h 的 `相对路径 -> 内容哈希`（按路径排序，稳定输出）。"""
    root = Path(cfg.project_root)
    excludes = set(cfg.excludes)
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in C_SUFFIXES:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in excludes for part in rel_parts):
            continue
        out["/".join(rel_parts)] = _file_digest(path)
    return out


def _prompts_digest() -> str:
    """提示词目录下全部 Python 模块的内容哈希。

    提示词是行为的一部分：改了措辞而断点照旧命中，就会拿旧结论冒充新提示词的结果。
    逐个文件取哈希再汇总，任一文件改动都会改变取值。
    """
    parts = []
    for path in sorted(_PROMPT_DIR.glob("*.py")):
        parts.append(f"{path.name}:{_file_digest(path)}")
    return _digest(*parts) if parts else "none"


def _model_digest(cfg: ReviewConfig) -> str:
    """生效模型的关键属性。密钥不参与——换密钥不影响结论，也不该写进产物。"""
    try:
        spec = resolve_model(cfg.model_profile or None)
    except ValueError:
        return "unresolved"
    return _digest(
        spec.name,
        spec.model,
        spec.base_url,
        str(spec.temperature),
        str(cfg.temperature),
        str(cfg.seed),
    )


def _thresholds(cfg: ReviewConfig) -> dict[str, object]:
    return {
        "small_module_line_threshold": cfg.small_module_line_threshold,
        "max_review_attempts": cfg.max_review_attempts,
        "max_findings_per_pass": cfg.max_findings_per_pass,
        "confirmed_confidence_threshold": cfg.confirmed_confidence_threshold,
        "demote_invalid_location": cfg.demote_invalid_location,
        "modules": cfg.modules,
    }


def build_manifest(cfg: ReviewConfig) -> RunManifest:
    """按当前配置与磁盘状态构建运行清单。"""
    sources = source_hashes(cfg)
    sources_digest = _digest(*[f"{k}:{v}" for k, v in sorted(sources.items())]) if sources else "empty"
    thresholds = _thresholds(cfg)
    manifest = RunManifest(
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        project_root=cfg.project_root,
        sources_digest=sources_digest,
        sources=sources,
        module_map_digest=_file_digest(modules_config_path()),
        prompts_digest=_prompts_digest(),
        model_digest=_model_digest(cfg),
        thresholds=thresholds,
        concurrency=cfg.max_parallel,
    )
    manifest.digest = fingerprint(manifest)
    return manifest


def fingerprint(manifest: RunManifest) -> str:
    """输入指纹：由 `FINGERPRINT_FIELDS` 参与计算，展示性字段不参与。"""
    payload = {name: getattr(manifest, name) for name in FINGERPRINT_FIELDS}
    return _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def load_manifest(path: Path) -> RunManifest | None:
    """读取已落盘的清单；缺失或损坏一律返回 None（视为「无有效断点依据」）。"""
    if not path.is_file():
        return None
    try:
        return RunManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.warning("[清单] 读取失败，将视为全部陈旧: %s (%s)", path, exc)
        return None


def save_manifest(path: Path, manifest: RunManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def describe_changes(old: RunManifest | None, new: RunManifest) -> list[str]:
    """列出两次清单之间实质变化的字段，供日志与报告说明「为什么要重算」。"""
    if old is None:
        return ["无既有清单（首次运行或产物早于本机制）"]
    changed = []
    for name in FINGERPRINT_FIELDS:
        before, after = getattr(old, name), getattr(new, name)
        if before == after:
            continue
        if name == "sources" or name == "sources_digest":
            changed.append(f"源码内容变化（{_source_change_brief(old, new)}）")
        elif name == "model_digest":
            changed.append("生效模型或采样参数变化")
        elif name == "prompts_digest":
            changed.append("提示词变化")
        elif name == "artifact_version":
            changed.append(f"产物格式版本 {before} -> {after}")
        elif name == "concurrency":
            changed.append(f"并发数 {before} -> {after}")
        elif name == "thresholds":
            changed.append(f"阈值变化 {before} -> {after}")
        else:
            changed.append(f"{name}: {before} -> {after}")
    return changed


def _source_change_brief(old: RunManifest, new: RunManifest) -> str:
    previous, current = old.sources or {}, new.sources or {}
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    modified = sorted(p for p in set(previous) & set(current) if previous[p] != current[p])
    parts = []
    if modified:
        parts.append(f"修改 {_brief_list(modified)}")
    if added:
        parts.append(f"新增 {_brief_list(added)}")
    if removed:
        parts.append(f"删除 {_brief_list(removed)}")
    return "、".join(parts) or "文件清单一致但汇总哈希不同"


def _brief_list(paths: list[str], limit: int = 5) -> str:
    head = "、".join(paths[:limit])
    return head if len(paths) <= limit else f"{head} 等 {len(paths)} 个"
