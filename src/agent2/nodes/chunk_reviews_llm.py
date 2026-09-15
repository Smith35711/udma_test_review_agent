"""子块检视（大模型）：各逻辑子块并行检视，findings 合并。

并发放在本节点内部（与 CLI 层的模块并发是同一套机制），池子是**本节点私有的局部
对象**——绝不能把子块任务提交到外层那个池：外层工作线程在等内层结果时会自锁。

合并顺序按**子块顺序**而不是完成顺序：findings 之后的编号（f1…fN）由此确定，
用完成顺序会让同一份代码两次运行编出不同的号。`pool.map` 天然保序，正合此用。

子块之间互不依赖，所以某一块拿不到结果时只记下它、继续跑其余块——一个子块失败
不该让另一个子块已经检出的问题一起作废。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..artifacts import read_source_slices, write_json
from ..llm_retry import call_llm
from ..log import current_stage, get_logger
from ..prompts.review import LINE_RULE_ABSOLUTE, REVIEW_PROMPT
from ..schemas import SEVERITIES, ReviewResult
from ..state import ReviewState

logger = get_logger(__name__)

CHUNK_WORKERS = 3
"""子块并发上限。与模块级并发（上限 4）叠加后峰值并发有限流风险，用户已知情选择此值。"""


def _chunk_background(module_background: str, chunk: dict, chunk_count: int) -> str:
    """拼出这一块的背景信息：先是"只看得到子块"的说明，再是本模块的外部依赖。

    **外部依赖必须带上**：子块切得越细，越容易看不见跨模块的类型与契约，
    而那份清单是扫描阶段专门为本模块整理好的——丢掉它，切分反而让检视更盲目。
    """
    notes = [
        f"本模块是大模块，已按职责切成 {chunk_count} 个子块；本次只给出其中一个子块"
        f"（{chunk['name']}：{chunk['purpose']}）的源码，模块的其他部分**没有提供**。",
        "因此：只报告你能在本子块源码中确证的问题；子块之外的调用方行为、"
        "全局变量的其他访问点、未给出的类型定义，都不要凭推测下结论。"
        "下面列出的跨模块符号不受此限——它们是已知的契约与类型，可以直接引用。",
    ]
    if module_background:
        notes += ["", module_background]
    return "\n".join(notes)


def _review_one(config, module: dict, chunk: dict, background: str) -> tuple[list[dict], str]:
    """检视一个子块，返回（意见清单，失败原因）。失败不抛出，交给调用方汇总。"""
    token = current_stage.set(f"{module['id']}/{chunk['name']}")
    try:
        prompt = REVIEW_PROMPT.format(
            module_id=f"{module['id']} 的子块「{chunk['name']}」",
            background=background,
            source_code=read_source_slices(config.project_root, chunk["ranges"]),
            severities=" / ".join(SEVERITIES),
            line_rule=LINE_RULE_ABSOLUTE,
        )
        result = call_llm(config, prompt, ReviewResult)
        return [finding.model_dump() for finding in result.findings], ""
    except Exception as exc:  # noqa: BLE001 - 单个子块失败不该作废其余子块的结果
        logger.error(
            "子块检视失败: 模块=%s 子块=%s %s: %s",
            module["id"], chunk["name"], type(exc).__name__, exc,
        )
        return [], f"{chunk['name']}: {type(exc).__name__}: {exc}"
    finally:
        current_stage.reset(token)


def chunk_reviews_llm(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    chunks = state["chunks"]

    findings: list[dict] = []
    failures: list[str] = []
    module_background = state.get("background") or ""
    workers = max(min(len(chunks), CHUNK_WORKERS), 1)
    logger.info(
        "开始子块检视: 模块=%s 子块=%d 并发=%d 外部依赖=%s",
        module["id"], len(chunks), workers,
        f"{len(module_background)} 字符" if module_background else "无",
    )

    if workers <= 1:
        outcomes = [
            _review_one(config, module, chunk, _chunk_background(module_background, chunk, len(chunks)))
            for chunk in chunks
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(
                pool.map(
                    lambda chunk: _review_one(
                        config, module, chunk, _chunk_background(module_background, chunk, len(chunks))
                    ),
                    chunks,
                )
            )

    for index, (chunk_findings, error) in enumerate(outcomes):
        # 逐块落盘：并发写同一个文件会互相截断，故一块一个文件
        write_json(config.chunk_findings_path(module["id"], index), chunk_findings)
        findings.extend(chunk_findings)
        if error:
            failures.append(error)

    write_json(config.artifact_path(module["id"], "raw_findings"), findings)
    logger.info(
        "子块检视完成: 模块=%s 子块=%d 意见=%d 未完成=%d",
        module["id"], len(chunks), len(findings), len(failures),
    )
    return {"findings": findings, "incomplete_chunks": failures}
