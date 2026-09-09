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


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def is_run_completed(output_root: Path) -> bool:
    """以项目总报告是否存在作为上次运行完成的标志。"""
    return (output_root / PROJECT_REPORT).is_file()


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
        dest = module_dir / HISTORY_DIR / run_ts
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
        run_ts = _run_timestamp()
        moved = _archive_module_artifacts(root, run_ts)
        if root.is_dir():
            _remove_keeping_history(root)
        logger.info("[版本] 清产物重跑：归档 %d 个三阶段产物（run=%s），其余当前产物已重置", moved, run_ts)
        return
    if is_run_completed(root):
        run_ts = _run_timestamp()
        moved = _archive_module_artifacts(root, run_ts)
        (root / PROJECT_REPORT).unlink(missing_ok=True)
        logger.info("[版本] 上次已完成：归档 %d 个三阶段产物（run=%s），将重新检视", moved, run_ts)
        return
    logger.info("[版本] 上次未完成：续跑，不归档")
