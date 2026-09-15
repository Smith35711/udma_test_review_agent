# agent2

Linux C 代码检视智能体。两个阶段：**架构分析**（确定性扫描 + 模块间依赖）与**代码检视**（检视 → 格式校验 → 反驳 → 遗漏补做 → 报告）；模块按行数分流到小模块与大模块两条检视路径。需求见 `docs/需求规格.md`。

## 结构

```
agent2/
├── config/models.yaml          模型配置（分层：defaults < providers < models[]）
├── docs/需求规格.md             需求文档
├── eval/                       评测：缺陷标准清单、大模块样例生成器、评测记录
├── scripts/
│   ├── run_review.py           CLI 入口
│   └── show_models.py          打印模型总览（不发请求、不打印密钥）
├── tests/                      离线测试：假客户端驱动，零额度
└── src/agent2/
    ├── log.py                  日志：控制台 + 文件双 handler，同等级
    ├── model_config.py         配置加载、三级合并、密钥解析
    ├── llm.py                  唯一 LLM 出口 stream_llm_once
    ├── llm_retry.py            带重试的调用（大模块专用，见下）
    ├── csymbols.py             文本级 C 解析：函数、全局变量、宏、类型、调用点
    ├── artifacts.py            产物与源文件的读写
    ├── configuration.py        RunConfig 与产物路径（目录结构的唯一真相来源）
    ├── schemas.py              结构化契约（检视、反驳、架构方案、探测取证）
    ├── state.py                图状态
    ├── graph.py                三张图的组装
    ├── nodes/                  节点：确定性以 _tool.py 结尾，大模型以 _llm.py 结尾
    └── prompts/                提示词：每个大模型节点一个文件
```

## 用法

包未安装，用旧工程的 venv 指向本包源码即可运行：

```bash
PYTHONPATH=agent2/src agent/.venv/bin/python agent2/scripts/run_review.py \
    --project /path/to/c_project [--stages arch,review] [--modules M01,M03] \
    [--concurrency N] [--large-concurrency N] [--timeout SEC] \
    [--log-level LEVEL] [--log-file PATH]
```

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `--project DIR` | 待检视目录 | 必填 |
| `--stages LIST` | `arch` / `review`，可单选或组合；不含 `arch` 时复用既有分析产物 | `arch,review` |
| `--modules LIST` | 待检视模块编号，只作用于 review | 全部 |
| `--concurrency N` | **小模块**的并发数：0=自动（min(模块数, 4)），1=串行 | 0 |
| `--large-concurrency N` | **大模块**的并发数（大模块一次调用要处理二十来条意见，开大了易触发限流） | 2 |
| `--timeout SEC` | 同时作用于首 token 等待与 chunk 间隔；0=沿用默认 60/30 | 0 |
| `--log-level` | 日志等级 | `INFO` |
| `--log-file PATH` | 日志文件路径 | `output/agent2.log` |

退出码：`0` 完成；`1` 有模块失败（其余模块已照常执行）；`2` 前置检查未通过（参数非法、模型连通性测试失败、模块清单缺失）。

**产物根目录是所有项目共用的**（`agent2/output`），模块编号又是按项目内路径从 `M01` 起编的——换个项目跑就会把上一个项目的同名模块产物覆盖掉。要同时保有两个项目的结果，请用 `--output-root` 指定不同的目录。

产物目录（相对 `agent2/`）：

```
output/
├── references/                架构分析产物
│   ├── modules.json             模块清单总表
│   └── dependencies.json        模块编号 → 该模块用到的其他模块符号（检视用的背景信息）
├── M01/                       该模块的全部中间与最终产物
│   ├── findings_raw.json        检视的模型原始产出
│   ├── findings_checked.json    通过格式校验并编号后的意见
│   ├── rebuttals_raw.json       反驳的模型原始产出
│   ├── rebuttals_checked.json   通过格式校验并合并后的结论
│   ├── report.md                最终报告
│   ├── error.json               仅在该模块失败时出现，记录失败原因
│   ├── symbols.json             大模块：符号表（对外接口 / 内部函数 / 全局变量 / 宏）
│   ├── call_graph.json          大模块：调用图与循环调用组
│   ├── architecture.json        大模块：形态判断与分解方案
│   ├── chunks.json              大模块：定稿的子块划分与行区间
│   ├── chunk_<n>_findings.json  大模块：逐子块的检视产出
│   └── probe_rounds.json        大模块：探测取证的查询与证据留痕
└── M02/ …
```

## 流程

模块按行数分流：`≤ 2000` 行（`.c + .h` 之和）走小模块路径，超过的走大模块路径。

**小模块**（源码可整体进上下文）：

```
review_llm → review_check_tool ─[合格]→ rebuttal_llm → rebuttal_check_tool
                    │                                        │
               [不合格] 该模块终止                   omission_check_tool
                                                             │
                                             [还有遗漏] 回到 rebuttal_llm
                                                             │
                                                      report_tool → END
```

**大模块**（源码装不下，先把模块"看小"）：

```
symbol_table_tool → call_graph_tool → architecture_llm → architecture_check_tool
                          ▲                                     │
                          └──────[方案需重做且未到上限]─────────┤
                                                                │[定稿]
                                                    chunk_reviews_llm（子块并行）
                                                                │
                                               review_check_tool ┤(不合格则终止)
                                                                │
                                   probe_rebuttal_llm → rebuttal_check_tool
                                          ▲                     │
                                          └──[仍有证据请求]── probe_tool
                                                                │
                                                 omission_check_tool →(遗漏)
                                                                        │
                                                          report_tool → END
```

两个阶段形状相同：大模型节点只负责产出，紧随其后的确定性节点把格式关，
产物对称地落 `raw` 与 `checked` 两份。

**调度分两段**：所有小模块先并行跑完，再让大模块按 `--large-concurrency` 成批跑。
小模块多而轻、大模块少而重；混在一起时重的那几个会长时间占着名额，小模块全在后面排队。

模块的串行循环与并发在 CLI 层（`ThreadPoolExecutor`），不在图里：模块之间彼此独立，循环放图里要额外背一套游标状态与回边。

## 关键约定

- **检视意见字段**：`file`、`line`、`severity`、`description`、`evidence`、`suggestion`。严重等级取闭集 `提示 / 一般 / 严重 / 致命`；反驳结论取 `维持 / 反驳 / 待确认`。两个闭集都在 `schemas.py`。
- **格式契约两侧对称**：`review_check_tool` 与 `rebuttal_check_tool` 是仅有的两道格式闸门，都只校验格式、不校验内容。口径一致——字段齐全、文本非空、编号合法、取值落在闭集内（严重等级 / 反驳结论）。
- **两道闸门的处置刻意不同**：检视产物不合法意味着该模块结论整体不可信，故**只终止当前模块**（其余模块照常执行，失败原因落 `error.json`）；反驳的条目彼此独立，故丢弃不合法的那条、记 WARNING 留痕，交给遗漏检查去补做，两轮仍拿不到才转「待确认」。
- **编号只在一处产生**：`f1…fN` 由 `review_check_tool` 分配，模型不参与，报告与反驳都以此对齐。
- **哪些意见尚未反驳只有一个判据**：`schemas.unanswered_findings`，反驳节点、反驳校验节点、遗漏检查节点三处共用。
- **反驳不增减条目**：模型多返的编号被拒收，少返的由 `omission_check_tool` 发现并交回补做，**最多 2 轮**；达上限仍无结论的标为「待确认」，绝不静默丢弃。
- **报告列出全部意见**，按结论分三块：`维持` → `待确认` → `已被反驳`，块内按严重等级降序；行号渲染成可点击的 `[file:line](绝对路径#Lline)`。
- **排序只发生在渲染这一步**：JSON 产物（`findings_checked.json` 等）保持模型给出的原始顺序，编号 `f1…fN` 也按该顺序分配，排序是 Markdown 格式化时的事。
- **规模分支**：`.c + .h` 行数之和 ≤ 2000 走小模块，超过的走大模块。
- **背景信息**：检视用的「背景信息」段由扫描阶段的 `dependencies_tool` 填——该模块用到的、**定义在项目其他模块**里的宏（含宏函数）、函数完整签名与类型定义。标准库与系统调用一概不收：它们不属于被测项目，对判断契约与边界没有帮助，只会占上下文。索引**只扫头文件**：跨模块能用到的东西必须出现在头文件里，这也顺带避开了源文件里 `return bm_init(name);` 这类"形似声明"的调用语句。
- **文本级解析，不编译**：符号表与调用图都是正则级的近似（局限见 `csymbols` 模块注释），调用图是**下界**——报出的边可信，未报出的不代表不存在。
- **分解方案由模型提议、确定性节点把关**：`architecture_check_tool` 的硬约束由构造保证（每个函数恰好一块、每一行都有归属，漏认领的进「残余」块），故永远通过；软约束（每块行数、循环调用组不拆散）退化时带原因退回重做，**最多 2 轮**，仍不达标则记入 `chunks.json` 与报告后继续——为一条可能不成立的约束让整个模块拿不到报告更糟。
- **循环调用组是事前输入而非事后否决**：强连通分量先喂进架构提示词（让模型一开始就不切错），校验只作复检。
- **探测分析**：大模块的 findings 来自各子块，每条只看得见模块的一角。反驳因此分两轮——第一轮能给结论的给结论、给不了的提**证据请求**（闭集查询：callers / callees / accessors / definition / source / search / macro），由 `probe_tool` 在符号表与调用图上确定性取证；第二轮凭证据定论，**每条必须有结论**。查询有条数上限、证据有总量上限，超限标明截断。
- **大模块调用带重试**（`llm_retry.py`）：一次调用处理二十来条意见时思考时间到分钟级，中途超过 chunk 间隔阈值的静默是常态；大模块一轮跑十几次调用，任何一次失败都会让整模块拿不到报告。小模块路径不接这一层——调用规模小、已实测稳定。
- **提示词是 `str.format` 模板**：正文里任何字面花括号都要写成 `{{` / `}}`；占位符与节点传入的具名参数必须一一对应。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `MODEL_CONFIG` | 模型配置文件路径（默认 `agent2/config/models.yaml`） |
| `LLM_PROFILE` | 默认生效模型名 |
| `LOG_LEVEL` | 控制台与文件日志等级，默认 INFO |
| `OPENCODE_CONFIG` | opencode 全局配置路径（密钥的最后一级回退来源） |

## 测试

```bash
cd /home/gzq/langgraph
PYTHONPATH=agent2/src agent/.venv/bin/python -m unittest discover -s agent2/tests -t agent2/tests -v
```

全部离线、秒级，假客户端驱动，两个文件：

- `test_flow.py`——小模块：正常流程、格式校验不通过、遗漏补做回边、补做达上限转「待确认」、报告分块与排序。
- `test_large_module.py`——大模块：符号表与调用图解析、分解方案的归属修补与行覆盖、循环调用组被拆散时退回重做、子块并发合并、探测两轮取证与收敛、模块间依赖只收内部符号。

假客户端**按提示词里注入的 schema 标题分派回复，不能按调用顺序**：子块是并发跑的，顺序不定，按顺序取回复必然随机失败。

实跑验证用的样例：`test_project`（6 个小模块）与 `test_project_large`（1 个大模块 + 1 个依赖它的小模块，由 `eval/make_large_fixture.py` 生成，含循环调用组、扇出枢纽、重复代码与 9 处植入缺陷）。

## 未纳入

- 架构分析阶段的其他分析节点（规格 §4.3 留空）
- 产物版本管理、断点续跑（需求未提出）
- `llm_probe.py` 那样的端点逐层连通性诊断
- `stats.py` 那样的跨调用 token / 耗时汇总：目前只能从日志逐次读取
- 小模块路径的调用重试：只有大模块接了 `llm_retry`（见「关键约定」）
