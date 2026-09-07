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
    name: str = Field(description="模块名，通常为子系统目录名")
    path: str = Field(description="模块根目录相对路径")
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
    size_class: str = Field(description="small / large")
    token_estimate: int


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
    severity: str = Field(description="critical/high/medium/low")
    title: str
    description: str
    file: str
    line: int
    call_chain: list[str] = Field(description="从入口到问题点的调用链，file:line 形式；无法给出完整链路则必须为空列表")
    channel: str = Field(default="finding", description="finding=完整路径支撑；observation=有疑点但路径不完整")


class RawFindings(BaseModel):
    findings: list[Finding]
    observations: list[Finding] = Field(default_factory=list)


class Verdict(BaseModel):
    id: str
    conclusion: str = Field(description="confirmed / refuted / uncertain")
    severity: str = Field(description="复核后的严重等级")
    confidence: float = Field(ge=0.0, le=1.0)
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
