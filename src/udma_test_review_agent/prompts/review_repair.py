"""检查项补检提示词：只回答上游已定位、而首轮检视未回应的检查项。

这是「架构阶段 → 检视阶段」信息交接的闭环环节。首轮检视是**开放式**的——模型自由
决定看哪里、报什么；补检是**定向**的——把上游已经定位到 file:line、却没有任何对应结论
的检查项连同其所在函数片段交回给模型，要求逐项给出结论。开放式检视会稳定地漏掉一部分
已定位线索（实测 `parser.c:13` 一行上并存的长度校验、定长拷贝无 NUL 等缺陷只报出一件），
定向补检把「漏检」从事后记账变成一次会追加的检视动作。

判据、JSON 契约、字段要求、自检与其它检视路径同源（`._shared`），故此处只保留
补检专有的输入说明与组装顺序。
"""

from ._shared import (
    _ROLE,
    _EVIDENCE,
    _FALSE_POSITIVE,
    _SEVERITY,
    _JSON_CONTRACT,
    _FIELD_RULES,
    _NEGATIVE,
    _SELF_CHECK,
)

CHECKLIST_REPAIR_PROMPT = (
    _ROLE
    + """# 二、输入说明（检查项补检）
首轮检视之后，下列**上游架构阶段已经定位到 file:line 的检查项**仍没有任何对应结论。
请**只针对这些检查项逐项核对**，不要扩展到其它问题：
- 核对后认为成立：输出为 finding，`file`/`line` 应与该检查项一致（或指向真正的问题语句）；
- 核对后认为不成立：**直接省略即可，不要为此输出任何条目**——零发现是合格结果，
  不得为凑满检查项而编造 finding，也不要用 observation 记录「已核对但不成立」；
- 仅当确有疑点、但证据不足以定性时才放入 observations（与 findings 同一套标准）；
- 若某项与首轮已有主张是同一问题，不要重复报告。

模块架构摘要（职责/接口契约/并发契约）：
{module_arch}

待回应的检查项（每条附所在函数片段）：
{open_items}

"""
    + _SEVERITY
    + _EVIDENCE
    + _FALSE_POSITIVE
    + """# 九、输出 JSON 契约
"""
    + _JSON_CONTRACT
    + _FIELD_RULES
    + _NEGATIVE
    + _SELF_CHECK
    + """# 十五、相关源码
{source}
"""
)
