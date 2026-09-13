"""外部静态分析工具执行层：gcc -fanalyzer 与 cppcheck。

**定位是补充，不是替代。** 实测这两个工具合起来只能覆盖 `test_project` 18 项已知缺陷
中的 6 项，而其中最重要的一项——`buffer.c:7` 的无界 `strcpy`——两个工具都抓不到
（目标缓冲区容量需跨函数推断）。因此：

- 有工具报告 ⇒ 该位置确实有问题（可作为硬证据）；
- **没有**工具报告 ⇏ 没问题（绝不可用作证伪依据）。

整个模块是**尽力而为**：被测项目不可编译、工具未安装、超时，一律记录原因后跳过，
绝不让检视流程失败。设计文档明确「不依赖编译」，本层不改变这一前提。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ... import findings_util as fu
from ...log import get_logger
from ...schemas import ScanResult, ToolEvidence

logger = get_logger(__name__)

TOOL_TIMEOUT = 90.0
"""单个编译单元/单次 cppcheck 调用的超时（秒）。超时即跳过该工具。"""

GCC_LINE_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?:warning|error):\s*(?P<msg>.+)$"
)
"""gcc 诊断行：`file:line:col: warning: message [CWE-nnn] [-Wrule]`。

只取这一类「主诊断行」，note 与事件追溯行（缩进的 `| 11 | ...`）一概丢弃——
它们是同一条诊断的展开，收进来会让同一缺陷重复计数。
"""

_RULE_RE = re.compile(r"\[-W(?P<rule>[\w-]+)\]\s*$")
_CWE_RE = re.compile(r"\[CWE-(?P<cwe>\d+)\]")

CPPCHECK_LINE_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):(?P<severity>[a-z]+):(?P<id>[\w-]+):(?P<msg>.*)$"
)


@dataclass(frozen=True)
class RuleMapping:
    """工具规则到本项目分类体系的映射。"""

    category: str
    defect_kind: str
    severity: str


RULE_MAPPINGS: tuple[tuple[str, RuleMapping], ...] = (
    # 内存安全 —— 名称匹配在前（gcc 的 -Wanalyzer-* 与 cppcheck 的 id 都命中）
    ("use-after-free", RuleMapping("memory", "use_after_free", "致命")),
    ("useafterfree", RuleMapping("memory", "use_after_free", "致命")),
    ("deallocuse", RuleMapping("memory", "use_after_free", "致命")),
    ("double-free", RuleMapping("memory", "double_free", "致命")),
    ("doublefree", RuleMapping("memory", "double_free", "致命")),
    ("bufferoverflow", RuleMapping("memory", "unbounded_write", "致命")),
    ("out-of-bounds", RuleMapping("memory", "unbounded_write", "严重")),
    ("arrayindexoutofbounds", RuleMapping("memory", "unbounded_write", "严重")),
    ("possible-null-dereference", RuleMapping("memory", "null_deref", "严重")),
    ("nullpointer", RuleMapping("memory", "null_deref", "严重")),
    ("dangling", RuleMapping("memory", "use_after_free", "严重")),
    ("memleak", RuleMapping("resource", "missing_release", "一般")),
    # 资源
    ("file-leak", RuleMapping("resource", "missing_release", "严重")),
    ("resourceleak", RuleMapping("resource", "missing_release", "严重")),
    ("fd-leak", RuleMapping("resource", "missing_release", "严重")),
    ("malloc-leak", RuleMapping("resource", "missing_release", "一般")),
    # 未定义行为
    ("uninit", RuleMapping("ub", "other", "严重")),
    ("uninitialized", RuleMapping("ub", "other", "严重")),
    ("integer-overflow", RuleMapping("ub", "integer_overflow", "严重")),
    # 信任边界
    ("possible-null-argument", RuleMapping("trust_boundary", "missing_error_check", "严重")),
    ("unchecked-return", RuleMapping("trust_boundary", "missing_error_check", "一般")),
    ("ignoredreturnvalue", RuleMapping("trust_boundary", "missing_error_check", "一般")),
    # CWE 兜底：部分非 analyzer 告警只带 CWE 编号而无规则名
    ("cwe-416", RuleMapping("memory", "use_after_free", "致命")),
    ("cwe-415", RuleMapping("memory", "double_free", "致命")),
    ("cwe-690", RuleMapping("memory", "null_deref", "严重")),
    ("cwe-476", RuleMapping("memory", "null_deref", "严重")),
    ("cwe-787", RuleMapping("memory", "unbounded_write", "致命")),
    ("cwe-125", RuleMapping("memory", "unbounded_write", "严重")),
    ("cwe-401", RuleMapping("resource", "missing_release", "一般")),
    ("cwe-775", RuleMapping("resource", "missing_release", "严重")),
    ("cwe-190", RuleMapping("ub", "integer_overflow", "严重")),
    ("cwe-457", RuleMapping("ub", "other", "严重")),
)


def map_rule(rule: str) -> RuleMapping | None:
    """把工具规则名映射到 `(类别, 缺陷种类, 严重度)`；无法识别时返回 None。

    **返回 None 的规则只作为「印证」证据存在，不用于注入新 finding。**
    宁可不补漏，也不给一条自己都归不了类的工具输出编造类别——那会污染 finding 词表，
    而 finding 的类别是后续聚类、去重的键。
    """
    token = (rule or "").strip().lower()
    if not token:
        return None
    for needle, mapping in RULE_MAPPINGS:
        if needle in token:
            return mapping
    return None


def _severity_from_gcc(msg: str) -> str:
    """gcc 未给出项目等级，按规则映射决定；此处仅作兜底。"""
    return "一般" if "warning" in msg else "严重"


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        logger.warning("[工具] 超时(%.0fs): %s", TOOL_TIMEOUT, " ".join(cmd[:3]))
        return -1, "", f"超时 {TOOL_TIMEOUT:.0f}s"
    except OSError as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _relativize(raw: str, root: Path) -> str:
    """把工具输出的路径转成被测项目内的相对路径（POSIX 分隔符）。"""
    text = raw.strip().replace("\\", "/")
    try:
        return str(Path(text).resolve().relative_to(root)) if Path(text).is_absolute() else text
    except (OSError, ValueError):
        return text


def _gcc_analyzer(scan: ScanResult, c_files: list[str], root: Path) -> tuple[list[ToolEvidence], str | None]:
    """逐编译单元运行 gcc -fanalyzer。返回 (证据列表, 跳过原因或 None)。"""
    gcc = shutil.which("gcc")
    if gcc is None:
        return [], "gcc 未安装"
    flags = [flag for flag in scan.build_flags if flag.startswith(("-I", "-D"))]
    entries: list[ToolEvidence] = []
    failures: list[str] = []
    for rel in c_files:
        cmd = [gcc, "-fanalyzer", "-Wall", "-Wextra", *flags, "-I", str(root), "-c", rel, "-o", "/dev/null"]
        code, _, stderr = _run(cmd, cwd=root)
        found = 0
        for raw in stderr.splitlines():
            match = GCC_LINE_RE.match(raw.strip())
            if not match:
                continue
            msg = match.group("msg").strip()
            rule_match = _RULE_RE.search(msg)
            rule = rule_match.group("rule") if rule_match else "unknown"
            cwe = _CWE_RE.search(msg)
            mapping = map_rule(rule) or map_rule(f"cwe-{cwe.group('cwe')}" if cwe else "")
            entries.append(
                ToolEvidence(
                    tool="gcc-analyzer",
                    file=_relativize(match.group("file"), root),
                    line=int(match.group("line")),
                    rule=rule if rule != "unknown" else (f"CWE-{cwe.group('cwe')}" if cwe else "unknown"),
                    severity=mapping.severity if mapping else _severity_from_gcc(msg),
                    message=msg,
                )
            )
            found += 1
        if found == 0 and code != 0:
            failures.append(rel)
    if failures and not entries:
        return entries, f"gcc -fanalyzer 在 {len(failures)} 个文件上无输出且返回非零（可能缺少头文件或不可编译）：{failures[:3]}"
    if failures:
        logger.warning("[工具] gcc -fanalyzer 在 %d 个文件上未产出诊断: %s", len(failures), failures[:3])
    return entries, None


def _cppcheck(scan: ScanResult, c_files: list[str], root: Path) -> tuple[list[ToolEvidence], str | None]:
    """运行 cppcheck。cppcheck 不需要编译，故在不可编译项目上仍可产出。"""
    cppcheck = shutil.which("cppcheck")
    if cppcheck is None:
        return [], "cppcheck 未安装"
    if not c_files:
        return [], None
    flags = [flag for flag in scan.build_flags if flag.startswith(("-I", "-D"))]
    cmd = [
        cppcheck,
        "--enable=warning,performance,portability",
        "--inline-suppr",
        "--quiet",
        "--suppress=missingIncludeSystem",
        "--template={file}:{line}:{column}:{severity}:{id}:{message}",
        *flags,
        "-I",
        str(root),
        *c_files,
    ]
    code, stdout, stderr = _run(cmd, cwd=root)
    entries: list[ToolEvidence] = []
    for raw in (stdout + "\n" + stderr).splitlines():
        match = CPPCHECK_LINE_RE.match(raw.strip())
        if not match:
            continue
        severity = match.group("severity").strip()
        if severity in {"information", "debug"}:
            continue
        rule = match.group("id").strip()
        mapping = map_rule(rule)
        entries.append(
            ToolEvidence(
                tool="cppcheck",
                file=_relativize(match.group("file"), root),
                line=int(match.group("line")),
                rule=rule,
                severity=mapping.severity if mapping else _cppcheck_severity(severity),
                message=match.group("msg").strip(),
            )
        )
    if code not in (0, 1, 2) and not entries:
        return entries, f"cppcheck 返回码 {code}"
    return entries, None


def _cppcheck_severity(severity: str) -> str:
    return {"error": "严重", "warning": "一般", "performance": "提示", "portability": "提示"}.get(
        severity, "一般"
    )


def _dedup(entries: list[ToolEvidence]) -> list[ToolEvidence]:
    """同一位置同一规则的多次报告只留一条。

    gcc 会同时给出「普通告警」与「analyzer 告警」两条指向同一缺陷的诊断
    （如 `-Wuse-after-free` 与 `-Wanalyzer-use-after-free`），cppcheck 也可能与 gcc
    重复命中同一处。保留严重度最高的一条，避免同一工具缺陷被重复计入。
    """
    best: dict[tuple[str, int, str], ToolEvidence] = {}
    order = {"致命": 0, "严重": 1, "一般": 2, "提示": 3}
    for entry in entries:
        mapping = map_rule(entry.rule)
        key = (entry.file, entry.line, mapping.defect_kind if mapping else entry.rule)
        current = best.get(key)
        if current is None or order.get(entry.severity, 9) < order.get(current.severity, 9):
            best[key] = entry
    return sorted(best.values(), key=lambda e: (e.file, e.line, e.rule, e.tool))


MATCH_TOLERANCE = 1
"""工具证据与 finding 的位置匹配容差。

工具报的行与模型报的行会差 1：`resource.c` 的文件泄漏，gcc 报在 `return -1;`（第 10 行），
而缺陷根源在 `fopen`（第 7 行）。容差放在匹配器里、不放进锚点身份。
"""


def _as_dicts(entries: Iterable[ToolEvidence | dict]) -> list[dict]:
    return [e.model_dump() if isinstance(e, ToolEvidence) else dict(e) for e in entries]


def match_for(
    finding: dict, entries: Iterable[ToolEvidence | dict], *, tolerance: int = MATCH_TOLERANCE
) -> tuple[list[dict], list[dict]]:
    """返回 `(同缺陷种类的证据, 仅同位置的证据)`。

    分两档是因为两者的证明力不同：**同种类**意味着工具独立复现了模型的这条具体判断，
    足以支撑 confirmed；**仅同位置**只说明该语句确实有问题，能阻止「被证伪」，
    但不足以断言模型说的就是那件事。
    """
    file = str(finding.get("file") or "")
    try:
        line = int(finding.get("line") or 0)
    except (TypeError, ValueError):
        return [], []
    if not file or line <= 0:
        return [], []
    kind = fu.defect_kind(finding)
    same_kind: list[dict] = []
    same_place: list[dict] = []
    for entry in _as_dicts(entries):
        if entry.get("file") != file:
            continue
        try:
            distance = abs(int(entry.get("line") or 0) - line)
        except (TypeError, ValueError):
            continue
        if distance > tolerance:
            continue
        mapping = map_rule(str(entry.get("rule") or ""))
        if mapping is not None and mapping.defect_kind == kind:
            same_kind.append(entry)
        else:
            same_place.append(entry)
    return same_kind, same_place


def injectable(entries: Iterable[ToolEvidence | dict]) -> list[tuple[dict, RuleMapping]]:
    """筛出可用于**补漏**的工具证据：每个位置至多一条，取最严重者。

    **只保留能映射到闭集词表的规则。** 归不了类的工具输出只作印证之用——
    finding 的类别与种类是后续聚类、去重、评分的键，编造一个只会污染整套判定。
    同一位置多条（gcc 的 `-Wuse-after-free` 与 `-Wanalyzer-double-free` 会各报一次）
    只取最严重的一条，避免同一处缺陷被注入两次。
    """
    best: dict[tuple[str, int], tuple[dict, RuleMapping]] = {}
    for entry in _as_dicts(entries):
        mapping = map_rule(str(entry.get("rule") or ""))
        if mapping is None:
            continue
        key = (str(entry.get("file") or ""), int(entry.get("line") or 0))
        current = best.get(key)
        if current is None or fu.severity_rank(entry.get("severity")) < fu.severity_rank(
            current[0].get("severity")
        ):
            best[key] = (entry, mapping)
    return [best[key] for key in sorted(best)]


def collect(scan: ScanResult) -> tuple[list[ToolEvidence], list[str], list[str]]:
    """运行全部可用工具，返回 `(证据, 成功运行的工具, 跳过原因)`。"""
    root = Path(scan.project_root)
    c_files = sorted(f.path for m in scan.modules for f in m.files if f.path.endswith(".c"))
    entries: list[ToolEvidence] = []
    available: list[str] = []
    skipped: list[str] = []

    for name, runner in (("gcc-analyzer", _gcc_analyzer), ("cppcheck", _cppcheck)):
        found, reason = runner(scan, c_files, root)
        if reason:
            skipped.append(f"{name}: {reason}")
            logger.warning("[工具] %s 跳过 —— %s", name, reason)
            continue
        available.append(name)
        entries.extend(found)
        logger.info("[工具] %s 完成，诊断 %d 条", name, len(found))

    deduped = _dedup(entries)
    logger.info(
        "[工具] 合计 %d 条（去重前 %d）｜可用工具=%s｜跳过=%s",
        len(deduped),
        len(entries),
        available or "无",
        skipped or "无",
    )
    return deduped, available, skipped
