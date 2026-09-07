# AGENT 框架说明：langgraph 典型项目结构

## 概述

LangGraph 将 agent 建模为**有状态图**（StateGraph）：节点执行计算，边（含条件边）决定流转方向，共享状态（State）在各节点间传递，Checkpointer 提供持久化与会话记忆。本文以 langchain-ai/langgraph-template 及社区通行的 src 布局为基线，描述通用典型模板；实际工程可按需裁剪或引入 create_react_agent 等高层构造。

## 目录布局

```
<project>/
├── src/
│   └── <package>/              # 应用主包（Python 包）
│       ├── __init__.py         # 包导出
│       ├── state.py            # 图状态定义
│       ├── configuration.py    # 运行期配置
│       ├── log.py              # 日志初始化与配置
│       ├── llm.py              # 模型构造与调用封装
│       ├── nodes.py            # 节点实现
│       ├── tools/              # 工具子包（接口与实现分离）
│       │   ├── __init__.py     # 工具聚合导出
│       │   ├── registry.py     # @tool 接口定义与描述
│       │   └── executors/      # 复杂执行实现
│       │       └── script_runner.py  # 脚本/命令执行器
│       ├── prompts.py          # 提示词模板
│       └── graph.py            # 图组装与编译入口
├── output/                      # 输出产物（报告、生成文件等）
├── temp/                        # 运行时临时文件（不入版本控制）
├── tests/                      # 单元与图级测试
├── pyproject.toml              # 依赖与项目元数据
├── langgraph.json              # LangGraph Platform 部署声明
├── .env.example                # 环境变量样例
└── README.md
```

## 模块职责

| 模块 | 职责 | 关键产出 |
| --- | --- | --- |
| `state.py` | 定义图的共享状态：字段、类型（TypedDict / Pydantic）、各字段更新策略（Reducer，如 `add_messages` 累加消息）。决定节点间传递与合并的语义 | `State`、`Reducer` |
| `configuration.py` | 声明运行期配置（`@dataclass`），如模型名、温度、系统参数；经 `RunnableConfig` 的 `configurable` 注入，支持按调用覆盖 | `Configuration` |
| `log.py` | 统一配置日志（级别、格式、处理器），在包入口完成初始化；各节点与工具以模块级 `logger` 记录执行轨迹，杜绝 `print` 调试 | `Logger` 配置、模块级 logger |
| `llm.py` | 封装模型/供应商客户端构造：统一模型名、参数与密钥注入，收敛重试与错误处理，提供唯一模型实例出口；`graph.py` 与节点经此注入模型，避免重复实例化 | `create_llm()`、模型实例 |
| `prompts.py` | 集中管理提示词：系统提示、模板化用户输入，使用 `ChatPromptTemplate` 等结构 | Prompt 模板对象 |
| `tools/__init__.py` | 工具子包聚合导出，作为模型绑定与节点引用的唯一工具入口 | 工具列表 |
| `tools/registry.py` | 定义模型可见的工具接口层：`@tool` 包装、名称与描述、入参 Schema；接口与实现分离 | 注册工具 |
| `tools/executors/` | 承载复杂工具实现：解析入参、构建并执行命令（`subprocess`）、捕获退出码/`stderr`/超时，异常包装为可读错误 | 执行器模块 |
| `nodes.py` | 实现各节点：以 `(state) -> dict | Command` 签名编写；典型包括模型调用节点、工具执行节点、检索/聚合节点；组合 LLM 与工具形成 agent 循环节点 | 节点函数 |
| `graph.py` | 组装：实例化 `StateGraph`，`add_node` 注册节点，`add_edge` / `add_conditional_edges` 连接流转，`set_entry_point`/`set_finish_point` 定起止，`compile(checkpointer=...)` 编译并导出 `graph` | 编译后的 `CompiledGraph` |
| `__init__.py` | 声明包公共 API，保持引用路径稳定 | 导出符号 |
| `pyproject.toml` | 声明依赖（`langgraph`、`langchain-*`、`langgraph-checkpoint-*` 等）与打包、运行工具（uv/pip） | 环境 |
| `langgraph.json` | 平台部署声明：将 `graph.py` 中编译图映射为 API endpoint，声明依赖来源与构建环境 | 部署配置 |
| `.env.example` | 列出运行所需密钥与变量（模型 API Key 等）的样例 | 环境变量模板 |
| `output/` | 汇聚节点生成的输出产物（评审报告、导出文件、图表等），统一交付与清理 | 输出目录 |
| `temp/` | 存放运行时中间临时文件（下载缓存、分片等）；不入版本控制，会话结束清理 | 临时目录 |
| `tests/` | 以内存 Checkpointer 构造图并 `invoke`，断言状态与输出；验证节点行为与图流转 | 测试套件 |

## 数据流与运行模型

1. **入口调用**：`graph.invoke(input, config={"configurable": {...}})` 或 `astream` 流式调用；`input` 与状态字段合并作为初始状态。
2. **节点执行**：按拓扑依次执行节点；节点返回部分状态（或 `Command` 指示跳转），经 Reducer 合并更新共享状态。
3. **条件路由**：`add_conditional_edges` 依据返回内容（如是否需继续调用工具）在节点间分派。
4. **Agent 循环**：典型 ReAct 模式由“模型节点”→“工具执行节点”→“回到模型节点”的条件边构成，直至模型输出结束标记。
5. **终止与返回**：到达 `__end__` 后返回最终状态；`graph` 亦暴露 `get_state`/`update_state` 支持运行期检查与回退。
6. **持久化与会话**：`compile(checkpointer=...)` 传入 Checkpointer（内存或 SQLite/Postgres），以 `thread_id` 划分会话，支持跨轮记忆与 `interrupt` 人机中断续跑。

## 扩展形态

- **多 agent 子图**：将 `graph` 作为节点嵌入上级图，或经 `Command` 实现层级/并行协作。
- **人机协同**：`interrupt` 挂起等待人工输入，`Command(resume=...)` 恢复。
- **长时记忆**：引入 `BaseStore` 实现跨会话语义记忆，或持久化 Checkpointer 提供短期记忆。
- **部署与前端**：经 LangGraph Platform 暴露 REST 接口；前端以 `stream_mode` 消费流式事件。

## 约定

- 保持节点为无副作用纯函数，副作用收敛于工具层。
- 状态字段显式声明 Reducer，避免隐式覆盖丢失数据。
- 图结构与业务逻辑分离：`graph.py` 只做组装，节点实现下沉至 `nodes.py`。
- 模型实例由 `llm.py` 统一创建并注入节点，节点不得自行实例化模型。
- 日志经 `log.py` 统一配置，节点与工具以模块级 `logger` 记录，禁止 `print` 输出。
- 工具分层：模型仅经 `tools/registry.py` 暴露接口，复杂逻辑置于 `tools/executors/`，接口与实现不得混同。
- 输出产物写入 `output/`，运行时中间文件置于 `temp/`；`temp/` 不入版本控制，会话结束须清理。
