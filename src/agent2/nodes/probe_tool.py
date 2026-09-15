"""证据查询（确定性）：执行反驳节点提出的取证请求。

查询类型是**闭集**，全部在符号表、调用图与源码上完成，**不调用模型**——
模型只能点菜，不能自己编证据。

每类查询有条数上限，每轮证据有总量上限：大模块分支存在的全部理由就是控制上下文，
一次检索命中几千行就把第二轮撑爆了。超限一律**标明已截断**，不假装完整。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..artifacts import read_source_slices, read_text, write_json
from ..log import get_logger
from ..schemas import PROBE_KINDS
from ..state import ReviewState

logger = get_logger(__name__)

MAX_QUERIES_PER_ROUND = 12
MAX_RESULTS_PER_QUERY = 20
MAX_EVIDENCE_CHARS = 8000
"""一轮证据的总字符上限。"""


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _callers(target: str, context: dict) -> list[str]:
    return [
        f"{edge['file']}:{edge['line']}  {edge['caller']} 调用 {target}"
        for edge in context["graph"]["edges"]
        if edge["callee"] == target
    ]


def _callees(target: str, context: dict) -> list[str]:
    return [
        f"{edge['file']}:{edge['line']}  {target} 调用 {edge['callee']}"
        for edge in context["graph"]["edges"]
        if edge["caller"] == target
    ]


def _accessors(target: str, context: dict) -> list[str]:
    """列出这个标识符在函数体里出现过的每一处，连行与所在函数一起给出。

    只给出现位置与整行源码，不替模型判断是读还是写——那一行的内容足以看清。
    """
    pattern = re.compile(rf"\b{re.escape(target)}\b")
    hits = []
    for function in context["functions"]:
        lines = _lines(read_text(context["root"] / function["file"]))
        for number in range(function["line"], min(function["end_line"], len(lines)) + 1):
            if pattern.search(lines[number - 1]):
                hits.append(f"{function['file']}:{number}（函数 {function['name']}）  {lines[number - 1].strip()}")
    return hits


def _definition(target: str, context: dict) -> list[str]:
    pattern = re.compile(rf"\b(?:typedef|struct|union|enum)\b.*\b{re.escape(target)}\b")
    hits = []
    for name in context["module"]["files"]:
        lines = _lines(read_text(context["root"] / name))
        for index, line in enumerate(lines):
            if pattern.search(line) or re.match(rf"^\s*\}}?\s*{re.escape(target)}\s*;", line):
                hits.append(f"{name}:{index + 1}  " + " ".join(lines[index : index + 10])[:240])
                break
    return hits


def _source(target: str, context: dict) -> list[str]:
    for function in context["functions"]:
        if function["name"] == target:
            return [
                read_source_slices(
                    context["root"],
                    [{"file": function["file"], "start": function["line"], "end": function["end_line"]}],
                )
            ]
    return []


def _search(target: str, context: dict) -> list[str]:
    """受控全文检索。闭集查询答不了「这个常量还在哪里被改过」这类问题，靠它兜底。"""
    try:
        pattern = re.compile(target)
    except re.error as exc:
        return [f"（检索模式不合法，无法执行：{target}（{exc}））"]
    hits = []
    for name in context["module"]["files"]:
        for index, line in enumerate(_lines(read_text(context["root"] / name)), start=1):
            if pattern.search(line):
                hits.append(f"{name}:{index}  {line.strip()[:160]}")
                if len(hits) > MAX_RESULTS_PER_QUERY:
                    return hits
    return hits


def _macro(target: str, context: dict) -> list[str]:
    hits = []
    for name in context["module"]["files"]:
        lines = _lines(read_text(context["root"] / name))
        for index, line in enumerate(lines):
            if re.match(rf"^\s*#\s*define\s+{re.escape(target)}\b", line):
                hits.append(f"{name}:{index + 1}  {line.strip()[:200]}")
    return hits


_HANDLERS = {
    "callers": _callers,
    "callees": _callees,
    "accessors": _accessors,
    "definition": _definition,
    "source": _source,
    "search": _search,
    "macro": _macro,
}


def _run_query(query: dict, context: dict) -> dict:
    kind = str(query.get("kind") or "")
    target = str(query.get("target") or "")
    handler = _HANDLERS.get(kind)
    if handler is None:
        # 认不出的类型当作可读反馈回给模型：第二轮它还有机会改
        return {
            "kind": kind,
            "target": target,
            "result": f"（不支持的查询类型 {kind!r}，可用：{' / '.join(PROBE_KINDS)}）",
            "truncated": False,
        }
    hits = handler(target, context)
    limited = hits[:MAX_RESULTS_PER_QUERY]
    return {
        "kind": kind,
        "target": target,
        "result": "\n".join(limited) if limited else "（没有命中）",
        "truncated": len(hits) > len(limited),
    }


def probe_tool(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    symbols = state["symbols"]

    queries = (state.get("probe_queries") or [])[:MAX_QUERIES_PER_ROUND]
    context = {
        "root": Path(config.project_root),
        "module": module,
        "graph": state["call_graph"],
        "functions": symbols["interfaces"] + symbols["internals"],
    }

    results, used = [], 0
    for query in queries:
        result = _run_query(query, context)
        used += len(result["result"])
        if used > MAX_EVIDENCE_CHARS:
            # 超预算就整体标截断并停手：与其悄悄只给一半，不如说清是被预算切掉的
            result = dict(result, result=result["result"][: max(MAX_EVIDENCE_CHARS - used, 0)], truncated=True)
            results.append(result)
            break
        results.append(result)

    evidence = list(state.get("probe_evidence") or [])
    evidence.append({"round": state.get("probe_rounds", 0), "queries": queries, "results": results})
    write_json(config.artifact_path(module["id"], "probe_rounds"), evidence)
    logger.info(
        "探测取证: 模块=%s 第 %d 轮 查询=%d 条 证据=%d 字符",
        module["id"], state.get("probe_rounds", 0), len(results), used,
    )
    return {"probe_evidence": evidence, "probe_queries": []}
