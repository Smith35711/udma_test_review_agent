"""评审流程各阶段的结构化产物模型（全部经 schema 校验）。

关于 `deterministic=True`
-----------------------
标了该标记的字段是**流水线自己填的**（锚点、来源批次、校验状态等），不是模型的输出契约。
`llm.py::_apply_json_constraint` 会把它们从注入提示词的 JSON Schema 里剔除，
避免模型被要求编造 `anchor_fingerprint` 这类它无从得知的值。它们仍参与落盘与校验。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

ARTIFACT_VERSION = 2
"""产物格式版本。断点续跑靠产物存在性判断，**改了 id 语义就必须升版本**：
否则续跑会把旧格式的 `f1` 序号与新的锚点 id 混在同一批产物里，两边都不再自洽。
本值进入 `RunManifest.artifact_version` 并参与输入指纹，故升版本会让既有产物整体
失效并重算（见 `manifest.py`）——这正是期望行为：格式变了，旧产物一律不可复用。"""

DETERMINISTIC = {"deterministic": True}
"""标记「该字段由流水线填写、不进模型输出契约」。"""


class SourceFileInfo(BaseModel):
    path: str = Field(description="相对被测项目根的路径")
    lines: int
    chars: int


class FunctionInfo(BaseModel):
    name: str
    file: str
    start_line: int
    end_line: int


class ModuleInventory(BaseModel):
    module_id: str = Field(default="", description="模块标识，如 M01")
    name: str = Field(description="模块名，文件级模块取标识")
    path: str = Field(description="模块主文件相对路径（.c）")
    files: list[SourceFileInfo]
    chars: int
    token_estimate: int


class IncludeEdge(BaseModel):
    src: str
    dst: str


class CallEdge(BaseModel):
    caller: str = Field(description="调用方函数名")
    callee: str
    caller_file: str


class ScanResult(BaseModel):
    project_root: str
    modules: list[ModuleInventory]
    include_graph: list[IncludeEdge]
    call_graph: list[CallEdge]
    functions: list[FunctionInfo]
    build_flags: list[str] = Field(default_factory=list, description="从构建文件文本提取的 -I/-D 参数")


class SystemModule(BaseModel):
    name: str
    responsibility: str = Field(description="职责一句话")
    entry: str = Field(description="入口文件或主要文件")


class TrustEntry(BaseModel):
    """信任面入口（外部输入进入系统的位置）。

    `evidence` 与行级位置共同构成**可核对性**要求：实测这条约束此前完全缺失，
    模型仅凭函数名 `release_twice` 就推断出一个「并发热点」，而样例工程没有任何
    线程或同步原语——该推断随后扩散进每个模块的检视上下文，制造出一整类
    「多线程并发写同一缓冲」的假阳性。没有行级代码依据的条目必须由产出方
    （`nodes/llm/global_arch.py`）显式标记为推测，下游不得当作事实使用。
    """

    file: str
    line: int = 0
    kind: str = Field(description="syscall/socket/file_io/argv/ipc/parse 之一")
    description: str
    evidence: str = Field(default="", description="支撑该结论的代码依据（带行号的语句）")


class ConcurrencyHotspot(BaseModel):
    """并发热点：锁、共享状态、信号、fork 等并发使用点。`evidence` 要求同 `TrustEntry`。"""

    file: str
    line: int = 0
    kind: str = Field(description="mutex/cond/rwlock/atomic/signal/fork/shared_state 之一")
    description: str
    evidence: str = Field(default="", description="支撑该结论的代码依据（带行号的语句）")


class GlobalArchitecture(BaseModel):
    layers: list[str] = Field(description="系统分层描述")
    modules: list[SystemModule]
    trust_entries: list[TrustEntry]
    concurrency_hotspots: list[ConcurrencyHotspot]
    conventions: list[str] = Field(default_factory=list, description="项目级惯例：内存所有权/错误码等")


class InterfaceContract(BaseModel):
    signature: str
    file: str
    line: int = 0
    precondition: str = Field(default="", description="前置条件；空表示未知")
    input_validated: bool = Field(default=False, description="输入是否在上游已校验(边界函数为 False)")
    is_boundary: bool = Field(default=False, description="是否为信任面边界函数")


class DomainInfo(BaseModel):
    name: str
    files: list[str]
    description: str = ""


class ModuleConcurrencyContract(BaseModel):
    thread_model: str = Field(description="single_thread / per_connection / multi_thread 之一")
    caller_locked: list[str] = Field(default_factory=list, description="约定由调用方加锁的结构或接口")
    internal_locks: list[str] = Field(default_factory=list, description="模块内部已持锁边界描述")


class ChecklistItem(BaseModel):
    category: str = Field(description="memory/concurrency/trust_boundary/resource/logic/macro/ub 之一")
    item: str
    file: str = Field(default="", description="该检查项对应的具体文件；无法落到行级时留空")
    line: int = Field(default=0, description="该检查项对应的具体行号；无法落到行级时为 0")
    evidence: str = Field(default="", description="支撑该检查项的代码依据（带行号的语句）")


class ModuleArchitecture(BaseModel):
    module: str
    responsibility: str
    interfaces: list[InterfaceContract]
    domains: list[DomainInfo]
    concurrency_contract: ModuleConcurrencyContract
    checklist: list[ChecklistItem]


class ModuleSize(BaseModel):
    module: str
    lines: int = Field(description="模块 .c 与 .h 物理行数之和")
    chars: int = Field(description="模块 .c 与 .h 字符数之和")
    size_class: str = Field(description="small / large")
    line_threshold: int = Field(description="规模判定阈值（行）")


class PathStep(BaseModel):
    file: str
    line: int = 0
    description: str


class CriticalPath(BaseModel):
    category: str = Field(description="concurrency / trust_boundary")
    title: str
    steps: list[PathStep]
    risk: str


class CriticalPathReport(BaseModel):
    paths: list[CriticalPath]


class ToolEvidence(BaseModel):
    """外部静态分析工具（gcc -fanalyzer / cppcheck）的独立发现。

    定义在 `Finding` 之前，因为 `Finding.tool_evidence` 直接引用本模型——
    结构化而非字符串，是为了让报告与评测能区分「哪个工具、哪条规则」印证了哪一条主张。
    """

    tool: str = Field(description="gcc-analyzer / cppcheck")
    file: str
    line: int = 0
    rule: str = Field(description="工具规则名，如 use-after-free / doubleFree / CWE-416")
    severity: str = Field(description="映射到本项目等级：提示/一般/严重/致命")
    message: str


class Finding(BaseModel):
    id: str = Field(description="f1/f2/... 编号")
    category: str = Field(description="memory/concurrency/trust_boundary/resource/logic/macro/ub 之一")
    severity: str = Field(description="提示/一般/严重/致命 之一（由低到高）")
    title: str
    description: str = Field(description="问题描述：一句话说清是什么问题，含关键变量或函数")
    root_cause: str = Field(
        description="根因分析：详细指出具体问题代码位置(file:line)、数据流或调用链、所违反的契约或假设"
    )
    impact: str = Field(description="影响：可导致的后果及严重等级依据")
    evidence: str = Field(description="代码证据：带行号的关键代码语句或片段")
    suggestion: str = Field(description="修复方案：详细、可执行的具体改法与步骤")
    file: str
    line: int
    call_chain: list[str] = Field(description="从入口到问题点的调用链，file:line 形式；无法给出完整链路则必须为空列表")
    channel: str = Field(default="finding", description="finding=完整路径支撑；observation=有疑点但路径不完整")
    defect_kind: str = Field(
        default="",
        description=(
            "缺陷种类，**必须从闭集取值**：unbounded_write / no_nul_termination / index_not_advanced / "
            "unchecked_alloc / missing_release / double_free / use_after_free / null_deref / "
            "integer_overflow / missing_error_check / race / path_trust / unsafe_macro / other。"
            "同一语句上可能并存多个不同缺陷，该字段用于把它们彼此区分开；无法归类时选 other，"
            "不要自造标签（自造标签会让同一语句上的同类缺陷无法被识别为同一主张）"
        ),
    )

    # ---- 以下由流水线填写，不进入模型输出契约 ----
    anchor: str = Field(default="", description="锚点 id：<file>:<行号>#<源文本指纹>", json_schema_extra=DETERMINISTIC)
    anchor_fingerprint: str = Field(default="", description="所在语句源文本的指纹", json_schema_extra=DETERMINISTIC)
    source_pass: str = Field(default="", description="产出来源批次：small/large/focused/tool", json_schema_extra=DETERMINISTIC)
    source: str = Field(default="llm", description="llm / tool / merged", json_schema_extra=DETERMINISTIC)
    merged_from: list[str] = Field(default_factory=list, description="被合并进来的原始 id", json_schema_extra=DETERMINISTIC)
    evidence_status: str = Field(
        default="",
        description="位置校验结果：verified / unattributable / invalid（见 nodes/llm/review.py）",
        json_schema_extra=DETERMINISTIC,
    )
    tool_evidence: list[ToolEvidence] = Field(
        default_factory=list, description="外部工具（gcc -fanalyzer / cppcheck）的印证条目", json_schema_extra=DETERMINISTIC
    )


class ReviewFunnel(BaseModel):
    """检视阶段的条目漏斗：每一步过滤掉多少条。

    设计文档 P6 要求「过滤率纳入报告，作为评审结论可信度依据」，而在这之前**漏斗根本
    不存在**——只知道最终留下几条，不知道中间丢了多少、为什么丢。过滤率异常（例如
    去重砍掉一大半）是提示词或上下文注入出问题的信号，没有漏斗就只能看见结果看不见过程。
    """

    model_findings: int = Field(default=0, description="各批次声称 channel=finding 的原始条数之和")
    model_observations: int = Field(default=0, description="各批次 observation 条数之和")
    location_invalid: int = Field(default=0, description="位置校验未通过、降级为 observation")
    tool_added: int = Field(default=0, description="工具补漏新增的候选")
    tool_rejected: int = Field(default=0, description="工具候选因位置校验未通过被丢弃")
    after_dedup: int = Field(default=0, description="锚点+缺陷种类去重后的条数")
    capped: int = Field(default=0, description="超出单次上限被丢弃")
    obs_redundant: int = Field(default=0, description="与已保留主张重复或自身重复而剔除的 observation")
    kept_findings: int = Field(default=0, description="最终 findings")
    kept_observations: int = Field(default=0, description="最终 observations")


class RawFindings(BaseModel):
    findings: list[Finding]
    observations: list[Finding] = Field(default_factory=list)
    checklist_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "上游架构已定位到 file:line、而本轮检视既未出 finding 也未出 observation 的检查项。"
            "由 `nodes/llm/review.py` 确定性计算后回填，**不由模型产出**——"
            "它是「上游已知线索在下游丢失」这一漏检模式的显式记账，用于让漏检可见。"
        ),
        json_schema_extra=DETERMINISTIC,
    )
    funnel: ReviewFunnel = Field(
        default_factory=ReviewFunnel,
        description="检视阶段的条目漏斗，由 `nodes/llm/review.py` 确定性回填，不由模型产出",
        json_schema_extra=DETERMINISTIC,
    )


class ReviewCheck(BaseModel):
    passed: bool = Field(description="检视结论是否通过格式与内容审查")
    issues: list[str] = Field(default_factory=list, description="未通过时的具体问题")
    feedback: str = Field(default="", description="给生成方的修正指引")


class Verdict(BaseModel):
    id: str
    conclusion: str = Field(description="confirmed / refuted / uncertain")
    severity: str = Field(description="复核后的严重等级：提示/一般/严重/致命 之一")
    confidence: float = Field(ge=0.0, le=1.0)
    rebuttal_opinion: str = Field(description="反驳意见：一句话说明维持或推翻该 finding 的理由，三种结论均须给出")
    refutation_log: str = Field(description="反驳尝试记录：逐点核对结果")
    evidence_note: str = Field(default="", description="关键证据说明，file:line 形式")
    contract_dependent: bool = Field(
        default=False,
        description=(
            "是否依赖**本模块之外**不可见的契约（调用方加锁约定、外部长度约束等）。"
            "为 true 表示本阶段无法独立核验：应由确定性闸门降级为 uncertain 并单列，"
            "不作为 confirmed 计入精确率"
        ),
    )

    # ---- 以下由流水线填写，不进入模型输出契约 ----
    inconsistent: bool = Field(
        default=False, description="同一主张出现互相冲突的结论，已交人工裁决", json_schema_extra=DETERMINISTIC
    )
    sibling_verdicts: list[dict] = Field(
        default_factory=list, description="同主张上被保留的分歧结论原文", json_schema_extra=DETERMINISTIC
    )
    verdict_source: str = Field(
        default="model",
        description=(
            "结论来源：model=模型给出；fallback=模型漏回、由闸门降级补位；"
            "tool=被外部静态分析工具证据校正（印证或裁决）"
        ),
        json_schema_extra=DETERMINISTIC,
    )
    demoted_reason: str = Field(default="", description="被闸门降级的原因", json_schema_extra=DETERMINISTIC)


class RebuttalReport(BaseModel):
    results: list[Verdict]


class ToolEvidenceReport(BaseModel):
    """工具证据层产物。

    **工具缺席不等于证伪**：实测 gcc -fanalyzer 与 cppcheck 合起来只能覆盖 18 项已知
    缺陷中的 6 项，且最重要的 `buffer.c:7` 无界 strcpy 两个工具都抓不到。因此本产物
    只能用于「印证」「补漏」「裁决矛盾」，绝不可用作「没有报告 ⇒ 不是缺陷」的依据。
    """

    available: list[str] = Field(default_factory=list, description="本次成功运行的工具")
    skipped: list[str] = Field(default_factory=list, description="未能运行的工具及原因")
    entries: list[ToolEvidence] = Field(default_factory=list)


class ReviewMeta(BaseModel):
    module: str
    mode: str
    elapsed_sec: float
    findings_total: int
    confirmed: int
    refuted: int
    uncertain: int
    observations: int


class RunManifest(BaseModel):
    """本次运行的输入指纹，用于判断断点产物是否仍然有效。

    断点续跑靠「产物存在」判断，产物本身却不知道自己是在什么输入下产生的。
    于是改动源码、换模型、改提示词之后重跑，S1–S3 会静默复用旧结论，与新的检视
    结果混在一份报告里——两边都不再自洽，且完全无声。本清单在 scan 阶段落盘，
    再次运行时逐字段比对，不一致即归档旧产物并全量重算。
    """

    version: int = 1
    artifact_version: int = ARTIFACT_VERSION
    created_at: str = Field(description="生成时间（本地时间，仅作展示）")
    project_root: str
    sources_digest: str = Field(description="全部 .c/.h 内容哈希的汇总")
    sources: dict[str, str] = Field(default_factory=dict, description="相对路径 -> 内容哈希")
    module_map_digest: str
    prompts_digest: str
    model_digest: str = Field(description="生效模型名/模型/端点/温度/种子")
    thresholds: dict[str, object] = Field(default_factory=dict)
    concurrency: int = 0
    digest: str = Field(default="", description="以上全部输入的汇总哈希，用于快速比对（由 manifest.fingerprint 回填）")

FINGERPRINT_FIELDS = (
    "artifact_version",
    "project_root",
    "sources_digest",
    "module_map_digest",
    "prompts_digest",
    "model_digest",
    "thresholds",
    "concurrency",
)
"""参与失效判定的字段。`created_at`/`sources`/`digest` 不入列：
前两者是展示信息，后者是这些字段的汇总。"""
