# udma_test_review_agent

面向 Linux C 项目的自动化代码检视智能体，基于 **LangGraph** 有状态图编排。

它按**文件级模块**组织检视对象，先做全局与模块架构分析，再逐模块检视，并由独立的**反驳子代理**对每条结论做证伪式验证，最终产出人类可读的报告与机器可消费的结构化结论。全流程不依赖编译。

框架结构模板见 `docs/AGENT框架说明.md`，请求处理行为规范见仓库根目录 `AGENTS.md`。

---

## 快速开始

```bash
# 1. 安装（要求 Python >= 3.10）
cd agent
python -m venv .venv
.venv/bin/pip install -e .

# 2. 配置密钥（供 config/models.yaml 中的 ${DEEPSEEK_API_KEY} 引用）
export DEEPSEEK_API_KEY=sk-...

# 3. 编辑 config/modules.yaml，把被测项目的每个 .c 映射到 M01、M02…

# 4. 运行：全部模块串行检视
.venv/bin/python scripts/run_review.py --project /path/to/c_project --modules all

# 5. 查看报告
cat output/review/report.md
```

---

## 基本架构

### 处理流程

```
START
  │
  ▼
scan(S0)            确定性：文本级扫描 → 模块清单 / include 图 / 近似调用图
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
┌──────────────── 模块循环（按模块标识升序） ────────────────┐
│  size_analysis      确定性：统计 .c/.h 物理行数与字符数，判定 small/large │
│  review_module      LLM：small 全量生成+格式与内容审查重生成；large 按文件分批 │
│  rebuttal_module    LLM：整模块一次请求批量证伪验证         │
│  review_check       确定性：核对每条 finding 均有反驳结论并落盘 │
│  module_report      LLM：单模块 Markdown 格式化 + 执行摘要   │
│  advance            确定性：推进到下一模块                  │
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
| `scan` | 工具 | 文本级扫描：模块清单、include 图、近似调用图、函数表 | `scan.json` |
| `global_arch` | LLM | 全局分层、信任面入口、并发热点、项目惯例 | `global_architecture.json` |
| `module_archs` | LLM | 逐模块接口契约、并发契约、检查项 | `<ID>/architecture.json` |
| `critical_paths` | LLM | 并发时序与污点传播关键路径 | `critical_paths.json` |
| `merge` | 工具 | 跨模块确定性校验（如头文件声明未见实现） | 状态中的候选列表 |
| `size_analysis` | 工具 | 统计模块 `.c` 与 `.h` 物理行数、字符数，按行数阈值判定 `small`/`large` | `<ID>/size.json` |
| `review_module` | LLM | 单模块检视：small 全量读取并生成 JSON，经格式与内容审查不通过则重新生成；large 按文件分批，高风险模块追加专项检视 | `<ID>/findings_raw.json` |
| `rebuttal_module` | LLM | 整模块一次请求批量证伪；漏返回条目单独重发，仍缺失则标记 `uncertain` | 状态中的反驳结果 |
| `review_check` | 工具 | 核对每条 finding 均有反驳结论（缺失补 `uncertain`），通过后落盘 | `<ID>/findings_verified.json` |
| `module_report` | LLM | 单模块 Markdown 格式化 + LLM 执行摘要，即时落盘 | `<ID>/report.md` |
| `advance` | 工具 | 模块循环推进 | — |
| `report` | 工具 | 汇总各模块报告与统计、跨模块候选，生成项目总报告 | `report.md` |

### 目录结构

```
agent/
├── config/
│   ├── models.yaml            # 多模型配置
│   └── modules.yaml           # 文件级模块标识映射（M01 → 路径）
├── scripts/
│   ├── run_review.py          # 命令行入口
│   └── test_single_llm_node.py# 单 LLM 节点示例
├── src/udma_test_review_agent/
│   ├── graph.py               # 图组装与编译（导出 graph）
│   ├── state.py               # 图状态 ReviewState
│   ├── configuration.py       # 运行期配置 ReviewConfig
│   ├── model_config.py        # 模型配置加载与解析
│   ├── module_map.py          # 模块映射与选择解析
│   ├── llm.py                 # 唯一 LLM 出口：流式、超时、心跳、schema 校验
│   ├── log.py                 # 日志：控制台按等级，文件恒 DEBUG
│   ├── schemas.py             # 各阶段结构化产物模型（Pydantic）
│   ├── prompts/               # 提示词子包（按节点拆分）
│   ├── nodes/
│   │   ├── common.py          # 节点通用辅助
│   │   ├── llm/               # 大模型节点（架构 / 检视 / 反驳 / 模块报告）
│   │   └── tool/              # 确定性工具节点（扫描 / 跨模块校验 / 尺寸分析 / 反驳审查 / 推进 / 汇总）
│   └── tools/executors/
│       └── c_scanner.py       # S0 纯文本 C 扫描器
└── pyproject.toml             # 依赖与打包
```

### 产物与版本管理

产物写入 `--output-root`（默认 `output/review`），**每模块一个子目录**：

| 路径 | 内容 |
| --- | --- |
| `scan.json` | S0 扫描结果 |
| `global_architecture.json` | S1 全局架构 |
| `critical_paths.json` | S3 关键路径 |
| `<ID>/architecture.json` | 模块架构摘要 |
| `<ID>/size.json` | 模块尺寸（行数 / 字符数 / small·large） |
| `<ID>/findings_raw.json` | 检视原始发现（findings + observations） |
| `<ID>/findings_verified.json` | 反驳并经审查节点核对后的结论（confirmed / refuted / uncertain） |
| `<ID>/report.md` | 单模块报告（反驳审查后即时生成） |
| `<ID>/history/<run_ts>/` | 历史版本归档（三阶段产物） |
| `report.md` | 项目总报告 |

**断点续跑**：任一产物已存在即跳过对应阶段。

**版本归档（最新 + 归档）**：
- 每模块子目录固定保留最新产物，`history/<run_ts>/` 保存历史版本；
- 上次运行已完成（项目总报告 `report.md` 存在）时，启动前将各模块的 `findings_raw.json`、`findings_verified.json`、`report.md` 归档到 `history/<run_ts>/`，再重新检视；
- 上次运行未完成（总报告缺失）时视为中断，直接续跑，不归档；
- CLI `--clean`：先归档三阶段产物，再重置其余当前产物（保留 `history/`），触发全量重算；
- 旧版扫描产物（缺少 `module_id`）会自动重新扫描。

---

## 配置

### 模型配置（`config/models.yaml`）

`active` 指定默认启用项，`defaults` 写跨端点一致的字段，`providers` 写各端点的连接参数，`models` 只写差异：

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
  opencode:
    base_url: https://opencode.ai/zen/go/v1
    api_key: ${OPENCODE_API_KEY}

models:
  - {name: deepseek-v4.1-flash, provider: deepseek, model: deepseek-v4.1-flash-expires-on-0910}
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
| `extra_headers` | 附加请求头（可选） |

合并规则：

- `defaults` 与 `providers.<名>` 只接受上表中的 `type`、`base_url`、`api_key`、`temperature`、`max_retries`、`timeout`、`extra_headers`；写入 `provider` / `name` / `model` 会直接报错（否则会给所有条目静默注入）。
- **空值一律视为「未填写」**，沿用下层取值（`timeout:` 与 `timeout: ""` 均不会清掉下层配置）；`extra_headers` 按**键**浅合并，同名键由上层覆盖、下层其余键保留，其余字段整体覆盖。
- 迁移成本：新增模型只需追加 `{name, provider, model}` 一行；换密钥或改 endpoint 只动 `providers` 一处。
- **向后兼容**：不含 `provider` 键的条目即旧格式，可与新格式混用；旧配置文件（无 `providers` / `defaults`）行为完全不变。旧格式条目同样会继承 `defaults`。

切换优先级（高到低）：

1. 命令行 `--model NAME`
2. 环境变量 `LLM_PROFILE`
3. 配置文件 `active`

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
| `LLM_PROFILE` | 覆盖当前启用模型，填**模型 `name`**（如 `deepseek-chat`），不是 provider 名 |
| `MODEL_CONFIG` | 自定义模型配置文件路径（默认 `config/models.yaml`） |
| `MODULES_CONFIG` | 自定义模块映射文件路径（默认 `config/modules.yaml`） |
| `LOG_LEVEL` | 控制台日志等级（默认 `INFO`） |

---

## 使用方法

### 命令行

```bash
.venv/bin/python scripts/run_review.py \
  --project /path/to/c_project \
  --modules all \
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
| `--clean` | 清产物重跑：先归档三阶段产物，再全量重算 |
| `--log-level` | 控制台日志等级：`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

**模块选择语法**：

| 写法 | 含义 |
| --- | --- |
| `all` | 全部模块，按标识升序 |
| `M01` | 单个模块 |
| `M01,M03` | 多个模块 |
| `M01-M05` | 区间（按标识顺序） |
| `M01-M03,M08` | 区间与单个组合 |

## 日志

- 控制台按 `--log-level`（或 `LOG_LEVEL`，默认 `INFO`）过滤；
- 日志文件 `output/llm_test.log` **始终记录 DEBUG**，包含约每 5 秒一条的 LLM 流式心跳（已接收 chunk 数、思维链 / 正文字数、阶段），便于事后排查。

---

## 设计要点与限制

- **不依赖编译**：include 图、符号与调用图均为源码文本级近似分析，受宏与函数指针影响可能不完整；不执行 `gcc` / `clang` / `cpp`。
- **规模按行数判定**：每个模块读取时先由 `size_analysis` 统计 `.c` 与 `.h` 物理行数并对照阈值（默认 2000 行）判定 `small` / `large`；large 按文件分批检视后合并去重，并对合并结果重新编号以保证 finding id 唯一。
- **小模块生成即审查**：small 分支全量读取后生成 JSON，经格式校验与 LLM 内容审查，不通过则携带问题反馈重新生成，直至通过或达重试上限。
- **反驳结果全量核对**：反驳后由独立 `review_check` 节点逐条核对，确保每条 finding 均有反驳结论；缺失条目降级为 `uncertain` 并留痕，不静默丢弃。
- **模块报告即时生成**：单模块反驳审查通过后即由 `module_report` 渲染 Markdown 并落盘，便于分批查看；项目总报告由 `report` 汇总生成。
- **证伪式验证**：反驳子代理以证伪为目标，结论必附证据链与置信度；`refuted` 留痕剔除，`uncertain` 降级保留待人工确认。
- **批量反驳**：整模块一次请求产出全部结论；模型漏返回的条目会单独重发，仍缺失则由审查节点标记 `uncertain`。
- **问题解释结构化**：每条 finding 含问题描述、根因分析（须指出具体 `file:line`、数据流或调用链及所违反的契约）、影响、代码证据与修复方案，并保留一条「反驳意见」（confirmed / refuted / uncertain 均给出）；严重等级分「提示 / 一般 / 严重 / 致命」四级。
- **报告结构**：模块报告依次为执行摘要（LLM 生成，置于文档最前）、汇总（确定性统计）、已确认问题、待人工确认、观察项、已否定问题；四类条目统一采用标准卡片格式，已否定问题置于文末留痕。
- **证据可回溯**：所有 finding 引用真实 `file:line`；模块报告将位置渲染为可点击的 `file:line` 链接。
