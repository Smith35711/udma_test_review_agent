"""结构化契约：模型输出与落盘产物都以此为准。

这里只声明**字段与类型**，不写取值的格式规则。字段之外的形式要求（行号为正、
等级与结论在闭集内、文本非空）一律由确定性的校验节点承担：
检视产物由 `nodes/review_check_tool.py` 校验，反驳产物由 `nodes/rebuttal_check_tool.py`
校验——两处的校验口径对称，规则各自集中在一个节点里，本文件不重复承担。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

SEVERITIES = ("提示", "一般", "严重", "致命")
"""严重等级闭集，由 `review_check_tool` 校验，报告按此排序。"""

CONCLUSIONS = ("维持", "反驳", "待确认")
"""反驳结论闭集：维持 = 问题成立，反驳 = 问题不成立，待确认 = 未能判定。

由 `rebuttal_check_tool` 校验。"""


def unanswered_findings(findings: list[dict], rebuttals: list[dict]) -> list[dict]:
    """挑出还没有结论的检视意见。

    **这是「哪些意见尚未反驳」的唯一判据**，反驳节点（决定本轮问哪些）、
    反驳校验节点（决定哪些编号合法）、遗漏检查节点（决定是否回边补做）三处共用。
    各写一份的话，三者对「已答复」的理解一旦有出入，就会出现凭空消失或反复重问的条目。
    """
    answered = {item["id"] for item in rebuttals}
    return [finding for finding in findings if finding["id"] not in answered]


class Finding(BaseModel):
    """一条检视意见。"""

    file: str = Field(description="问题所在文件，相对待检视目录的路径")
    line: int = Field(description="问题所在行号，从 1 开始")
    severity: str = Field(description=f"严重等级，取值：{' / '.join(SEVERITIES)}")
    description: str = Field(description="问题描述")
    evidence: str = Field(description="支撑结论的代码片段或推理依据")
    suggestion: str = Field(description="修复方案")


class ReviewResult(BaseModel):
    """一次模块检视的完整输出。"""

    findings: list[Finding] = Field(default_factory=list, description="检视发现的问题清单")


class Rebuttal(BaseModel):
    """对一条检视意见的反驳结论。"""

    id: str = Field(description="被反驳意见的编号，如 f1")
    conclusion: str = Field(description=f"结论，取值：{' / '.join(CONCLUSIONS)}")
    reason: str = Field(description="结论的依据：证据链与上下文")


class RebuttalResult(BaseModel):
    """一次反驳的完整输出。"""

    items: list[Rebuttal] = Field(default_factory=list, description="逐条反驳结论")


# ---------------- 大模块专用 ----------------

ARCHITECTURE_PATTERNS = (
    "循环调用组",
    "深层链式调用",
    "扇出枢纽",
    "重复代码",
    "围绕共享状态",
    "分层调用",
)
"""架构形态的候选名。只作为提示词里的可选清单与报告用词，不做闭集校验——
形态判断是描述性的，硬性闭集只会把表述不同的合理判断挡在外面。"""

PROBE_KINDS = (
    "callers",
    "callees",
    "accessors",
    "definition",
    "source",
)
"""证据请求的**闭集**：模型只能点这几道菜，取证由确定性工具执行。

`callers` 上游调用点 / `callees` 下游被调 / `accessors` 全局变量的全部读写点 /
`definition` 类型定义的原文 / `source` 指定函数的源码。
"""


class Chunk(BaseModel):
    """一个逻辑子块：一组函数的集合，不涉及物理文件。"""

    name: str = Field(description="子块名，简短且能说明这组函数的职责")
    purpose: str = Field(description="这组函数共同承担的职责，一句话")
    functions: list[str] = Field(default_factory=list, description="属于该子块的函数名")


class ArchitecturePlan(BaseModel):
    """架构分析的输出：形态判断 + 分解方案。"""

    patterns: list[str] = Field(default_factory=list, description="识别出的架构形态")
    summary: str = Field(description="模块整体形态的判断，两三句话")
    strategy: str = Field(description="分解策略：为什么这样切，一句话")
    chunks: list[Chunk] = Field(default_factory=list, description="分解方案")


class ProbeQuery(BaseModel):
    """一条证据请求。"""

    kind: str = Field(description=f"查询类型，只能取：{' / '.join(PROBE_KINDS)}")
    target: str = Field(description="查询对象：函数名、全局变量名或类型名")


class ProbeRebuttalResult(BaseModel):
    """探测分析一轮的输出：能判定的给结论，判不了的点菜取证。"""

    items: list[Rebuttal] = Field(default_factory=list, description="本轮能判定的反驳结论")
    queries: list[ProbeQuery] = Field(default_factory=list, description="判不了时需要补充的证据")
