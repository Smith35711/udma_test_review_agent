"""评审流程各阶段提示词模板。"""

from __future__ import annotations

GLOBAL_ARCH_PROMPT = """你是软件架构分析专家。以下是一个 Linux C 项目的目录树、模块依赖信息与各模块入口文件首部。请分析并输出全局架构报告（JSON）。

要求：
- 仅基于给出的信息归纳，不臆造未出现的事实；
- trust_entries 列出外部输入进入点（syscall/socket/文件IO/argv/ipc/数据解析），给出 file 与大致行号；
- concurrency_hotspots 列出锁、共享状态、信号、fork 等并发使用点；
- conventions 记录可观察的项目级惯例（内存所有权、错误码风格等）。

目录树与模块清单：
{inventory}

依赖信息（include 边）：
{includes}

各模块入口首部：
{headers}
"""

MODULE_ARCH_PROMPT = """你是软件架构分析专家。请分析以下 C 模块并输出模块架构报告（JSON）。

模块名：{module}

全局架构中与本模块相关的信息：
{global_context}

要求：
- interfaces：列出对外接口函数（头文件签名），标注 precondition（前置条件，未知留空）、
  input_validated（输入是否已在上游校验；直接接触外部输入的边界函数为 False）、is_boundary；
- domains：给出内部域划分（按文件或功能簇），供后续按域检视使用；
- concurrency_contract：thread_model 取 single_thread / per_connection / multi_thread 之一，
  caller_locked 列出"由调用方负责加锁"的结构或接口，internal_locks 描述模块内部持锁边界；
- checklist：生成本模块检视检查项，category 取 memory/concurrency/trust_boundary/resource/logic/macro/ub 之一；
- size_class：small（一次性全量检视）/ large（按域分批检视），依据代码规模 token 估算 {token_estimate}，
  阈值 {small_threshold}；
- 仅基于给出信息，不臆造。

模块源码或结构清单：
{source}
"""

CRITICAL_PATH_PROMPT = """你是软件架构分析专家。基于以下全局架构与各模块架构报告，分析关键路径（JSON）。

要求：
- concurrency 路径：追踪共享数据的跨模块读写时序（锁获取/释放配对、初始化与使用顺序）；
- trust_boundary 路径：追踪外部输入从进入点到校验点再到危险汇点的传播；
- 每条路径给出步骤序列（file:line + 描述）、涉及模块、风险说明；
- 仅列出有 file:line 证据支撑的路径，不做无证据推测；
- 若证据不足，宁缺毋滥。

全局架构报告：
{global_arch}

各模块架构报告：
{module_archs}
"""

REVIEW_PROMPT = """你是资深 C 语言代码评审专家。对以下模块执行一次综合检视，输出结构化 findings（JSON）。

本模块架构摘要（职责/接口契约/并发契约/检查项）：
{module_arch}

全局相关背景（信任面/并发热点中涉及本模块的条目）：
{global_context}

检视清单（按类别组织，逐项核对）：
{checklist}

强制规则：
- 内存/字符串/整数等模式可判问题须与常规静态告警区分，聚焦静态工具无法覆盖的语义级问题；
- 并发类与信任面类 finding 必须给出从入口到问题点的完整调用链（call_chain，file:line 形式）；
  给不出完整链路的一律放入 observations（channel=observation），不得放入 findings；
- 内部函数（input_validated=True 或非边界函数）默认信任前置条件，不报参数校验缺失；
  仅当前置条件无法满足或契约缺失时才报；
- 校验可能以 clamp、查找表、白名单、状态机、封装宏完成，未见显式 if 长度判断不等于未校验；
- 豁免项（不报）：调用方已持锁（契约声明 caller_locked）、参数来自内部可信路径、
  init 后只读数据、校验由封装宏完成且可引用其定义；
- 每条 finding 引用真实 file:line，禁止编造行号。

模块源码（带行号）：
{source}
"""

DOMAIN_REVIEW_PROMPT = """你是资深 C 语言代码评审专家。以下是一个大型 C 模块中的一个域（单文件），请对该域执行检视，输出结构化 findings（JSON）。

模块架构摘要（接口契约/并发契约/检查项）：
{module_arch}

模块公共接口签名表（头文件，供调用契约参照，不在本次检视范围内）：
{headers}

强制规则：
- 并发类与信任面类 finding 必须给出完整调用链（call_chain，file:line 形式）；给不出的放入 observations；
- 内部函数默认信任前置条件，不报参数校验缺失；
- 校验可能以 clamp、查找表、白名单、状态机、封装宏完成；
- 豁免项：调用方已持锁（契约声明）、内部可信路径、init 后只读、封装宏校验；
- 每条 finding 引用真实 file:line。

本域源码（带行号）：
{source}
"""

FOCUSED_REVIEW_PROMPT = """你是资深 C 语言代码评审专家。本模块被标记为高风险（信任面入口/并发热点密集），请执行一次专项检视，仅覆盖 concurrency 与 trust_boundary 两个维度，输出结构化 findings（JSON）。

本模块架构摘要：
{module_arch}

强制规则：
- 并发类：须给出完整读写时序与调用链；契约声明为调用方负责的项不报；
- 信任面类：须给出从外部输入进入点到危险汇点的完整路径；内部函数默认信任前置条件；
- 给不出完整链路的一律放入 observations；
- 每条引用真实 file:line。

模块源码（带行号）：
{source}
"""

REBUTTAL_PROMPT = """你是检视结论的证伪者。以下是一条检视 finding，请尝试证明它不成立（JSON 输出）。

Finding：
{finding}

相关函数源码（带行号，finding 所在函数）：
{function_code}

上游调用点（近似调用图，供核对上游是否已校验/已持锁）：
{callers}

反驳要求：
- 逐点核对：调用链是否真实存在（可与给出的调用点核对）、上游是否已有校验/加锁、
  是否命中豁免规则（调用方负责、内部可信路径、封装宏校验、init 后只读）；
- 结论三选一：confirmed（无法证伪）/ refuted（找到反证）/ uncertain（证据不足）；
- refuted 必须在 refutation_log 中写明具体反证（file:line）；
- uncertain 的条目将标记"待人工确认"，不要为了结案而强行 confirmed/refuted；
- 复核严重等级与置信度；属于"依赖调用方契约"性质的条目置 contract_dependent=True。
"""

REPORT_SUMMARY_PROMPT = """以下是一份代码检视报告，请用不超过 10 行输出执行摘要（中文，面向技术负责人）：总体结论、按严重等级的问题分布、最需要优先处理的 3 项、过程可信度说明（过滤率与置信度分布）。

报告内容：
{report}
"""
