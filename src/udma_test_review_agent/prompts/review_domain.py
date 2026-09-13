"""大型模块单文件域检视提示词（large 分批分支）。

本模块只保留**本路径专有的**内容：域检视的输入说明段与组装顺序。
其余共享片段来自 `._shared`（唯一副本），节号与整合前一致，交叉引用不受影响。

与 `review.py` 的差别仅在第二节（输入说明）与第三节的域检视方法：
本路径每次只看一个 `.c`，头文件签名另给作契约参照。
"""

from ._shared import (
    _ROLE,
    _METHOD,
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
from ._shared import _CHECKLIST_ALL

DOMAIN_REVIEW_PROMPT = (
    _ROLE
    + """# 二、输入说明（大型模块的单文件域检视）
模块架构摘要（接口契约/并发契约/检查项）：
{module_arch}

模块公共接口签名表（头文件，供调用契约参照，不在本次检视范围内）：
{headers}

本模块涉及的关键路径（跨模块数据流与并发时序，用于判断缺陷是否真实可达）：
{critical_paths}

"""
    + """# 三、本域检视方法（按序执行）
1. 通读本文件源码，结合接口契约建立函数内数据流；
2. 逐个函数识别候选缺陷，重点检查边界、错误路径、资源释放与外部输入使用；
3. 对每个候选核验触发条件与证据，剔除猜测与纯风格问题；
4. 证据不足或链路不完整者降级为 observations；
5. 跨模块契约相关项参照头文件签名判断，但不臆断未给出的实现。

"""
    + _CHECKLIST_ALL
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
本域源码（带行号）：
{source}
"""
)

