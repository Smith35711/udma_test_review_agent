"""检视产物的格式校验（确定性）：只校验格式，不校验内容。

与 `rebuttal_check_tool.py` 对称：两个阶段的大模型节点都只负责产出，格式由紧随
其后的确定性节点把关，产物也都按 raw / checked 落两份。

校验口径：行号为正整数、严重等级在闭集内、各项文本非空。字段类型由
`schemas.Finding` 保证，这里补的是它管不到的形式要求。**规则集中在这一处**，
别处不再重复校验。

不通过即终止当前模块（需求规格 §5.4）：不进入反驳与报告，其余模块照常执行。
"""

from __future__ import annotations

from ..artifacts import write_json
from ..log import get_logger
from ..schemas import SEVERITIES
from ..state import ReviewState

logger = get_logger(__name__)

_SEVERITY_SET = frozenset(SEVERITIES)
_REQUIRED_TEXT_FIELDS = ("file", "description", "evidence", "suggestion")


def _format_problem(finding: dict, index: int) -> str:
    """返回第 index 条意见的格式问题；没有问题返回空串。"""
    line = finding.get("line")
    if not isinstance(line, int) or line < 1:
        return f"第 {index} 条的行号不是正整数：{line!r}"
    severity = finding.get("severity")
    if severity not in _SEVERITY_SET:
        return f"第 {index} 条的严重等级不在闭集内：{severity!r}"
    for field in _REQUIRED_TEXT_FIELDS:
        if not str(finding.get(field, "")).strip():
            return f"第 {index} 条的 {field} 为空"
    return ""


def review_check_tool(state: ReviewState) -> dict:
    module_id = state["module"]["id"]
    findings = state.get("findings") or []
    for index, finding in enumerate(findings, start=1):
        problem = _format_problem(finding, index)
        if problem:
            logger.error("格式校验不通过: 模块=%s %s，终止该模块", module_id, problem)
            return {"failed": True, "fail_reason": problem}

    # 通过后统一编号：编号只在这里产生，模型不参与，报告与反驳都以此为准
    checked = [{"id": f"f{index}", **finding} for index, finding in enumerate(findings, start=1)]
    write_json(state["config"].artifact_path(module_id, "checked_findings"), checked)
    logger.info("格式校验通过: 模块=%s 意见=%d 条", module_id, len(checked))
    return {"findings": checked, "failed": False}
