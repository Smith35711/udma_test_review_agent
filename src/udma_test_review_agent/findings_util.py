"""finding 锚点身份、缺陷族、聚类与一致性判定。

本模块是**纯函数 + 字典进出**的确定性逻辑层：不依赖 pydantic、不读盘、不调 LLM。
检视流水线（`nodes/llm/review.py`、`nodes/tool/review_check.py`）与离线评测脚本
（`scripts/eval_review.py`）共用同一套判定，避免「代码里合并规则一套、打分规则另一套」。

为什么区分「锚点」与「缺陷」
---------------------------
实测数据否定了「同一行 = 同一缺陷」的假设。`test_project` 里：

- `buffer.c:7` 一条 `strcpy(dst, src);` 上，模型同时给出两类主张——
  「无界写入」（memory，可单看该行证实）与「信任面入口未校验」（trust_boundary，
  需调用方证据）；后者被反驳是**正确的**，因为该工程 `call_graph` 为空。
- `parser.c:13` 一条 `memcpy(records[i].name, data, 8);` 上同时给出三类**都成立**的
  不同缺陷：源指针不推进（logic）、缺 `data_len` 校验导致越界读（trust_boundary）、
  `char name[8]` 填满无 NUL 终止（memory）。

因此：

- **锚点身份是可达的**（`file` + 行号 + 源文本指纹），与模型措辞无关，且源码一变就变；
- **缺陷身份是不可达的**——「这两条是不是同一个 bug」是模型的语义判断，逐次运行不一致。
  只能退而求其次，用**闭集** `defect_kind` 让模型在固定词表内作答。

聚类据此分两层：先按**锚点**分组（用于展示归并与矛盾检测），组内再按
`(锚点, defect_kind)` 判定是否为同一主张。**跨缺陷族的条目绝不互相合并**——
按行号强行合并会把 `parser.c:13` 的三个真实缺陷压成一个、丢掉两个，那是净损失。

跨运行匹配的容差
----------------
实测同一缺陷的行号会漂移 ±1（`buffer.c` 的未检查 `malloc` 在六次运行中报在 11 与 12），
故容差放在**匹配器**里（`anchors_match`），不放进 id——id 必须稳定且可读。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

# ---------------------------------------------------------------- 严重等级

SEVERITY_ORDER = {"致命": 0, "严重": 1, "一般": 2, "提示": 3}
"""由高到低。未知等级排在最后，不参与「最严重」竞选。"""

_SEVERITY_ALIASES = {
    "critical": "致命", "fatal": "致命", "blocker": "致命",
    "high": "严重", "major": "严重", "error": "严重",
    "medium": "一般", "moderate": "一般", "warning": "一般",
    "low": "提示", "minor": "提示", "info": "提示", "note": "提示",
}


def norm_severity(value: Any) -> str:
    """把自由书写的严重等级收敛到「致命/严重/一般/提示」；无法识别时回退「一般」。"""
    text = str(value or "").strip()
    if not text:
        return "一般"
    if text in SEVERITY_ORDER:
        return text
    lowered = text.lower()
    if lowered in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[lowered]
    for alias, canonical in _SEVERITY_ALIASES.items():
        if alias in lowered:
            return canonical
    return "一般"


def severity_rank(value: Any) -> int:
    """严重等级排序键，越小越严重。

    取值为「无法识别」时落到「一般」（rank 2）——`norm_severity` 已经把未知等级收敛到
    四档之内，故 `SEVERITY_ORDER.get` 的兜底值 9 实际不可达，仅作防御。
    """
    return SEVERITY_ORDER.get(norm_severity(value), 9)


def worst_severity(values: Iterable[Any]) -> str:
    """取一组等级中最严重者；空集回退「一般」。"""
    items = [norm_severity(v) for v in values]
    return min(items, key=severity_rank) if items else "一般"


# ---------------------------------------------------------------- 缺陷族与缺陷种类

CATEGORY_FAMILIES = ("memory", "ub", "resource", "trust_boundary", "concurrency", "logic", "macro")
"""与 `schemas.ChecklistItem.category` 同一套枚举，供展示与评分分组。"""

_FAMILY_SYNONYMS = {
    "memory": "memory", "mem": "memory", "buffer": "memory", "bounds": "memory",
    "overflow": "memory", "use_after_free": "memory", "uaf": "memory",
    "double_free": "memory", "dangling": "memory", "uninit": "memory",
    "uninitialized": "memory", "内存": "memory", "越界": "memory", "溢出": "memory",
    "ub": "ub", "undefined": "ub", "undefined_behavior": "ub", "未定义": "ub",
    "resource": "resource", "fd": "resource", "file_io": "resource", "handle": "resource",
    "leak": "resource", "descriptor": "resource", "资源": "resource", "泄漏": "resource",
    "trust_boundary": "trust_boundary", "boundary": "trust_boundary", "trust": "trust_boundary",
    "input": "trust_boundary", "validation": "trust_boundary", "信任面": "trust_boundary",
    "concurrency": "concurrency", "thread": "concurrency", "race": "concurrency",
    "locking": "concurrency", "并发": "concurrency", "线程": "concurrency",
    "logic": "logic", "correctness": "logic", "逻辑": "logic",
    "macro": "macro", "preprocessor": "macro", "宏": "macro",
}

_FAMILY_KEYWORDS = (
    ("memory", ("memory", "buffer", "bounds", "overflow", "heap", "stack", "内存", "越界", "释放")),
    ("ub", ("undefined", "uninit", "未定义", "未初始化")),
    ("resource", ("resource", "leak", "descriptor", "文件描述", "资源")),
    ("trust_boundary", ("trust", "boundary", "input", "信任面", "外部输入")),
    ("concurrency", ("concurren", "thread", "race", "lock", "并发", "线程", "同步")),
    ("logic", ("logic", "correct", "逻辑")),
    ("macro", ("macro", "preprocess", "宏")),
)

DEFECT_KINDS = (
    "unbounded_write", "no_nul_termination", "index_not_advanced", "unchecked_alloc",
    "missing_release", "double_free", "use_after_free", "null_deref", "integer_overflow",
    "missing_error_check", "race", "path_trust", "unsafe_macro", "other",
)
"""缺陷种类闭集。**闭集是这套判定的前提**——自由文本标题跨运行必然不稳
（实测同一处 `strcpy` 缺陷在六次运行里产出 7 个不同标题、3 个不同 category），
只有在固定词表内作答，`(锚点, 种类)` 才具备可比性。

`other` 是逃生舱：模型无法归类时必须显式选它，而不是自造标签——
自造标签会让两条同族条目在种类上不相等，等于退回「永不合并」。
"""

_KIND_SYNONYMS = {
    "buffer_overflow": "unbounded_write", "overflow": "unbounded_write",
    "strcpy": "unbounded_write", "unbounded_copy": "unbounded_write",
    "no_nul": "no_nul_termination", "missing_nul": "no_nul_termination",
    "not_terminated": "no_nul_termination", "missing_terminator": "no_nul_termination",
    "index_advanced": "index_not_advanced", "pointer_not_advanced": "index_not_advanced",
    "malloc_unchecked": "unchecked_alloc", "unchecked_malloc": "unchecked_alloc",
    "leak": "missing_release", "resource_leak": "missing_release", "no_free": "missing_release",
    "double_free": "double_free", "uaf": "use_after_free", "use_after_free": "use_after_free",
    "dangling": "use_after_free", "null_deref": "null_deref", "nullptr": "null_deref",
    "int_overflow": "integer_overflow", "size_overflow": "integer_overflow",
    "err_check": "missing_error_check", "unchecked_return": "missing_error_check",
    "race": "race", "data_race": "race", "thread_unsafe": "race",
    "trust": "path_trust", "boundary": "path_trust", "input_validation": "path_trust",
    "macro": "unsafe_macro",
}


def category_family(value: Any) -> str:
    """把模型自由书写的类别收敛到固定族；无法识别时回退 `logic`。"""
    text = _norm_token(value)
    if not text:
        return "logic"
    if text in CATEGORY_FAMILIES:
        return text
    if text in _FAMILY_SYNONYMS:
        return _FAMILY_SYNONYMS[text]
    for family, needles in _FAMILY_KEYWORDS:
        if any(needle in text for needle in needles):
            return family
    return "logic"


def defect_kind(finding: dict[str, Any]) -> str:
    """取缺陷种类；模型未给或不在闭集内时，回退到**族限定的哨兵** `family:<族>`。

    回退值刻意保留族名而不是映射到某个具体种类：实测 M03 的 `f2`(ub) 与 `f6`(logic)
    都落在 `resource.c:12`，若二者都回退成 `other` 就会被判为同一主张而合并——
    两个不同性质的缺陷丢掉一个。`family:<族>` 保证不同族永不因缺字段而互相合并。
    """
    raw = _norm_token(finding.get("defect_kind"))
    if raw in DEFECT_KINDS:
        return raw
    if raw in _KIND_SYNONYMS:
        return _KIND_SYNONYMS[raw]
    return f"family:{category_family(finding.get('category'))}"


# ---------------------------------------------------------------- 锚点身份


def line_fingerprint(source_line: str) -> str:
    """源文本指纹：8 位十六进制，对空白归一化后的行文本取 sha1。

    用**源码**而非模型措辞计算，因此模型措辞变化不影响锚点；源码一改锚点即变，
    这正是基线比对需要的语义。
    """
    text = " ".join(str(source_line or "").split())
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def anchor_id(file: str, line: int, fingerprint: str = "") -> str:
    """锚点 id：`<file>:<行号>#<指纹>`；无指纹时退化为 `<file>:<行号>`。

    **行号必须留在 id 内**：`resource.c:20` 与 `resource.c:21` 都是 `free(p);`，
    纯内容哈希会在这个恰好最关键的 double free 上相撞。
    """
    file_ = str(file or "?")
    line_ = _as_int(line)
    return f"{file_}:{line_}#{fingerprint}" if fingerprint else f"{file_}:{line_}"


def anchors_match(left: dict[str, Any], right: dict[str, Any], *, tolerance: int = 1) -> bool:
    """判断两条 finding 是否指向**同一处语句**。

    优先比指纹（精确且不受行号漂移影响）；任一侧缺指纹时退回行号邻近判定。
    容差默认 ±1，源自实测的行号漂移（`buffer.c` 未检查 `malloc` 报在 11 与 12）。
    """
    if str(left.get("file") or "?") != str(right.get("file") or "?"):
        return False
    left_fp = str(left.get("anchor_fingerprint") or "")
    right_fp = str(right.get("anchor_fingerprint") or "")
    if left_fp and right_fp:
        return left_fp == right_fp
    return abs(_as_int(left.get("line")) - _as_int(right.get("line"))) <= tolerance


def group_by_anchor(findings: list[dict[str, Any]], *, tolerance: int = 0) -> list[list[int]]:
    """按锚点分组，返回**索引分组**；组内保持输入顺序，组间按 (文件, 行号) 升序。

    贪心：与组内任一成员匹配即入组。锚点相同的条目在展示上归并、在一致性上互相参照。

    默认 `tolerance=0`（精确锚点），**这是刻意的**：单次运行内，模型报在 `buffer.c:11`
    与 `buffer.c:12` 的两条是它有意分别指认的两条语句，应按两条处理。若在此处放宽容差，
    密集区域会链式并组（10 连 11、11 连 12 ⇒ 三行并成一组），把不同语句混为一谈。
    跨运行的同一缺陷才有 ±1 漂移问题，容差只应出现在**跨运行匹配**里（评测脚本调用
    `anchors_match(..., tolerance=1)`），不应参与运行内的归并。
    """
    order = sorted(
        range(len(findings)),
        key=lambda i: (str(findings[i].get("file") or "?"), _as_int(findings[i].get("line"))),
    )
    groups: list[list[int]] = []
    for index in order:
        for group in groups:
            if any(anchors_match(findings[index], findings[member], tolerance=tolerance) for member in group):
                group.append(index)
                break
        else:
            groups.append([index])
    return groups


def claim_key(finding: dict[str, Any]) -> tuple[str, str]:
    """同一主张的判定键：`(锚点, 缺陷种类)`。锚点缺指纹时退化为 `file:line` 文本。"""
    anchor = str(finding.get("anchor") or "") or anchor_id(
        finding.get("file"), _as_int(finding.get("line")), str(finding.get("anchor_fingerprint") or "")
    )
    return anchor, defect_kind(finding)


def dedup_claims(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把**同一锚点且同一种类**的条目合并为一条，其余原样保留。

    替换原先的 `(file, line // 10, category)` 键。旧键有两处错：`//10` 让相差 1 行的
    同一缺陷跨桶分裂、也让相邻的不同缺陷被吞并；含 `category` 又让同一缺陷换个标签
    就不能合并。新键在锚点内按闭集种类区分，两个方向都不误伤。
    """
    merged: list[dict[str, Any]] = []
    index_of: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = claim_key(finding)
        slot = index_of.get(key)
        if slot is None:
            index_of[key] = len(merged)
            merged.append(finding)
            continue
        merged[slot] = merge_same_claim([merged[slot], finding])
    return merged


def merge_same_claim(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """合并同一锚点、同一种类的多条条目（不改动入参）。

    规范条目选取顺序：严重度最高 → 根因描述最长 → 原序最前。
    证据取并集去重，并保留全部原始 id 与来源批次以便回溯。
    """
    if not claims:
        raise ValueError("claims 不能为空")
    canonical = dict(claims[0])
    for candidate in claims[1:]:
        if _better(candidate, canonical):
            canonical = dict(candidate)

    evidence: list[str] = []
    for item in claims:
        for text in _texts(item.get("evidence")):
            if text not in evidence:
                evidence.append(text)
    origin_ids = [str(item.get("id") or "") for item in claims if item.get("id")]
    passes = sorted({str(item.get("source_pass") or "") for item in claims} - {""})

    if evidence:
        canonical["evidence"] = " ｜ ".join(evidence)
    canonical["severity"] = worst_severity(item.get("severity") for item in claims)
    canonical["merged_from"] = origin_ids if len(origin_ids) > 1 else []
    canonical["source_pass"] = "+".join(passes)
    return canonical


def _better(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    cand_rank = severity_rank(candidate.get("severity"))
    inc_rank = severity_rank(incumbent.get("severity"))
    if cand_rank != inc_rank:
        return cand_rank < inc_rank
    return len(str(candidate.get("root_cause") or "")) > len(str(incumbent.get("root_cause") or ""))


# ---------------------------------------------------------------- 一致性判定

_CONCLUSION_RANK = {"confirmed": 0, "uncertain": 1, "refuted": 2}


def resolve_conclusions(conclusions: Iterable[str]) -> tuple[str, bool]:
    """把**同一主张**的多条结论收敛为一条，返回 `(结论, 是否矛盾)`。

    规则（刻意保守）：
    - 全部一致 → 该结论；
    - confirmed 与 refuted 同时出现 → `uncertain` 且标记矛盾：两个方向的强主张冲突，
      静默择一会同时带来假阳性与漏报两种风险，交人工裁决更诚实；
    - confirmed 与 uncertain → `confirmed`（有证据强于无证据）；
    - refuted 与 uncertain → `refuted`（反驳按提示词契约须给出具体反证，强于「未能判定」）。

    注意本函数只应在**同一主张**内调用（见 `claim_key`）。不同缺陷族的条目共用锚点时
    并不矛盾，对它们调用本函数会把「三个都成立的不同缺陷」压成一个。
    """
    known = [c for c in conclusions if c in _CONCLUSION_RANK]
    if not known:
        return "uncertain", False
    unique = set(known)
    if len(unique) == 1:
        return known[0], False
    if {"confirmed", "refuted"} <= unique:
        return "uncertain", True
    # 逐对显式判定，不用「按 rank 取最小」代劳：rank 的次序是为了排序（confirmed 在前），
    # 拿它当"哪条结论更强"用，在 refuted 与 uncertain 上会得出与契约相反的答案
    # （refuted 按提示词契约须给出具体反证，强于「未能判定」，应胜出）。
    if unique == {"confirmed", "uncertain"}:
        return "confirmed", False
    if unique == {"refuted", "uncertain"}:
        return "refuted", False
    return "uncertain", False


def _norm_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _texts(value: Any) -> list[str]:
    """把 evidence 字段拆成去重前的候选文本列表（支持字符串或列表）。"""
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(" ｜ ") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]
