"""评审流程 CLI 入口。

用法:
  agent/.venv/bin/python agent/scripts/run_review.py --project /path/to/c_project [--modules all|M01|M01,M03|M01-M05] [--log-level LEVEL]
  agent/.venv/bin/python agent/scripts/run_review.py --list-models

退出码:
  0  评审完成，且未触发 --fail-on 门槛
  1  有模块评审失败（结果不完整）
  2  评审完成，但检出的已确认问题达到 --fail-on 门槛（供构建门禁使用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataclasses import asdict

from udma_test_review_agent import findings_util as fu
from udma_test_review_agent.configuration import ReviewConfig
from udma_test_review_agent.graph import graph
from udma_test_review_agent.log import get_logger, set_log_level
from udma_test_review_agent.model_config import ROLES, describe_models
from udma_test_review_agent.nodes.common import _AUTO_PARALLEL, _metrics_path, _project_findings_path
from udma_test_review_agent.stats import get_stats, reset_stats
from udma_test_review_agent.versioning import prepare_run

logger = get_logger(__name__)

FAIL_ON_RANK = {"严重": fu.severity_rank("严重"), "致命": fu.severity_rank("致命")}
"""`--fail-on` 可选项 -> 触发的严重度门槛（rank 越小越严重，命中条件是 <=）。"""

EXIT_MODULE_FAILURE = 1
EXIT_SEVERITY_THRESHOLD = 2


def _print_models(profile: str | None) -> int:
    """打印可选模型表与角色路由；返回进程退出码（0 成功，1 配置错误）。"""
    try:
        overview = describe_models(profile=profile)
    except ValueError as exc:  # ModelConfigError 继承 ValueError
        print(f"模型配置错误: {exc}", file=sys.stderr)
        return 1
    print(f"模型配置: {overview.path}")
    print(f"生效模型: {overview.effective}（来源: {overview.source}；配置 active={overview.active}）")
    names = {item.name for item in overview.items}
    unknown = overview.effective not in names
    if unknown:
        # 只会在 --model / LLM_PROFILE 填了不存在的名字时出现；报错但仍打印列表便于对照
        print(
            f"模型配置错误: 生效模型 {overview.effective} 不在模型列表中，可选: {sorted(names)}",
            file=sys.stderr,
        )
    print()
    name_w = max([len(item.name) for item in overview.items] + [4])
    provider_w = max([len(item.provider or "-") for item in overview.items] + [8])
    print(f"{'NAME':<{name_w}}  {'PROVIDER':<{provider_w}}  {'MODEL':<38}  {'ACTIVE':<6}  KEY")
    for item in overview.items:
        mark = "*" if item.name == overview.effective else ("-" if item.active else "")
        print(
            f"{item.name:<{name_w}}  {item.provider or '-':<{provider_w}}  {item.model:<38}  "
            f"{mark:<6}  {'已配置' if item.has_api_key else '未配置'}"
        )
    print()
    print("角色路由（config/models.yaml 的 roles 块；未列出的角色走生效模型）:")
    for role in ROLES:
        target = overview.roles.get(role)
        print(f"  {role:<9} -> {target or '(未配置)'}")
    print()
    print(
        f"共 {len(overview.items)} 个模型；用 --model NAME 或环境变量 LLM_PROFILE 切换"
        "（填模型 name，不是 provider 名）。ACTIVE 列 * 为本次生效，- 为配置 active 但被覆盖。\n"
        "优先级：--model（压过一切）> roles 角色路由 > LLM_PROFILE > active。"
    )
    return 1 if unknown else 0


def _build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数表。

    抽成独立函数是为了让测试能直接断言参数契约（例如 `--fail-on` 的 choices 与
    `FAIL_ON_RANK` 同源）——写在 `main()` 里就只能靠子进程调用来测。
    """
    parser = argparse.ArgumentParser(description="udma 测试评审智能体")
    parser.add_argument(
        "--project",
        default="",
        help="被测项目根目录（跑评审时必填；仅使用 --list-models 时可省略）",
    )
    parser.add_argument("--modules", default="all", help="模块选择：all | M01 | M01,M03 | M01-M05")
    parser.add_argument(
        "--model",
        default="",
        help="模型配置名称（config/models.yaml 中的 name），留空取 active；可选值见 --list-models",
    )
    parser.add_argument("--small-line-threshold", type=int, default=2000, help="小模块行数阈值（.c 与 .h 物理行数之和）")
    parser.add_argument("--output-root", default="output/review", help="产物输出目录")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        metavar="N",
        help=(
            f"模块检视循环的并发数：0=自动（取 min(模块数, {_AUTO_PARALLEL})），1=串行，N>=1 为显式上限；"
            "仅作用于模块循环，架构阶段与总报告仍串行"
        ),
    )
    parser.add_argument("--clean", action="store_true", help="清产物重跑：先归档三阶段产物再全量重算")
    parser.add_argument(
        "--fail-on",
        choices=["none", *FAIL_ON_RANK],
        default="none",
        help=(
            "检出达到该等级的**已确认**问题时以退出码 2 结束，供构建门禁使用："
            "none=只看模块失败（默认，与既有行为一致）｜严重｜致命。"
            "只统计 confirmed——uncertain 是需要人看的，不该让构建失败"
        ),
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="列出 config/models.yaml 中的可选模型与当前生效项，随后退出（无需 --project）",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="控制台与文件日志等级（默认取环境变量 LOG_LEVEL，未设置则 INFO）",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    set_log_level(args.log_level or os.getenv("LOG_LEVEL", "INFO"))

    if args.list_models:
        raise SystemExit(_print_models(args.model or None))

    if not args.project.strip():
        parser.error("--project 必填（除非仅使用 --list-models）")

    try:
        config = ReviewConfig(
            project_root=args.project,
            modules=args.modules,
            model_profile=args.model,
            small_module_line_threshold=args.small_line_threshold,
            output_root=args.output_root,
            max_parallel=args.concurrency,
        )
    except ValueError as exc:  # 配置非法（如并发数为负）：单行中文报错，不打印 traceback
        parser.error(str(exc))

    logger.info(
        "启动评审: project=%s modules=%s model=%s 并发=%s",
        args.project,
        args.modules,
        args.model or "(active)",
        args.concurrency if args.concurrency > 0 else f"自动(上限 {_AUTO_PARALLEL})",
    )
    if args.concurrency > _AUTO_PARALLEL:
        logger.warning(
            "并发数 %d 超过自动上限 %d，供应商限流（429）风险上升；过大时表现为被隔离的单模块失败",
            args.concurrency,
            _AUTO_PARALLEL,
        )
    prepare_run(config.output_root, clean=args.clean)
    reset_stats()  # 过程统计是进程级单例；同进程内重复 invoke（测试/长驻）必须清零
    result = graph.invoke({"config": asdict(config)})
    failed = dict(result.get("module_failures") or {})
    logger.info("评审结束: report=%s 失败模块=%d", result.get("report_path"), len(failed))

    summary = _run_summary(config, result, failed, args.fail_on)
    print(json.dumps(summary, ensure_ascii=False))
    if failed:
        for module, reason in sorted(failed.items()):
            print(f"模块 {module} 评审失败: {reason}", file=sys.stderr)
        raise SystemExit(EXIT_MODULE_FAILURE)
    if summary["exit_code"] == EXIT_SEVERITY_THRESHOLD:
        print(
            f"检出已确认的最高严重等级为 {summary['worst_confirmed_severity']}，"
            f"达到 --fail-on {args.fail_on} 门槛",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_SEVERITY_THRESHOLD)


def _run_summary(
    config: ReviewConfig, result: dict, failed: dict[str, str], fail_on: str
) -> dict:
    """组装 stdout 的机器可读摘要并决定退出码。

    退出码依据 `findings.json` 里的 `worst_confirmed_severity`，由报告节点在落盘时
    一并写好——CLI 不自己重算严重度，避免「报告里写致命、门禁却放行」这类两处判定
    不一致的问题。产物缺失（例如报告节点本身失败）时门槛不生效，只保留模块失败语义：
    宁可放行也不能凭一个读不到的产物拦下构建。
    """
    stats = get_stats().snapshot()
    filt = {"findings_total": 0, "refuted_rate": 0.0, "observation_rate": 0.0}
    worst = ""
    findings_path = _project_findings_path(config)
    try:
        export = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("机器可读产物不可用，严重度门槛不生效: %s (%s)", findings_path, exc)
        export = {}
    if export:
        worst = str(export.get("worst_confirmed_severity") or "")
        counts = export.get("counts") or {}
        total = int(counts.get("confirmed", 0)) + int(counts.get("refuted", 0)) + int(
            counts.get("uncertain", 0)
        )
        filt = {
            "findings_total": total,
            "refuted": int(counts.get("refuted", 0)),
            "observations": int(counts.get("observations", 0)),
            "refuted_rate": round(int(counts.get("refuted", 0)) / total, 4) if total else 0.0,
        }
    threshold = FAIL_ON_RANK.get(fail_on)
    triggered = bool(
        worst and threshold is not None and fu.severity_rank(worst) <= threshold
    )
    exit_code = 0
    if failed:
        exit_code = EXIT_MODULE_FAILURE
    elif triggered:
        exit_code = EXIT_SEVERITY_THRESHOLD
    return {
        "report_path": result.get("report_path"),
        "findings_path": str(findings_path),
        "metrics_path": str(_metrics_path(config)),
        "max_parallel": config.max_parallel,
        "failed_modules": failed,
        "worst_confirmed_severity": worst,
        "exit_code": exit_code,
        "llm_calls": stats["llm"]["calls"],
        "total_tokens": stats["llm"]["total_tokens"],
        "wall_clock": stats["wall_clock"],
        "filter": filt,
    }


if __name__ == "__main__":
    main()
