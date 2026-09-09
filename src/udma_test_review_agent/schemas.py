"""评审流程各阶段的结构化产物模型（全部经 schema 校验）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    file: str
    line: int = 0
    kind: str = Field(description="syscall/socket/file_io/argv/ipc/parse 之一")
    description: str


class ConcurrencyHotspot(BaseModel):
    file: str
    line: int = 0
    kind: str = Field(description="mutex/cond/rwlock/atomic/signal/fork/shared_state 之一")
    description: str


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


class RawFindings(BaseModel):
    findings: list[Finding]
    observations: list[Finding] = Field(default_factory=list)


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
    contract_dependent: bool = Field(default=False, description="是否属于依赖跨模块契约的条目(展示时降级)")


class RebuttalReport(BaseModel):
    results: list[Verdict]


class ReviewMeta(BaseModel):
    module: str
    mode: str
    elapsed_sec: float
    findings_total: int
    confirmed: int
    refuted: int
    uncertain: int
    observations: int
