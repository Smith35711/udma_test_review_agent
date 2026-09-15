"""检视流程命令行入口。

用法:
  agent2/.venv/bin/python agent2/scripts/run_review.py --project /path/to/c_project \\
      [--stages arch,review] [--modules M01,M03] [--concurrency N] [--timeout SEC] \\
      [--log-level LEVEL] [--log-file PATH]

退出码:
  0  执行完成，且没有模块失败
  1  有模块失败（其余模块已照常执行完）
  2  前置检查未通过（参数非法、模型连通性测试失败、模块清单缺失）
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent2.artifacts import read_json, write_json
from agent2.configuration import SMALL_MODULE_LINE_LIMIT, STAGE_NAMES, RunConfig
from agent2.graph import build_large_review_graph, build_review_graph, build_scan_graph
from agent2.llm import stream_llm_once
from agent2.log import current_stage, get_logger, set_log_file, set_log_level

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_MODULE_FAILED = 1
EXIT_PRECONDITION = 2

_SCAN_STAGE_LABEL = "扫描"
_CONNECTIVITY_PROMPT = "回复：ok"

DEFAULT_LARGE_CONCURRENCY = 2
"""大模块的默认并发。用户要求「大模块两个两个地跑」。"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Linux C 代码检视智能体")
    parser.add_argument("--project", required=True, help="待检视目录")
    parser.add_argument(
        "--stages",
        default=",".join(STAGE_NAMES),
        help=f"执行的阶段，逗号分隔，可选 {list(STAGE_NAMES)}；不含 arch 时复用已有分析产物",
    )
    parser.add_argument("--modules", default="", help="待检视模块编号，逗号分隔；留空表示全部")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        metavar="N",
        help="小模块的并发数：0=自动（min(模块数, 4)），1=串行，N>1=并行上限",
    )
    parser.add_argument(
        "--large-concurrency",
        type=int,
        default=DEFAULT_LARGE_CONCURRENCY,
        metavar="N",
        help=(
            "大模块的并发数。大模块先分解再逐块检视，一次调用要处理二十来条意见，"
            f"并发开大了容易触发限流；默认 {DEFAULT_LARGE_CONCURRENCY}"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SEC",
        help="模型调用超时（秒），同时作用于首 token 等待与 chunk 间隔；0 表示沿用默认 60/30",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="日志等级（默认 INFO）",
    )
    parser.add_argument("--log-file", default="", help="日志文件路径；留空为 agent2/output/agent2.log")
    return parser


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _build_config(args: argparse.Namespace) -> RunConfig:
    timeouts = (
        {"first_token_timeout": args.timeout, "chunk_timeout": args.timeout}
        if args.timeout > 0
        else {}
    )
    return RunConfig(
        project_root=args.project,
        stages=_split(args.stages),
        modules=_split(args.modules),
        concurrency=args.concurrency,
        **timeouts,
    )


def _check_llm(config: RunConfig) -> bool:
    """启动前置检查：以当前生效模型发起一次最小调用。"""
    try:
        reply = stream_llm_once(_CONNECTIVITY_PROMPT, **config.llm_options())
    except Exception as exc:  # noqa: BLE001 - 任何调用失败都归为「连不通」
        logger.error("模型连通性测试失败: %s: %s", type(exc).__name__, exc)
        return False
    logger.info("模型连通性测试通过: 回复=%r", str(reply).strip()[:40])
    return True


def _run_scan(config: RunConfig) -> None:
    stage_token = current_stage.set(_SCAN_STAGE_LABEL)
    try:
        build_scan_graph().invoke({"config": config})
    finally:
        current_stage.reset(stage_token)


def _select_modules(config: RunConfig) -> list[dict]:
    """读扫描产物并按 `--modules` 筛选；清单缺失、筛不出模块、或与目录对不上即报错。"""
    inventory = read_json(config.inventory_path, default=None)
    if not isinstance(inventory, list) or not inventory:
        raise ValueError(
            f"没有可检视的模块：模块清单缺失或为空（{config.inventory_path}）。请先执行 arch 阶段"
        )
    selected = (
        inventory
        if not config.modules
        else [module for module in inventory if module["id"] in set(config.modules)]
    )
    unknown = sorted(set(config.modules) - {module["id"] for module in selected})
    if unknown:
        raise ValueError(f"模块编号不存在: {unknown}，可选: {[m['id'] for m in inventory]}")

    # 清单里的路径必须真的存在于本次的待检视目录：否则说明清单是对另一个目录扫描出来的，
    # 此时若继续，读不到源码的模块会被当成空文件送进模型检视，产出无意义的结论。
    missing = [
        name
        for module in selected
        for name in module["files"]
        if not (Path(config.project_root) / name).is_file()
    ]
    if missing:
        raise ValueError(
            f"模块清单与待检视目录不匹配：{config.project_root} 下找不到 {missing}。"
            "请对当前目录重新执行 arch 阶段"
        )
    return selected


def _is_large(module: dict) -> bool:
    """按行数分流：超过阈值走大模块分支（先分解再并行检视）。"""
    return module["lines"] > SMALL_MODULE_LINE_LIMIT


def _record_failure(config: RunConfig, module_id: str, reason: str) -> str:
    """记日志并把失败原因落盘，供事后复盘该模块留下的半截产物。"""
    logger.error("模块 %s 检视失败: %s", module_id, reason)
    write_json(config.artifact_path(module_id, "error"), {"module": module_id, "reason": reason})
    return reason


def _review_one(graph, config: RunConfig, module: dict, background: str) -> tuple[str, str]:
    """跑一个模块的检视图，返回 (模块编号, 失败原因)；成功时失败原因为空串。

    `background` 是扫描阶段整理好的「本模块用到的其他模块的符号」，
    它一直是各节点的预留段，现在由这里填进去。
    """
    module_id = module["id"]
    # 工作线程不会自动继承调用方的上下文，模块编号要在本线程内设置，日志才带得出来
    stage_token = current_stage.set(module_id)
    try:
        result = graph.invoke({"config": config, "module": module, "background": background})
    except Exception as exc:  # noqa: BLE001 - 单个模块失败不该中断其余模块
        return module_id, _record_failure(config, module_id, f"{type(exc).__name__}: {exc}")
    finally:
        current_stage.reset(stage_token)

    if result.get("failed"):
        return module_id, _record_failure(
            config, module_id, str(result.get("fail_reason") or "格式校验不通过")
        )
    incomplete = result.get("incomplete_chunks") or []
    if incomplete:
        # 报告仍然产出了，但这部分代码没被检视过——如实记为失败，不让它悄悄过去
        return module_id, _record_failure(config, module_id, "子块未完成检视: " + "；".join(incomplete))
    return module_id, ""


def _review_batch(graph, config: RunConfig, batch: list[dict], workers: int, backgrounds: dict) -> dict[str, str]:
    """跑一批模块，返回失败清单。"""
    def one(module: dict):
        return _review_one(graph, config, module, backgrounds.get(module["id"], ""))

    if workers <= 1:
        outcomes = [one(module) for module in batch]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(one, batch))
    return {module_id: reason for module_id, reason in outcomes if reason}


def _run_reviews(config: RunConfig, modules: list[dict], large_concurrency: int) -> dict[str, str]:
    """执行模块检视，返回 {模块编号: 失败原因}；成功的模块不出现在结果里。

    **分两段调度**：所有小模块先并行跑完，再让大模块按 `--large-concurrency` 成批跑。
    小模块多而轻、大模块少而重；混在一起时重的那几个会长时间占着名额，小模块全在
    后面排队，而它们本来几秒就能出结果。分开之后小模块的总时延只与自己有关。
    """
    small = [module for module in modules if not _is_large(module)]
    large = [module for module in modules if _is_large(module)]
    backgrounds = read_json(config.dependencies_path, default={}) or {}

    failed: dict[str, str] = {}
    if small:
        workers = config.worker_count(len(small))
        logger.info("第一段：小模块 %d 个，并发 %d", len(small), workers)
        failed.update(_review_batch(build_review_graph(), config, small, workers, backgrounds))
    if large:
        logger.info(
            "第二段：大模块 %d 个（%s），并发 %d",
            len(large), "、".join(module["id"] for module in large), large_concurrency,
        )
        failed.update(
            _review_batch(build_large_review_graph(), config, large, max(large_concurrency, 1), backgrounds)
        )
    return failed


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    set_log_level(args.log_level)
    if args.log_file:
        set_log_file(args.log_file)

    try:
        config = _build_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    logger.info(
        "启动: 目录=%s 阶段=%s 模块=%s",
        config.project_root,
        ",".join(config.stages),
        ",".join(config.modules) or "全部",
    )

    if not _check_llm(config):
        return EXIT_PRECONDITION

    if "arch" in config.stages:
        try:
            _run_scan(config)
        except Exception as exc:  # noqa: BLE001 - 扫描失败没有可继续的基础，直接结束
            logger.error("扫描失败: %s: %s", type(exc).__name__, exc)
            print(f"错误: 扫描失败: {exc}", file=sys.stderr)
            return EXIT_PRECONDITION

    if "review" not in config.stages:
        logger.info("阶段选择不含 review，执行结束")
        return EXIT_OK

    try:
        modules = _select_modules(config)
    except ValueError as exc:
        logger.error("%s", exc)
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_PRECONDITION

    failed = _run_reviews(config, modules, args.large_concurrency)
    if failed:
        for module_id, reason in sorted(failed.items()):
            print(f"模块 {module_id} 检视失败: {reason}", file=sys.stderr)
        return EXIT_MODULE_FAILED

    logger.info("全部模块检视完成: 模块数=%d", len(modules))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
