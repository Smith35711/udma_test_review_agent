"""高风险模块专项检视提示词（concurrency / trust_boundary）。

本模块只保留**本路径专有的**内容：专项检视的角色定义、输入说明与组装顺序。
分类清单只取并发与信任面两节——这两节的**公共 20 条**与 `_CHECKLIST_ALL` 同源
（`_shared._CONCURRENCY_ITEMS` / `_TRUST_BOUNDARY_ITEMS`），专项另加 10 条。
此前是把公共 20 条整段抄写一遍，抄写的那份一旦漏改就永久分叉。

注意：本路径的角色段与另两条不同（并发与安全评审专家），故不共享 `_shared._ROLE`。
"""

from ._shared import (
    _CROSS_MODULE,
    _FALSE_POSITIVE,
    _SEVERITY,
    _EVIDENCE,
    _JSON_CONTRACT,
    _FIELD_RULES,
    _NEGATIVE,
    _SELF_CHECK,
    _EXAMPLES,
    _EDGE,
)
from ._shared import _CHECKLIST_FOCUSED

FOCUSED_REVIEW_PROMPT = (
    """# 一、角色与目标
你是一名资深 C 语言并发与安全评审专家，长期从事并发缺陷与信任边界漏洞分析。
本模块被标记为高风险（信任面入口/并发热点密集），你的任务是执行一次专项检视，
仅覆盖 concurrency 与 trust_boundary 两个维度，产出结构化 findings。
你的结论将被独立反驳代理逐条证伪；必须只依据真实代码、给出可定位证据与完整推理。

# 二、输入说明（高风险专项检视）
本模块架构摘要：
{module_arch}

本模块涉及的关键路径（跨模块数据流与并发时序，用于判断缺陷是否真实可达）：
{critical_paths}

"""
    + """# 三、专项检视方法（按序执行）
1. 识别所有共享状态、锁、临界区与外部输入进入点；
2. 对每个共享状态追踪跨函数/跨线程的读写时序，核对锁的获取/释放配对与原子性；
3. 对每条外部输入追踪从进入点到校验点再到危险汇点（拷贝、分配、索引、系统调用）的完整路径；
4. 核验触发条件与证据，剔除猜测；链路不完整者降级为 observations；
5. 对确认的问题给出根因、影响、修复方案与严重等级。

"""
    + _CHECKLIST_FOCUSED
    + _CROSS_MODULE
    + _FALSE_POSITIVE
    + _SEVERITY
    + _EVIDENCE
    + """# 九、输出 JSON 契约
"""
    + _JSON_CONTRACT
    + _FIELD_RULES
    + _NEGATIVE
    + _SELF_CHECK
    + _EXAMPLES
    + _EDGE
    + """# 十五、输入数据
模块源码（带行号）：
{source}
"""
)

