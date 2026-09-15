"""报告生成（确定性）：把检视与反驳结果格式化为 Markdown 并落盘。

行号在这里渲染成可点击的链接：模型只给相对路径与行号，绝对路径由本节点补，
免得模型去猜文件系统的布局。

**排序只发生在这里**：JSON 产物保持模型给出的原始顺序（编号也按该顺序分配），
展示顺序是渲染时的事。报告按结论分三块——维持在最上、待确认居中、被反驳的最下，
块内再按严重等级降序。全部意见都列出，读者一眼能看出过滤掉了什么。
"""

from __future__ import annotations

from pathlib import Path

from ..artifacts import write_text
from ..log import get_logger
from ..schemas import SEVERITIES
from ..state import ReviewState

logger = get_logger(__name__)

_SEVERITY_INDEX = {name: index for index, name in enumerate(SEVERITIES)}
"""排序键：`SEVERITIES` 里越靠后越严重，故降序排即「严重在前」。"""

_CONCLUSION_BLOCKS = (
    ("维持", "维持"),
    ("待确认", "待确认"),
    ("反驳", "已被反驳"),
)
"""分块顺序与标题。左为 `schemas.CONCLUSIONS` 里的取值，右为报告里显示的标题。"""

_URGENT_SEVERITIES = frozenset(("致命", "严重"))
"""摘要里点名建议优先处理的等级。"""


def _conclusion(finding: dict, rebuttals_by_id: dict) -> str:
    return rebuttals_by_id.get(finding["id"], {}).get("conclusion", "待确认")


def _location(project_root: str, finding: dict) -> str:
    line = finding["line"]
    return f"[{finding['file']}:{line}]({Path(project_root) / finding['file']}#L{line})"


def _code_block(text: str) -> str:
    """四空格缩进即 Markdown 代码块：比围栏稳妥，证据里出现反引号也不会破版。"""
    return "\n".join(f"    {line}" for line in (text.splitlines() or [""]))


def _summary(findings: list[dict], rebuttals_by_id: dict) -> str:
    """摘要由产物直接算出，不经模型：几行统计足以判断要不要往下读。

    用模型写这段的代价不只是多一次调用——它还多一条失败路径，摘要调用超时会让
    整个模块连已验证的 findings 与反驳结论一起拿不出来。
    """
    if not findings:
        return "本次未发现需要报告的问题。"

    conclusions = "、".join(
        f"{title} {sum(1 for f in findings if _conclusion(f, rebuttals_by_id) == name)}"
        for name, title in _CONCLUSION_BLOCKS
    )
    severities = "、".join(
        f"{name} {count}"
        for name in reversed(SEVERITIES)
        if (count := sum(1 for f in findings if f["severity"] == name))
    )
    text = (
        f"本模块共记录 {len(findings)} 条检视意见：{conclusions}；"
        f"严重等级分布为 {severities}。"
    )
    urgent = [
        finding
        for finding in findings
        if finding["severity"] in _URGENT_SEVERITIES
        and _conclusion(finding, rebuttals_by_id) == "维持"
    ]
    if urgent:
        ids = "、".join(finding["id"] for finding in urgent)
        text += f"其中属致命或严重且结论为维持的有 {len(urgent)} 条（{ids}），建议优先处理。"
    return text


def _overview(module: dict, findings: list[dict], rebuttals_by_id: dict) -> list[str]:
    counts = "、".join(
        f"{title} {sum(1 for f in findings if _conclusion(f, rebuttals_by_id) == conclusion)}"
        for conclusion, title in _CONCLUSION_BLOCKS
    )
    return [
        "## 概况",
        "",
        f"- 模块：{module['id']}（{module['name']}）",
        f"- 文件：{'、'.join(module['files'])}",
        f"- 规模：{module['lines']} 行 / {module['chars']} 字符 / 预估 {module['estimated_tokens']} Token",
        f"- 检视意见：{len(findings)} 条（{counts}）",
        "",
    ]


def _finding_section(project_root: str, finding: dict, rebuttal: dict) -> list[str]:
    return [
        f"### {finding['id']} [{finding['severity']}] {finding['description']}",
        "",
        f"- 位置：{_location(project_root, finding)}",
        f"- 修复方案：{finding['suggestion']}",
        "",
        f"反驳结论：**{rebuttal.get('conclusion', '待确认')}** —— {rebuttal.get('reason', '（无结论）')}",
        "",
        "证据：",
        "",
        _code_block(finding["evidence"]),
        "",
    ]


def _conclusion_blocks(project_root: str, findings: list[dict], rebuttals_by_id: dict) -> list[str]:
    lines: list[str] = []
    for conclusion, title in _CONCLUSION_BLOCKS:
        members = [f for f in findings if _conclusion(f, rebuttals_by_id) == conclusion]
        if not members:
            continue
        # 块内按严重等级降序；sorted 稳定，同等级的维持模型给出的先后
        ordered = sorted(
            members, key=lambda f: _SEVERITY_INDEX.get(f["severity"], -1), reverse=True
        )
        lines += [f"## {title}（{len(ordered)} 条）", ""]
        for finding in ordered:
            lines += _finding_section(project_root, finding, rebuttals_by_id.get(finding["id"], {}))
    return lines


def _decomposition(state: ReviewState) -> list[str]:
    """大模块的报告要交代分解情况：切成了哪几块、有没有没跑完的块、有没有违规。

    这些必须写进报告——大模块一次十几二十个模型调用，少检了一块却从报告上看不出来，
    等于把小模块时代"绝不静默丢弃"的约定放大了十倍。小模块没有这些键，整段不出现。
    """
    chunks = state.get("chunks") or []
    if not chunks:
        return []
    architecture = state.get("architecture") or {}
    lines = ["## 分解", ""]
    lines.append(f"- 形态：{'、'.join(architecture.get('patterns') or ['无明显形态'])}")
    if architecture.get("summary"):
        lines.append(f"- 判断：{architecture['summary']}")
    if architecture.get("strategy"):
        lines.append(f"- 分解策略：{architecture['strategy']}")
    lines.append(
        "- 子块：" + "、".join(f"{c['name']}（{len(c['functions'])} 个函数 / {c['lines']} 行）" for c in chunks)
    )
    incomplete = state.get("incomplete_chunks") or []
    if incomplete:
        lines.append(f"- **未完成的子块**：{'；'.join(incomplete)}——这部分代码未经检视")
    violations = state.get("plan_violations") or []
    if violations:
        lines.append(f"- 分解方案的未达标项：{'；'.join(violations)}")
    lines.append("")
    return lines


def report_tool(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    findings = state.get("findings") or []
    rebuttals_by_id = {item["id"]: item for item in state.get("rebuttals") or []}

    lines = [f"# {module['id']} 检视报告", ""]
    lines += ["## 摘要", "", _summary(findings, rebuttals_by_id), ""]
    lines += _overview(module, findings, rebuttals_by_id)
    lines += _decomposition(state)
    if findings:
        lines += _conclusion_blocks(config.project_root, findings, rebuttals_by_id)
    else:
        lines += ["## 检视意见", "", "本次未发现需要报告的问题。", ""]

    path = config.artifact_path(module["id"], "report")
    write_text(path, "\n".join(lines))
    logger.info("报告已落盘: 模块=%s 意见=%d 条 路径=%s", module["id"], len(findings), path)
    return {"report_path": str(path)}
