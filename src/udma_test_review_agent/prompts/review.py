"""单模块综合检视提示词（small 全量分支）。

本模块只保留**本路径专有的**内容：输入说明段与提示词组装顺序。
角色 / 检视方法 / 分类检查清单 / 跨模块契约 / 误报边界 / 定级 / 证据 / JSON 契约 /
字段规则 / 负向约束 / 自检 / 示例 / 边界，全部来自 `._shared`（唯一副本）——
三条检视路径共用同一批判据，改动只在一处生效，不会再出现「改了 A 忘了 B」的静默分叉。

节号（一/三/四/五/六/七/八/九/十/十一/十二/十三/十四/十五）与整合前一致，
故正文里的「见第五、六节」「按第十二节自检」等交叉引用仍然有效。
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

REVIEW_PROMPT = (
    _ROLE
    + """# 二、输入说明（本模块综合检视）
本模块架构摘要（职责/接口契约/并发契约/检查项）：
{module_arch}

全局相关背景（信任面/并发热点中涉及本模块的条目）：
{global_context}

本模块涉及的关键路径（跨模块数据流与并发时序，用于判断缺陷是否真实可达）：
{critical_paths}

检视清单（按类别组织，逐项核对）：
{checklist}

"""
    + _METHOD
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
模块源码（带行号）：
{source}
"""
)

