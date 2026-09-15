"""模块检视（大模型）：全量读入模块源码与背景信息，产出检视意见。"""

from __future__ import annotations

from ..artifacts import read_source_files, write_json
from ..llm import stream_llm_once
from ..log import get_logger
from ..prompts.review import LINE_RULE_PER_FILE, REVIEW_PROMPT
from ..schemas import SEVERITIES, ReviewResult
from ..state import ReviewState

logger = get_logger(__name__)


def review_llm(state: ReviewState) -> dict:
    config = state["config"]
    module = state["module"]
    background = state.get("background") or ""
    logger.info(
        "检视: 模块=%s 外部依赖=%s", module["id"], f"{len(background)} 字符" if background else "无"
    )
    prompt = REVIEW_PROMPT.format(
        module_id=module["id"],
        background=background or "（本模块没有用到其他模块的符号）",
        source_code=read_source_files(config.project_root, module["files"]),
        severities=" / ".join(SEVERITIES),
        line_rule=LINE_RULE_PER_FILE,
    )
    result = stream_llm_once(prompt, ReviewResult, **config.llm_options())
    findings = [finding.model_dump() for finding in result.findings]
    write_json(config.artifact_path(module["id"], "raw_findings"), findings)
    logger.info("检视完成: 模块=%s 意见=%d 条", module["id"], len(findings))
    return {"findings": findings}
