"""评审流程 CLI 入口。

用法:
  agent/.venv/bin/python agent/scripts/run_review.py --project /path/to/c_project [--mode single|all] [--module NAME]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataclasses import asdict

from udma_test_review_agent.configuration import ReviewConfig
from udma_test_review_agent.graph import graph
from udma_test_review_agent.log import get_logger

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="udma 测试评审智能体")
    parser.add_argument("--project", required=True, help="被测项目根目录")
    parser.add_argument("--mode", default="all", choices=["single", "all"], help="执行模式")
    parser.add_argument("--module", default="", help="single 模式的目标模块名")
    parser.add_argument("--small-threshold", type=int, default=30000, help="小模块 token 阈值")
    parser.add_argument("--output-root", default="output/review", help="产物输出目录")
    args = parser.parse_args()

    config = ReviewConfig(
        project_root=args.project,
        mode=args.mode,
        target_module=args.module,
        small_module_token_threshold=args.small_threshold,
        output_root=args.output_root,
    )
    logger.info("启动评审: project=%s mode=%s module=%s", args.project, args.mode, args.module)
    result = graph.invoke({"config": asdict(config)})
    logger.info("评审结束: report=%s", result.get("report_path"))
    print(json.dumps({"report_path": result.get("report_path")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
