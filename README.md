# udma_test_review_agent

面向 Linux C 项目的自动化代码检视智能体，基于 **LangGraph** 有状态图编排。

它按**文件级模块**组织检视对象，先做全局与模块架构分析，再逐模块检视（可并发），并由独立的**反驳子代理**对每条结论做证伪式验证，最终产出人类可读的报告与机器可消费的结构化结论。检视本身只读源码、**不依赖编译**；另有一层尽力而为的外部工具证据（`gcc -fanalyzer` / `cppcheck`）作为独立真相源，工具缺席时静默降级。

框架结构模板见 `docs/AGENT框架说明.md`，请求处理行为规范见仓库根目录 `AGENTS.md`。

---

## 快速开始

```bash
# 1. 安装（要求 Python >= 3.10）
cd agent
python -m venv .venv
.venv/bin/pip install -e .

# 2. 配置密钥。默认模型走 opencode-go 端点，密钥自动从 ~/.config/opencode/opencode.json
#    读取；也可显式导出（优先级高于全局配置）：
export OPENCODE_API_KEY=sk-...     # 或 DEEPSEEK_API_KEY / LLM_API_KEY

# 3. 编辑 config/modules.yaml，把被测项目的每个 .c 映射到 M01、M02…

# 4. 运行：全部模块（默认自动并发，上限 8）
.venv/bin/python scripts/run_review.py --project /path/to/c_project --modules all

# 5. 查看报告与机器可读产物
cat output/review/report.md
cat output/review/findings.json     # 机器读：CI 门禁与基线抑制消费这个

# 6.（可选）对本次产物算 recall / precision 等质量指标，离线、零额度
.venv/bin/python scripts/eval_review.py --run output/review
```

退出码：`0` 正常 ｜ `1` 有模块失败 ｜ `2` 触发了 `--fail-on` 门槛。详见[退出码](#退出码)。

---

## 基本架构

### 处理流程

```
START
  │
  ▼
scan(S0)            确定性：文本级扫描 → 模块清单 / include 图 / 近似调用图 / 运行清单
  │
  ▼
tool_evidence       确定性：尽力而为跑 gcc -fanalyzer 与 cppcheck → 外部独立证据
  │
  ▼
global_arch(S1)     LLM：全局架构（分层、信任面、并发热点、项目惯例）
  │
  ▼
module_archs(S2)    LLM：逐模块架构（接口契约、并发契约、检查项）
  │
  ▼
critical_paths(S3)  LLM：关键路径（并发时序、污点传播）
  │
  ▼
merge(S4)           确定性：跨模块校验（头文件声明未见实现等）
  │
  ▼
┌──────── 模块循环（按模块标识升序；各模块可并发执行，--concurrency） ────────┐
│  size_analysis      确定性：统计 .c/.h 物理行数与字符数，判定 small/large │
│  review_module      LLM：small 全量生成+内容审查重生成；large 按文件分批； │
│                     已定位检查项闭环：无同关切族结论者追加一次定向补检  │
│  rebuttal_module    LLM：整模块一次请求逐条证伪，漏返回的单独重发     │
│  review_check       确定性：一致性闸门 + 核对每条 finding 均有结论并落盘 │
│  module_report      LLM：单模块 Markdown 格式化 + 执行摘要   │
│  advance            确定性：推进到下一模块（唯一可写 review_index 的节点）│
└───────────────────────────┬────────────────────────────────┘
                            │ 全部完成
                            ▼
report              确定性 markdown 汇总渲染
  │
  ▼
 END
```

核心原则：**确定性与智能分离**。扫描、尺寸判定、反驳审查、格式转换与汇总由工具节点完成；LLM 仅承担架构归纳、检视、反驳、内容审查与摘要。阶段间以经 schema 校验的 JSON 交接，所有产物落盘。

### 节点职责

| 节点 | 类型 | 职责 | 主要产物 |
| --- | --- | --- | --- |
| `scan` | 工具 | 文本级扫描：模块清单、include 图、近似调用图、函数表；同时写运行清单 | `scan.json`、`run_manifest.json` |
| `tool_evidence` | 工具 | 尽力而为跑 `gcc -fanalyzer` 与 `cppcheck`，产出外部独立证据（工具缺失/不可编译则记录跳过原因，不失败） | `tool_evidence.json` |
| `global_arch` | LLM | 全局分层、信任面入口、并发热点、项目惯例（后两者须带真实 `file:line` 依据） | `global_architecture.json` |
| `module_archs` | LLM | 逐模块接口契约、并发契约、检查项 | `<ID>/architecture.json` |
| `critical_paths` | LLM | 并发时序与污点传播关键路径 | `critical_paths.json` |
| `merge` | 工具 | 跨模块确定性校验（如头文件声明未见实现） | 状态中的候选列表 |
| `size_analysis` | 工具 | 统计模块 `.c` 与 `.h` 物理行数、字符数，按行数阈值判定 `small`/`large` | `<ID>/size.json` |
| `review_module` | LLM | 单模块检视：small 全量读取并生成 JSON，经格式与内容审查不通过则重新生成；large 按文件分批，高风险模块追加专项检视；**上游已定位、而首轮无同关切族结论的检查项追加一次定向补检**（见[检查项闭环补检](#检查项闭环补检)）；工具独立报出而模型未提的主张由确定性判定补为候选（见[工具证据补漏](#工具证据补漏)） | `<ID>/findings_raw.json` |
| `rebuttal_module` | LLM | 整模块一次请求逐条证伪（含所在函数源码、上游调用点代码、本模块契约）；漏返回条目单独重发，仍缺失则标记 `uncertain`。两条红线见[反驳阶段的判定边界](#反驳阶段的判定边界) | 状态中的反驳结果 |
| `review_check` | 工具 | 一致性闸门：同一主张只留一条结论，冲突降 `uncertain` 并保留分歧原文；核对每条 finding 均有反驳结论（缺失补 `uncertain`），通过后落盘 | `<ID>/findings_verified.json` |
| `module_report` | LLM | 单模块 Markdown 格式化 + LLM 执行摘要，即时落盘 | `<ID>/report.md` |
| `advance` | 工具 | 模块循环推进（**唯一可写 `review_index` 的节点**） | — |
| `report` | 工具 | 汇总各模块报告与统计、跨模块候选，生成项目总报告与机器可读产物 | `report.md`、`findings.json`、`metrics.json` |

### 目录结构

```
agent/
├── config/
│   ├── models.yaml            # 多模型配置（含 roles 角色路由）
│   └── modules.yaml           # 文件级模块标识映射（M01 → 路径）
├── eval/
│   └── expected_findings.yaml # test_project 的人工 ground truth（评测基线）
├── scripts/
│   ├── run_review.py          # 命令行入口
│   ├── eval_review.py         # 离线质量评测：recall / precision / 矛盾率 / 重复率
│   └── test_single_llm_node.py# 单 LLM 节点示例（遗留 langchain 路径）
├── tests/                     # unittest 测试（venv 无 pytest），见「测试」
├── src/udma_test_review_agent/
│   ├── graph.py               # 图组装与编译（导出 graph）
│   ├── state.py               # 图状态 ReviewState（无 reducer，后写覆盖）
│   ├── configuration.py       # 运行期配置 ReviewConfig
│   ├── model_config.py        # 模型配置加载与解析（分层合并 / 角色路由 / 密钥回退链）
│   ├── module_map.py          # 模块映射与选择解析
│   ├── llm.py                 # 唯一 LLM 出口：流式、超时、心跳、采样参数、schema 校验
│   ├── log.py                 # 日志：控制台按等级，文件恒 DEBUG
│   ├── schemas.py             # 各阶段结构化产物模型（Pydantic）
│   ├── findings_util.py       # 确定性判定层：锚点身份、缺陷族、聚类、一致性
│   ├── manifest.py            # 运行清单：输入指纹与断点失效判定
│   ├── artifact_io.py         # 产物原子读写原语
│   ├── versioning.py          # 产物版本归档
│   ├── stats.py               # 过程统计（token / 阶段耗时 / 漏斗）
│   ├── prompts/               # 提示词子包（按节点拆分；_shared.py 为逐字共享片段）
│   ├── nodes/
│   │   ├── common.py          # 节点通用辅助（配置缓存、产物路径、批并发执行）
│   │   ├── llm/               # 大模型节点（架构 / 检视 / 反驳 / 模块报告）
│   │   └── tool/              # 确定性工具节点（扫描 / 工具证据 / 跨模块校验 / 尺寸分析 / 反驳审查 / 推进 / 汇总）
│   └── tools/executors/
│       ├── c_scanner.py       # S0 纯文本 C 扫描器
│       └── static_analyzers.py# gcc -fanalyzer 与 cppcheck 执行层
└── pyproject.toml             # 依赖与打包
```

`tools/registry.py`、`tools/executors/script_runner.py`、`tools/__init__.py`、`prompts/__init__.py`
是框架模板留下的**空占位文件**，本工程未使用 registry 工具层。

### 产物与版本管理

产物写入 `--output-root`（默认 `output/review`），**每模块一个子目录**：

| 路径 | 内容 |
| --- | --- |
| `scan.json` | S0 扫描结果 |
| `run_manifest.json` | **运行清单**：本次运行的输入指纹（源码哈希、模块映射、提示词、模型、阈值、并发） |
| `tool_evidence.json` | 外部工具证据（`gcc -fanalyzer` / `cppcheck` 的诊断，含 `available` / `skipped`） |
| `global_architecture.json` | S1 全局架构 |
| `critical_paths.json` | S3 关键路径 |
| `<ID>/architecture.json` | 模块架构摘要 |
| `<ID>/size.json` | 模块尺寸（行数 / 字符数 / small·large） |
| `<ID>/findings_raw.json` | 检视原始发现（findings + observations） |
| `<ID>/findings_verified.json` | 反驳并经审查节点核对后的结论（confirmed / refuted / uncertain） |
| `<ID>/report.md` | 单模块报告（反驳审查后即时生成） |
| `<ID>/history/<run_ts>/` | 历史版本归档（三阶段产物） |
| `report.md` | 项目总报告（含溯源头与过程指标段） |
| `findings.json` | **机器可读**：项目级扁平条目，含稳定 `identity`、结论、证据来源 |
| `metrics.json` | **过程指标**：token 用量、各阶段耗时、过滤率、条目漏斗 |

**机器可读产物**（供 CI 门禁与基线抑制消费，不要解析 `report.md`）：

- `findings.json` 顶层：`schema_version`、`generated_at`、`project_root`、`model_profile`、
  `manifest_digest`、`counts`（各结论数 + `observations` + `checklist_gaps`）、
  `severity_counts`、`worst_confirmed_severity`（**只统计 confirmed**，`--fail-on` 就读它）、
  `failed_modules`、`findings[]`、`observations[]`、`checklist_gaps[]`。
  每条 finding 含 `identity`（`锚点|缺陷种类`，源码一变就变）、`file` / `line` /
  `category_family` / `defect_kind`、`conclusion` / `confidence` / `verdict_source`、
  `source`（`llm` 或 `tool`）、`tool_evidence[]`、`merged_from[]`。
- `metrics.json` 顶层：`wall_clock`、`llm`（按角色的调用数 / 失败数 / tokens / 耗时）、
  `stages`（各阶段次数与单次最慢）、`filter`（过滤率、observations 占比、漏斗）、`modules`。
  其中 `filter.funnel` 是各模块漏斗的和，键为 `model_findings` / `model_observations` /
  `location_invalid` / `tool_added` / `tool_redundant` / `tool_rejected` / `checklist_repair` /
  `after_dedup` / `capped` / `obs_redundant` / `kept_findings` / `kept_observations`。

**断点续跑 = 产物存在性 + 输入指纹校验**：产物存在即跳过对应阶段，但**跳过之前**先比对
`run_manifest.json` 与当前配置/源码——不一致则归档旧产物并重算。修改源码、换模型、
改提示词、改阈值、改并发数都会正确失效对应阶段；否则会出现「架构基于旧代码、
findings 基于新代码」这种两边都不再自洽、却完全无声的状态。

**版本归档（最新 + 归档）**：
- 每模块子目录固定保留最新产物，`history/<run_ts>/` 保存历史版本；
- 上次运行已完成（项目总报告 `report.md` 存在）时，启动前将各模块的 `findings_raw.json`、`findings_verified.json`、`report.md` 归档到 `history/<run_ts>/`，再重新检视；
- 上次运行未完成（总报告缺失）时视为中断，直接续跑，不归档；
- **输入指纹变化时全量归档**：scan 阶段比对 `run_manifest.json` 发现输入变了（源码、模型、
  提示词、阈值、并发），会把范围更宽的产物一并归档——架构与规模产物同样由源码/提示词派生，
  只归档三阶段产物会让「架构基于旧代码、findings 基于新代码」继续成立。归档发生在写入新
  清单**之前**，否则旧产物会在被归档前就被新清单覆盖。
- CLI `--clean`：先归档三阶段产物，再重置其余当前产物（保留 `history/`），触发全量重算；
- 旧版扫描产物（缺少 `module_id`）会自动重新扫描。

想只重算某一阶段，**删掉对应产物文件**即可，比 `--clean` 精细。

---

## 配置

### 模型配置（`config/models.yaml`）

`active` 指定默认启用项，`defaults` 写跨端点一致的字段，`providers` 写各端点的连接参数，`models` 只写差异：

本工程默认启用 **opencode-go 端点**（`https://opencode.ai/zen/go/v1`）的 `deepseek-v4.1-flash`：

```yaml
active: deepseek-v4.1-flash

defaults:                  # 所有模型的公共默认值（可选）
  type: openai
  temperature: 0.7
  max_retries: 2
  timeout: 60

providers:                 # 同一端点的公共连接参数（可选）
  deepseek:
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY}
  opencode:                # opencode-go 端点；密钥见下方回退链
    base_url: https://opencode.ai/zen/go/v1
    api_key: ${OPENCODE_API_KEY}

models:
  - {name: deepseek-v4.1-flash, provider: opencode, model: deepseek-v4.1-flash}
  - {name: deepseek-chat,       provider: deepseek, model: deepseek-chat}
  - {name: glm-5.3-flash,       provider: opencode, model: glm-5.3-flash}
```

**取值分层**，优先级由低到高：模型条目内置默认值 → `defaults` → `providers.<名>` → `models[]` 条目。即下层写公共字段，上层只写差异。

| 字段 | 说明 |
| --- | --- |
| `name` | 唯一名称，供切换时引用；`active`、`--model`、`LLM_PROFILE` 均填此名，**不是 provider 名** |
| `model` | 传给供应商的模型标识（必填） |
| `provider` | 引用 `providers` 中的键（可选，省略即为旧格式条目） |
| `type` | 模型类型，当前支持 `openai`（OpenAI 兼容端点） |
| `base_url` | API Base URL（合并后仍必填） |
| `api_key` | 密钥：字面量或 `${ENV_VAR}`，支持 `${ENV_VAR:-默认值}` |
| `temperature` / `max_retries` / `timeout` | 采样温度（0.7）/ 重试次数（2）/ 超时秒数（60） |
| `seed` | 采样种子（可选）。写上同一个整数可压低重复运行之间的方差；**不写 = 不发送该参数**（供应商自由采样），注意「不写」与「写 `0`」不同 |
| `extra_headers` | 附加请求头（可选） |

**密钥回退链**：模型条目的 `api_key` 展开后为空时，运行期依次回退到
`DEEPSEEK_API_KEY` / `LLM_API_KEY` 环境变量，最后读 `$OPENCODE_CONFIG`
（默认 `~/.config/opencode/opencode.json`）。读全局配置时**先按 provider 名、再按
`base_url` 匹配 provider 块**，因此 `providers.opencode` 写 `${OPENCODE_API_KEY}`
（未设）而密钥实际在 opencode.json 的 `opencode-go` 块里时，仍能取到。
`--list-models` 的 KEY 列复用**同一条链**，故它显示「已配置」时运行期一定能拿到密钥
（曾出现列表报「未配置」、实际却能跑起来，让人据此误判该换模型）。**任何输出都不打印密钥本体。**

**角色路由**（可选 `roles` 块）：按阶段指定不同模型，例如让检视与反驳用强模型、
纯文字加工的摘要用便宜的：

```yaml
roles:
  review: deepseek-reasoner     # 检视是效果的决定性环节
  rebuttal: deepseek-reasoner   # 反驳需要与检视不同的视角
  summary: deepseek-chat        # 纯文字加工
```

可写角色：`arch`（全局/模块架构、关键路径）、`review`（模块检视）、`rebuttal`（反驳）、
`summary`（模块执行摘要）。**角色名与目标都会校验**——写错一个字母会直接报错，
不会静默失效（否则用户以为自己换过模型了）。未列出的角色走默认解析链。
当前映射用 `--list-models` 查看。

合并规则：

- `defaults` 与 `providers.<名>` 只接受上表中的 `type`、`base_url`、`api_key`、`temperature`、`seed`、`max_retries`、`timeout`、`extra_headers`；写入 `provider` / `name` / `model` 会直接报错（否则会给所有条目静默注入）。
- **空值一律视为「未填写」**，沿用下层取值（`timeout:` 与 `timeout: ""` 均不会清掉下层配置）；`extra_headers` 按**键**浅合并，同名键由上层覆盖、下层其余键保留，其余字段整体覆盖。
- 迁移成本：新增模型只需追加 `{name, provider, model}` 一行；换密钥或改 endpoint 只动 `providers` 一处。
- **向后兼容**：不含 `provider` 键的条目即旧格式，可与新格式混用；旧配置文件（无 `providers` / `defaults`）行为完全不变。旧格式条目同样会继承 `defaults`。

切换优先级（高到低）：

1. 命令行 `--model NAME`
2. `roles` 角色路由（按阶段指定，只作用于被指定的角色）
3. 环境变量 `LLM_PROFILE`
4. 配置文件 `active`

`roles` 压过 `LLM_PROFILE`：后者是全局默认项，而角色是配置文件里逐阶段的显式指定；
让一个全局默认无声作废整套路由，是「配置写了没反应」的又一变体。

当前生效项与全部可选模型可用 `--list-models` 查看（配置有误时单行中文报错 + 退出码 1，不打印 traceback）：

```bash
.venv/bin/python scripts/run_review.py --list-models
```

### 模块配置（`config/modules.yaml`）

模块按**文件级**定义：每个 `.c` 文件为一个模块，同目录同名 `.h` 自动并入；标识人工映射。**所有扫描到的 `.c` 必须被映射，否则启动报错。**

```yaml
modules:
  - id: M01
    path: buffer.c
  - id: M02
    path: parser.c
  - id: M03
    path: resource.c
```

### 环境变量

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` / `OPENCODE_API_KEY` | 供 `models.yaml` 中 `${...}` 引用 |
| `LLM_PROFILE` | 覆盖当前启用模型，填**模型 `name`**（如 `deepseek-chat`），不是 provider 名；被 `--model` 与 `roles` 压过 |
| `MODEL_CONFIG` | 自定义模型配置文件路径（默认 `config/models.yaml`） |
| `MODULES_CONFIG` | 自定义模块映射文件路径（默认 `config/modules.yaml`） |
| `LOG_LEVEL` | 控制台与文件日志等级（默认 `INFO`；被 `--log-level` 压过） |
| `RUN_REAL_LLM_TESTS` | 设为 `1` 启用真实联网的测试用例（默认关闭，见[测试](#测试)） |

---

## 使用方法

### 命令行

```bash
.venv/bin/python scripts/run_review.py \
  --project /path/to/c_project \
  --modules all \
  --concurrency 4 \
  --fail-on 严重 \
  --model deepseek-v4.1-flash \
  --output-root output/review \
  --log-level INFO
```

| 参数 | 说明 |
| --- | --- |
| `--project` | 被测 C 项目根目录（跑评审时必填；仅使用 `--list-models` 时可省略） |
| `--modules` | 模块选择，见下（默认 `all`） |
| `--model` | 模型配置名（`config/models.yaml` 中的 `name`），留空取 `active` |
| `--list-models` | 列出全部可选模型、当前生效项与密钥是否已配置，随后退出（无需 `--project`；不打印密钥本体） |
| `--small-line-threshold` | 小模块行数阈值（模块 `.c` 与 `.h` 物理行数之和，默认 2000；`≤` 阈值走小模块分支） |
| `--output-root` | 产物输出目录（默认 `output/review`） |
| `--concurrency N` | 模块检视循环的并发数：`0`=自动（取 `min(模块数, 8)`，默认）、`1`=串行、`N>=1` 为显式上限。仅作用于模块循环，架构阶段与总报告仍串行 |
| `--fail-on` | 检出达到该等级的**已确认**问题时以退出码 2 结束：`none`（默认，只看模块失败）/ `严重` / `致命` |
| `--clean` | 清产物重跑：先归档三阶段产物，再全量重算 |
| `--log-level` | 控制台日志等级：`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

### 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 评审完成，且未触发 `--fail-on` 门槛 |
| `1` | 有模块评审失败（结果不完整）；失败清单同时进报告与 stdout JSON |
| `2` | 评审完成，但检出的已确认问题达到 `--fail-on` 门槛（供构建门禁使用） |

`--fail-on` 只统计 **confirmed**：`uncertain` 是需要人看的，不该让构建失败。判定依据是
`findings.json` 的 `worst_confirmed_severity`（由报告节点落盘时写好），CLI 不自行重算，
避免「报告里写致命、门禁却放行」这类两处判定不一致；产物缺失时门槛不生效——宁可放行，
也不能凭一个读不到的产物拦下构建。

```bash
# 构建门禁用法：检出「严重」及以上已确认问题时让 CI 失败
.venv/bin/python scripts/run_review.py --project /path/to/c_project --fail-on 严重
```

**模块选择语法**：

| 写法 | 含义 |
| --- | --- |
| `all` | 全部模块，按标识升序 |
| `M01` | 单个模块 |
| `M01,M03` | 多个模块 |
| `M01-M05` | 区间（按标识顺序） |
| `M01-M03,M08` | 区间与单个组合 |

## 日志

- 控制台与文件 `output/llm_test.log` 按**同一等级**过滤，默认 `INFO`，由 `--log-level` / `LOG_LEVEL` 设置；
- 每条日志带 `[节点]` 前缀（如 `[review_module]`），标明当前正在执行的图节点；并发模块循环的线程在提交任务时复制上下文，故同样带所属节点；
- `DEBUG` 含约每 5 秒一条的 LLM 流式心跳（chunk 数、思维链 / 正文字数、阶段）与逐条中间状态，默认关闭——它会淹没阶段结论。需要排查底层细节时显式设 `--log-level DEBUG`。

---

## 质量评测

没有度量就只能靠手感判断「改了提示词是变好还是变坏」。`scripts/eval_review.py` 读**既有产物**、
零额度、离线可跑，把一次运行的检视质量算成可比数字。

```bash
# 单次运行
.venv/bin/python scripts/eval_review.py --run output/review

# 前后对比（基线 vs 改造后），差异表直接回答「变好还是变坏」
.venv/bin/python scripts/eval_review.py --run output/review --baseline /path/to/baseline/review

# 只要机器可读摘要
.venv/bin/python scripts/eval_review.py --run output/review --json
```

产物：`<run>/eval_report.md` + stdout 的 JSON 摘要。人工 ground truth 在
`eval/expected_findings.yaml`（每项含 `id/file/line/category/severity/desc`，并标注来源
`gcc-analyzer` / `cppcheck` / `manual`，以便区分「工具可证」与「仅人能判」）。

**指标为什么这样定义**：朴素的「confirmed / 全部 findings = 精确率」方向是错的——按反驳
提示词的契约，`refuted` 必须给出具体反证，所以**一条被正确反驳的 finding 是反驳阶段的成功，
不是误报**；按那个定义打分，一个把所有东西都压下去的实现会拿到最高分。因此分开报告：

| 指标 | 含义 |
| --- | --- |
| `recall_high` | 高严重度内核的召回，**主指标** |
| `precision_confirmed` | **已确认**条目里有多少对得上 ground truth（真正的误报率） |
| `suppression_correctness` | **已反驳**条目里有多少确实不在 ground truth 中（正确压制） |
| `false_refuted` | 被反驳但**确实存在**的缺陷，即漏报——这条流水线最危险的失败模式 |
| `contradiction_rate` | 同一主张出现互相冲突结论的比例。按**同一主张**（锚点 + 缺陷种类）统计，不按同一行：同一语句上不同主张得出不同结论是正常的 |
| `duplication_rate` | 同一主张重复入册的比例 |
| observations 占比 | 未经反驳验证的条目占比 |

## 测试

```bash
cd agent
.venv/bin/python -m unittest discover -s tests          # 全部（离线，秒级）
.venv/bin/python -m unittest tests.test_findings_util -v # 单个文件
```

**venv 里没有 pytest，一律用 stdlib `unittest`**。测试文件自行 `sys.path.insert(..., "src")`
自举（本仓库的 editable 安装不生效），故不依赖 `pip install`，两种调用方式都成立。
`tests/_support.py` 只放夹具、不含断言，导入即把包内日志文件重定向到临时目录，避免测试
污染真实的 `output/llm_test.log`。

默认全离线。真实联网的端到端用例（`tests/test_smoke_review.py`）与根目录
`tests/test_llm_demo.py::RealCallTest` 由**同一个开关** `RUN_REAL_LLM_TESTS=1` 启用，
不开则整类 skip：

```bash
RUN_REAL_LLM_TESTS=1 .venv/bin/python -m unittest tests.test_smoke_review -v
```

冒烟用例只钉**确定性不变量**（图上跑通、产物齐全且可解析、报告里每个位置都指向真实存在的
代码行、identity 唯一、`counts` 与 findings 数一致），**不评质量**——质量由上面的评测脚本
回答。两者混在一起，一次模型波动就会让它变红，而它想报的其实是「管线断了」。

---

## 关键判定边界

流程里有几处「看着对、实际会静默造成漏报或重复」的判定，单列出来说明它们各自把线划在哪。

### 工具证据补漏

工具证据层（`gcc -fanalyzer` / `cppcheck`）会为模型**一字未提**的主张补一条候选 finding，依据是「工具报告 ⇒ 该位置确实有问题」。补之前有两道确定性判定，任一命中即不补：

| 判定 | 含义 | 为什么必需 |
| --- | --- | --- |
| 同位置 | 模型在该语句（±1 行）上已有主张，无论缺陷种类是否相同 | 工具报的是不是同一件事，由 `review_check` 拿工具证据裁决；此处再补一条只会让同一条语句重复出卡 |
| 同函数同种类 | 同一函数内该缺陷种类模型已主张过 | 工具会为**同一处缺陷的每条路径**各报一次（`load_file` 的两条 `return` 各报一条 file-leak），模型只报一次且报在 `fopen`；只看行号这三条互不相识，一次 fd 泄漏会在报告里出三张卡 |

两道判定各自弃用了多少条，分别记进 `funnel.tool_redundant`（前两条）与 `funnel.tool_rejected`（位置校验未通过）——「本次补漏 0 条」必须能区分「工具没报」「全被判重复」「位置校验没过」这三种完全不同的情形。

### 检查项闭环补检

架构阶段会把识别到的风险点落到 `file:line` 形成检查项，本意是给检视阶段留下已定位的线索。但首轮检视是**开放式**的，会稳定地漏掉其中一部分；原流程对这些漏检只做事后记账（`checklist_gaps`），**没有任何动作去补**。`review_module` 现在把它变成闭环：

1. 首轮检视 enrich 之后，用**关切族**（`category_family`）判定每个已定位检查项是否已被回应；
2. 对未回应者追加**一次**定向补检（`source_pass=repair`），prompt 只含这些检查项及其所在函数片段，要求逐项给结论；
3. 补检结果并入同一套 enrich / 去重 / 上限 / 反驳 / 审查流水线，与首轮结论同等对待；
4. 最终 `checklist_gaps` 仍按**位置**从宽记账，避免关切族误判留下永久假警报。

判据松紧按用途分两档，这是刻意的：

| 用途 | 判据 | 理由 |
| --- | --- | --- |
| 触发补检 | 位置邻近 **且** 关切族一致 | 同一行可并存多个都成立的缺陷（实测 `parser.c:13` 上「源指针不推进」「缺长度校验」「定长拷贝无 NUL」三件事）。按位置销账时，报了一件事就把另两件的检查项一起抹掉 |
| 最终记账 | 位置邻近 **或** 同函数声明行 | 这一节是给人看的督促，**假警报比漏报更有害**：整节误报会让人整体忽略它 |

触发条数记进 `funnel.checklist_repair`。它为 0 既可能是「首轮已全部回应」，也可能是「架构没给出定位」——所以它是「架构→检视交接是否闭合」的指标，不等于问题数。

### 反驳阶段的判定边界

反驳代理是证伪者，但有两类「推翻」被明确禁止（见 `prompts/rebuttal.py` 第 6.1、6.2 节）：

- **前置条件不得单独作为反证。** 当 finding 的核心断言正是「该前置条件无法由接口表达、也无法在被检函数内校验」时（接口不含容量参数、入参不含长度、返回 `void` 无法上报失败），声明出来的前置条件恰恰**证实**了该断言。实测代价：`buffer.c:7` 的无界 `strcpy` 曾以「`buffer.h:6` 已把 dst 容量列为调用方前置条件」为由被判 `refuted`——而那正是这条 finding 要说的事，是一次真实漏报。
- **找不到调用方 ≠ 已推翻。** `call_chain` 为空、上游调用点缺失属于**证据缺失**（近似调用图本就不完整，模块外的调用方同样不可见），应判 `uncertain`。`refuted` 的反证必须是代码事实：该语句不成立、该校验已存在、该分支不可达、该资源已释放。

豁免清单的第一条（「契约明确由调用方负责」）也据此收窄为**只豁免并发类断言**——原先它可以被扩用到长度、容量、所有权等任何义务上。

---

## 设计要点与限制

- **检视不依赖编译**：include 图、符号与调用图均为源码文本级近似分析，受宏与函数指针影响可能不完整。但**工具证据层会尽力而为地调用 `gcc -fanalyzer` 与 `cppcheck`**（复用扫描到的 `-I`/`-D`）；工具缺失、项目不可编译或超时都只记录原因后跳过，绝不让流程失败。
- **工具证据是补充而非替代**：实测两个工具合起来只能覆盖 `test_project` 18 项已知缺陷中的 6 项，其中最重要的一项——`buffer.c:7` 的无界 `strcpy`——**两者都抓不到**（目标缓冲区容量需跨函数推断）。因此**有工具报告 ⇒ 该位置确实有问题**（可作硬证据），**没有工具报告 ⇏ 没问题**——绝不用工具缺席当反证。补漏的口径（哪些情形不重复补）见[工具证据补漏](#工具证据补漏)。
- **finding 身份稳定、结论唯一**：每条 finding 有 `identity`（`锚点|缺陷种类`）。锚点由 `file` + 行号 + 源文本指纹构成，与模型措辞无关且源码一变就变；缺陷种类取自闭集枚举，避免模型自由标签把同一缺陷拆成多条。去重键**不含模型自赋的 `category`**（那正是同一处 `strcpy` 溢出曾在一份报告里出现三次、结论互相矛盾的原因）。去重按 `claims_match`（锚点 + `kinds_overlap`）而非精确键相等：**一侧只给族哨兵、另一侧给了显式种类时视为同一主张**，因为哨兵一侧连族都可能标错（实测同一处 `strcpy` 首轮标 `ub`、补检标 `memory`），此时以显式一侧为准；两个显式不同种类仍各自保留（`parser.c:13` 一行三个缺陷是净收益）。跨运行匹配用「位置邻近 + 缺陷种类」容差，不追求 id 逐字相等。
- **一致性闸门**：按锚点聚类后**同一主张只允许一条结论**；`confirmed` 与 `refuted` 冲突时降为 `uncertain` 并**保留全部分歧原文**显式标注，不静默择一——强行归并会掩盖「模型对同一位置确有分歧」这个真实信号。同一行上的**不同**主张（缺陷种类不同）各自保留，互不吞并。
- **规模按行数判定**：每个模块读取时先由 `size_analysis` 统计 `.c` 与 `.h` 物理行数并对照阈值（默认 2000 行）判定 `small` / `large`；large 按文件分批检视后合并去重，并对合并结果重新编号以保证 finding id 唯一。
- **小模块生成即审查**：small 分支全量读取后生成 JSON，经格式校验与 LLM 内容审查，不通过则携带问题反馈重新生成，直至通过或达重试上限。
- **检查项逐项回应（闭环）**：上游 `architecture.json` 里已落到 `file:line` 的检查项，检视阶段须**逐项给出结论**（成立则输出 finding，不成立可一句话说明理由）。带行号的检查项是已经定位好的线索，整体略过是最典型的漏检。首轮未回应者由确定性比对（不经 LLM）识别后，**追加一次定向补检**再并入结论，剩余仍未回应者才记进 `checklist_gaps` 并告警——从「事后记账」改为「事中补检」，详见[检查项闭环补检](#检查项闭环补检)。**零发现是合格结果**，不得为凑满检查项而编造 finding。
  命中判定有两条，任一成立即算已回应：**位置邻近**（±2 行，模型的 finding 常指向语句开头而非关键字所在行），或**检查项锚在某个函数的声明行、而本轮结论落在该函数的实现里**（`file.h:6` 的 `void copy_name(...)` 对 `file.c:7` 的函数体）。第二条是必需的：头文件声明是这条记账最常见的假警报来源——实测一轮下来 5 条「已定位但无回应」里有 2 条（`buffer.h:6`/`:7`，都已在 `buffer.c` 的对应函数里给出结论）属于这种误报，而这一节的全部价值就在于被人当真。销账只认**声明行里出现的**函数名，不认检查项正文的顺带提及。
- **双通道分级的边界**：`call_chain` 完整性只约束 **concurrency 与 trust_boundary** 两类 finding——这两类的可达性必须由调用链证明，给不出链路的进 observations。其余类别（memory / ub / resource / logic / macro）**不因链路不全而降级**：只要问题语句与触发条件能在模块内核对，就应作为 finding。这条边界必须在提示词的**第三（检视方法）、第八（证据与调用链）、第十（字段级要求）节同时给出**：只改第八节时，第三、十节残留的「链路不完整即降级」旧措辞会让模型按就近文本行事——实测 `buffer.c:7` 的无界 `strcpy` 正是这样被移入 observations、整类移出结论表的。observation 不进反驳流程、不计入结论统计，真实的严重缺陷会因此静默消失（实测一轮下来曾占全部条目的 55%）。
- **反驳结果全量核对**：反驳后由独立 `review_check` 节点逐条核对，确保每条 finding 均有反驳结论；缺失条目降级为 `uncertain` 并留痕，不静默丢弃。
- **模块报告即时生成**：单模块反驳审查通过后即由 `module_report` 渲染 Markdown 并落盘，便于分批查看；项目总报告由 `report` 汇总生成。
- **证伪式验证**：反驳子代理以证伪为目标，结论必附证据链与置信度；`refuted` 留痕剔除，`uncertain` 降级保留待人工确认。
- **反驳是整模块一次请求**（**与设计文档 `review_agent_flow.txt` 2.3(1) 的偏离，此处如实记录**）：文档要求「独立上下文，仅获取单条 finding 与相关代码片段」，实现是整模块打包成一次请求、在提示词内要求**逐条独立证伪**（每条 id 必须给出且仅给出一条结论）。取舍是成本与延迟：逐条请求会把 LLM 调用数放大到 findings 数倍。代价是同一模块内的条目共享一个上下文，**隔离性弱于文档设想**，交叉污染无法从结构上排除。相关补强已做：每条 finding 的入批材料包含**以问题行为中心截取的所在函数源码**、**上游调用点的代码**（不再只有函数名）以及**本模块的接口与并发契约**（豁免判定原本拿不到契约依据，导致契约类断言一律被推翻）。另有两类「推翻」在提示词层面被明确禁止——前置条件不得单独作为反证、找不到调用方一律判 `uncertain`，见[反驳阶段的判定边界](#反驳阶段的判定边界)。
- **问题解释结构化**：每条 finding 含问题描述、根因分析（须指出具体 `file:line`、数据流或调用链及所违反的契约）、影响、代码证据与修复方案，并保留一条「反驳意见」（confirmed / refuted / uncertain 均给出）；严重等级分「提示 / 一般 / 严重 / 致命」四级。依赖跨模块契约、本阶段无法独立核验的条目会降级展示并显式标注。
- **报告结构**：模块报告依次为执行摘要（LLM 生成，置于文档最前）、汇总（确定性统计）、已确认问题、待人工确认、观察项、已否定问题。已确认/待确认/已否定条目**按锚点归并出卡**（同一语句上的多条主张合成一张卡、分行列出各自结论），避免 23 行的模块渲染出 15 张卡片；**观察项用紧凑表格**呈现并明示「未进入反驳流程、不作为结论」，使其在视觉上无法与经过验证的条目混淆。
- **证据可回溯**：所有 finding 引用真实 `file:line`；模块报告将位置渲染为可点击的 `file:line` 链接。位置会经过校验（文件须在扫描范围内、行号须在界内、不得落在空行或注释上），不合格的条目降级为 observation。项目总报告开头记录**溯源信息**：模型、采样参数、阈值、输入指纹与生成时间。
