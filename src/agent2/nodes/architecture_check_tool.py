"""分解方案的校验与修补（确定性）：模型提议，这里把关。

**硬约束由构造保证，永远通过**：每个函数恰好归属一个子块；模块每个 `.c` 的每一行
都被某个子块覆盖。模型漏认领、多认领、认了不存在的名字，都由本节点修补——
覆盖率不能靠模型自觉。为什么必须覆盖到行：全局变量、typedef、宏、函数之间的空行
都不属于任何函数，而缺陷恰恰常在这些地方（一个全局缓冲的越界就是例子）。

**软约束只报告、不阻断**：每块行数不超阈值、循环调用组不被拆散。达不到就带具体
原因退回重做，最多 2 轮；仍达不到则记下违规、打 WARNING 继续。理由是文本级调用图
本身是近似的，可能造出并不存在的"环"，为一条可能不成立的约束让整个模块拿不到
报告，比带着一条违规记录出报告更糟。
"""

from __future__ import annotations

from pathlib import Path

from ..artifacts import read_text, write_json
from ..configuration import SMALL_MODULE_LINE_LIMIT
from ..log import get_logger
from ..state import ReviewState

logger = get_logger(__name__)

MAX_PLAN_ROUNDS = 2
"""分解方案被退回重做的轮数上限。"""


def _assign_line_owners(functions: list[dict], owner: dict[str, int], total_lines: int) -> list[int | None]:
    """给文件的每一行定归属：函数行归其所在子块，其余顶层行归**下一个**函数所在子块。

    文件开头的顶层行（#include、宏、typedef）归第一个函数所在块；
    文件末尾多出来的行归最后一个块。这样"每个函数恰好一块"之外，
    还顺带保证了行的全覆盖。
    """
    owners: list[int | None] = [None] * (total_lines + 1)
    for function in functions:
        for line in range(function["line"], min(function["end_line"], total_lines) + 1):
            owners[line] = owner[function["name"]]

    following = None
    for line in range(total_lines, 0, -1):  # 未被认领的行：向后（更大行号）找归属
        if owners[line] is None:
            owners[line] = following
        else:
            following = owners[line]

    leading = next((value for value in owners[1:] if value is not None), None)
    for line in range(1, total_lines + 1):  # 文件开头残留的行：归第一个块
        if owners[line] is None:
            owners[line] = leading
        else:
            break
    return owners


def _merge_ranges(owners: list[int | None]) -> dict[int, list[dict]]:
    """把逐行的归属合并成「行区间」清单，按子块序号归类。"""
    ranges: dict[int, list[dict]] = {}
    start = 1
    for line in range(2, len(owners) + 1):
        if line == len(owners) or owners[line] != owners[start]:
            if owners[start] is not None:
                ranges.setdefault(owners[start], []).append({"start": start, "end": line - 1})
            start = line
    return ranges


def _repair_chunks(chunks: list[dict], by_name: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """修补归属：丢弃不存在的名字、同一函数只留第一次出现、漏掉的进「残余」块。"""
    accepted: list[dict] = []
    claimed: set[str] = set()
    unknown: list[str] = []
    for chunk in chunks:
        members = []
        for name in chunk.get("functions") or []:
            if name not in by_name:
                unknown.append(name)
                continue
            if name in claimed:
                continue
            claimed.add(name)
            members.append(name)
        if members:
            accepted.append(
                {
                    "name": chunk.get("name") or f"子块{len(accepted) + 1}",
                    "purpose": chunk.get("purpose") or "",
                    "functions": members,
                }
            )

    residual = [name for name in by_name if name not in claimed]
    if residual:
        accepted.append({"name": "残余", "purpose": "分解方案未认领的函数", "functions": residual})
    if not accepted:
        accepted.append(
            {"name": "整模块", "purpose": "未给出可用方案，整体作为一块", "functions": list(by_name)}
        )
    return accepted, unknown


def architecture_check_tool(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    symbols = state["symbols"]
    root = Path(config.project_root)

    functions = symbols["interfaces"] + symbols["internals"]
    by_name = {function["name"]: function for function in functions}
    chunks, unknown = _repair_chunks(state["architecture"].get("chunks") or [], by_name)
    if unknown:
        logger.warning("分解方案里有符号表之外的名字，已忽略: %s", sorted(set(unknown)))

    owner = {chunk["name"]: index for index, chunk in enumerate(chunks)}
    owner_of_function = {
        name: owner[chunk["name"]] for chunk in chunks for name in chunk["functions"]
    }

    # 按文件算行归属，再合并成区间
    code_files = [name for name in module["files"] if name.endswith(".c")]
    ranges: dict[int, list[dict]] = {}
    for name in code_files:
        total_lines = len(read_text(root / name).splitlines())
        file_functions = [f for f in functions if f["file"] == name]
        owners = _assign_line_owners(file_functions, owner_of_function, total_lines)
        for index, spans in _merge_ranges(owners).items():
            ranges.setdefault(index, []).extend(dict(span, file=name) for span in spans)

    for index, chunk in enumerate(chunks):
        chunk["ranges"] = sorted(ranges.get(index, []), key=lambda item: (item["file"], item["start"]))
        chunk["lines"] = sum(item["end"] - item["start"] + 1 for item in chunk["ranges"])

    # 软约束：只报告，不阻断
    violations = [
        f"子块「{chunk['name']}」{chunk['lines']} 行，超过 {SMALL_MODULE_LINE_LIMIT} 行"
        for chunk in chunks
        if chunk["lines"] > SMALL_MODULE_LINE_LIMIT
    ]
    for group in state["call_graph"]["cycles"]:
        owners = {owner_of_function.get(name) for name in group}
        if len(owners) > 1:
            violations.append(f"循环调用组被拆到了不同子块：{'、'.join(group)}")

    rounds = state.get("plan_rounds", 0)
    if violations and rounds < MAX_PLAN_ROUNDS:
        logger.warning("分解方案退回重做（第 %d/%d 轮）: %s", rounds + 1, MAX_PLAN_ROUNDS, "；".join(violations))
        return {
            "plan_rounds": rounds + 1,
            "plan_pending": True,
            "plan_feedback": "\n".join(f"- {item}" for item in violations),
        }

    if violations:
        logger.warning("分解方案仍有 %d 处违规，记入产物后继续: %s", len(violations), "；".join(violations))

    payload = {
        "module": module["id"],
        "patterns": state["architecture"].get("patterns") or [],
        "summary": state["architecture"].get("summary") or "",
        "strategy": state["architecture"].get("strategy") or "",
        "violations": violations,
        "chunks": chunks,
    }
    write_json(config.artifact_path(module["id"], "chunks"), payload)
    logger.info(
        "分解完成: 模块=%s 子块=%d 个（%s）违规=%d",
        module["id"], len(chunks), "、".join(f"{c['name']}:{c['lines']}行" for c in chunks), len(violations),
    )
    return {"chunks": chunks, "plan_violations": violations, "plan_pending": False, "plan_feedback": ""}
