"""符号表（确定性）：把模块"看小"——对外接口、内部函数、全局变量各一份精简清单。

它是调用图、架构分析与探测取证的公共输入，故必须精简：每项只留名字、类别、
文件:行号与行数，不留函数体（需要原文时按行区间回原文件切）。

解析是文本级近似（局限见 `csymbols` 的模块注释）。认不出的调用计入
`unresolved_calls` 一并落盘——这个数字是判断"分解是不是建立在瞎了眼的解析之上"
的唯一依据，不能省。
"""

from __future__ import annotations

from pathlib import Path

from .. import csymbols
from ..artifacts import read_text, write_json
from ..log import get_logger
from ..state import ReviewState

logger = get_logger(__name__)


def _split_by_suffix(files: list[str], suffix: str) -> list[str]:
    return [name for name in files if name.endswith(suffix)]


def symbol_table_tool(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    root = Path(config.project_root)

    code_files = _split_by_suffix(module["files"], ".c")
    header_files = _split_by_suffix(module["files"], ".h")
    functions, globals_ = csymbols.read_and_parse([root / name for name in code_files], root)

    exported = set()
    macros = set()
    for name in header_files:
        text = read_text(root / name)
        exported |= set(csymbols.parse_declarations(text))
        macros |= set(csymbols.parse_macros(text))
    for name in code_files:
        macros |= set(csymbols.parse_macros(read_text(root / name)))

    known = {function["name"] for function in functions}
    unresolved = 0
    for function in functions:
        text = read_text(root / function["file"])
        unresolved += sum(
            1 for call in csymbols.scan_calls(text, function) if call["callee"] not in known
        )

    def entry(function: dict, kind: str) -> dict:
        return {
            "name": function["name"],
            "kind": kind,
            "file": function["file"],
            "line": function["line"],
            "end_line": function["end_line"],
            "lines": function["lines"],
            "signature": function["signature"],
        }

    symbols = {
        "module": module["id"],
        "files": module["files"],
        "interfaces": [entry(f, "对外接口") for f in functions if f["name"] in exported],
        "internals": [entry(f, "内部函数") for f in functions if f["name"] not in exported],
        "globals": [dict(item, kind="全局变量") for item in globals_],
        "macros": sorted(macros),
        "unresolved_calls": unresolved,
    }
    write_json(config.artifact_path(module["id"], "symbols"), symbols)
    logger.info(
        "符号表: 模块=%s 接口=%d 内部函数=%d 全局变量=%d 宏=%d 未解析调用=%d",
        module["id"],
        len(symbols["interfaces"]),
        len(symbols["internals"]),
        len(symbols["globals"]),
        len(symbols["macros"]),
        unresolved,
    )
    return {"symbols": symbols}
