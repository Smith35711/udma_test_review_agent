"""评审流程 CLI 入口。

用法:
  agent/.venv/bin/python agent/scripts/run_review.py --project /path/to/c_project [--modules all|M01|M01,M03|M01-M05] [--log-level LEVEL]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataclasses import asdict

from udma_test_review_agent.configuration import ReviewConfig
from udma_test_review_agent.graph import graph
from udma_test_review_agent.log import get_logger, set_log_level
from udma_test_review_agent.versioning import prepare_run

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="udma 测试评审智能体")
    parser.add_argument("--project", required=True, help="被测项目根目录")
    parser.add_argument("--modules", default="all", help="模块选择：all | M01 | M01,M03 | M01-M05")
    parser.add_argument("--model", default="", help="模型配置名称（config/models.yaml 中的 name），留空取 active")
    parser.add_argument("--small-line-threshold", type=int, default=2000, help="小模块行数阈值（.c 与 .h 物理行数之和）")
    parser.add_argument("--output-root", default="output/review", help="产物输出目录")
    parser.add_argument("--clean", action="store_true", help="清产物重跑：先归档三阶段产物再全量重算")
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="控制台日志等级（默认取环境变量 LOG_LEVEL，未设置则 INFO；日志文件始终记录 DEBUG）",
    )
    args = parser.parse_args()

    set_log_level(args.log_level or os.getenv("LOG_LEVEL", "INFO"))

    config = ReviewConfig(
        project_root=args.project,
        modules=args.modules,
        model_profile=args.model,
        small_module_line_threshold=args.small_line_threshold,
        output_root=args.output_root,
    )
    logger.info("启动评审: project=%s modules=%s model=%s", args.project, args.modules, args.model or "(active)")
    prepare_run(config.output_root, clean=args.clean)
    result = graph.invoke({"config": asdict(config)})
    logger.info("评审结束: report=%s", result.get("report_path"))
    print(json.dumps({"report_path": result.get("report_path")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
