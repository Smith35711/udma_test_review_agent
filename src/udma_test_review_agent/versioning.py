"""产物版本管理：当前产物保持固定路径，重跑时归档三阶段产物到 <模块>/history/<run_ts>/。

规则：
- 每模块子目录下固定保留最新产物：findings_raw.json、findings_verified.json、report.md；
- 上次运行已完成（项目总报告存在）时，启动前将各模块三阶段产物归档，再重新检视；
- 上次运行未完成时视为中断，直接续跑，不归档；
- 「清产物重跑」先归档三阶段产物，再重置其余当前产物（保留 history/），触发全量重算。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .log import get_logger

logger = get_logger(__name__)

MODULE_ARTIFACTS = ("findings_raw.json", "findings_verified.json", "report.md")
PROJECT_REPORT = "report.md"
HISTORY_DIR = "history"

ALL_ROOT_ARTIFACTS = ("scan.json", "global_architecture.json", "critical_paths.json", "run_manifest.json")
ALL_MODULE_ARTIFACTS = MODULE_ARTIFACTS + ("summary.md", "architecture.json", "size.json")
"""输入指纹变化时的全量归档清单（见 `archive_all`）。

比 `MODULE_ARTIFACTS` 更宽：架构与规模产物同样由源码/提示词派生，输入变了它们
一样陈旧，只归档三阶段产物会让「架构基于旧代码、findings 基于新代码」继续成立。
`run_manifest.json` 也在其中——旧产物是在哪份清单下产生的，事后要能查。
"""


def run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_dir(base: Path) -> Path:
    """返回一个尚不存在的目录路径：base 已存在时依次尝试 base-2、base-3……

    时间戳只精确到秒，同一秒内发生两次归档会落到同一目录，而 `shutil.move` 在
    目标同名时直接覆盖——那等于静默丢弃上一份历史。这里退让编号，绝不覆盖。
    """
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = base.with_name(f"{base.name}-{index}")
        if not candidate.exists():
            return candidate
    return base  # 极端情况：交给调用方，至少不静默改语义


def is_run_completed(output_root: Path) -> bool:
    """以项目总报告是否存在作为上次运行完成的标志。"""
    return (output_root / PROJECT_REPORT).is_file()


def archive_all(output_root: Path, run_ts: str) -> int:
    """把**全部**当前产物移入 history/<run_ts>/（根级与各模块级），返回移动文件数。

    与 `_archive_module_artifacts` 的区别在于归档范围：后者只动三阶段产物，用于
    「上次跑完了，重新检视」；本函数用于「输入变了，所有派生结论一律作废」。
    """
    moved = 0
    if not output_root.is_dir():
        return 0
    root_dest: Path | None = None
    for name in ALL_ROOT_ARTIFACTS:
        src = output_root / name
        if not src.is_file():
            continue
        if root_dest is None:
            root_dest = _unique_dir(output_root / HISTORY_DIR / run_ts)
            root_dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(root_dest / name))
        moved += 1
    for module_dir in sorted(output_root.iterdir()):
        if not module_dir.is_dir() or module_dir.name == HISTORY_DIR:
            continue
        present = [module_dir / name for name in ALL_MODULE_ARTIFACTS if (module_dir / name).is_file()]
        if not present:
            continue
        dest = _unique_dir(module_dir / HISTORY_DIR / run_ts)
        dest.mkdir(parents=True, exist_ok=True)
        for src in present:
            shutil.move(str(src), str(dest / src.name))
            moved += 1
        logger.info("[版本] 归档 %s -> %s（%d 个文件）", module_dir.name, dest, len(present))
    return moved


def _archive_module_artifacts(output_root: Path, run_ts: str) -> int:
    """将各模块三阶段产物移入 <模块>/history/<run_ts>/，返回移动文件数。"""
    moved = 0
    if not output_root.is_dir():
        return 0
    for module_dir in sorted(output_root.iterdir()):
        if not module_dir.is_dir() or module_dir.name == HISTORY_DIR:
            continue
        present = [module_dir / name for name in MODULE_ARTIFACTS if (module_dir / name).is_file()]
        if not present:
            continue
        dest = _unique_dir(module_dir / HISTORY_DIR / run_ts)
        dest.mkdir(parents=True, exist_ok=True)
        for src in present:
            shutil.move(str(src), str(dest / src.name))
            moved += 1
        logger.info("[版本] 归档 %s -> %s（%d 个文件）", module_dir.name, dest, len(present))
    return moved


def _remove_keeping_history(path: Path) -> None:
    """递归删除当前产物，保留 history/ 目录。"""
    for entry in path.iterdir():
        if entry.name == HISTORY_DIR:
            continue
        if entry.is_dir():
            _remove_keeping_history(entry)
            try:
                entry.rmdir()
            except OSError:
                pass
        else:
            entry.unlink()


def prepare_run(output_root: str | Path, *, clean: bool = False) -> None:
    """运行前处理产物：按完成状态或 clean 决定归档与重置。"""
    root = Path(output_root)
    if clean:
        run_ts = run_timestamp()
        moved = _archive_module_artifacts(root, run_ts)
        if root.is_dir():
            _remove_keeping_history(root)
        logger.info("[版本] 清产物重跑：归档 %d 个三阶段产物（run=%s），其余当前产物已重置", moved, run_ts)
        return
    if is_run_completed(root):
        run_ts = run_timestamp()
        moved = _archive_module_artifacts(root, run_ts)
        (root / PROJECT_REPORT).unlink(missing_ok=True)
        logger.info("[版本] 上次已完成：归档 %d 个三阶段产物（run=%s），将重新检视", moved, run_ts)
        return
    logger.info("[版本] 上次未完成：续跑，不归档")
