"""检视图的离线测试：假客户端驱动，零额度、秒级。

真实调用只能证明"连得通"，流程分支得靠这里覆盖：格式校验不通过必须在该节点终止，
遗漏补做必须真的回边，补做达上限必须把剩余条目标为「待确认」而不是丢掉。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent2 import llm
from agent2.artifacts import read_json, read_text
from agent2.configuration import RunConfig
from agent2.graph import build_review_graph
from agent2.log import set_log_level
from agent2.nodes.scan_tool import scan_project

set_log_level("WARNING")

C_SOURCE = "int copy(char *dst, const char *src) {\n  return 0;\n}\n"
H_SOURCE = "int copy(char *dst, const char *src);\n"

_REVIEW_OK = json.dumps(
    {
        "findings": [
            {
                "file": "buffer.c",
                "line": 2,
                "severity": "严重",
                "description": "问题甲",
                "evidence": "return 0;",
                "suggestion": "修复甲",
            }
        ]
    },
    ensure_ascii=False,
)

_REVIEW_BAD_SEVERITY = json.dumps(
    {
        "findings": [
            {
                "file": "buffer.c",
                "line": 2,
                "severity": "高",
                "description": "问题甲",
                "evidence": "return 0;",
                "suggestion": "修复甲",
            }
        ]
    },
    ensure_ascii=False,
)

_REVIEW_TWO = json.dumps(
    {
        "findings": [
            {
                "file": "buffer.c",
                "line": 1,
                "severity": "严重",
                "description": "问题甲",
                "evidence": "int copy(",
                "suggestion": "修复甲",
            },
            {
                "file": "buffer.h",
                "line": 1,
                "severity": "提示",
                "description": "问题乙",
                "evidence": "int copy(",
                "suggestion": "修复乙",
            },
        ]
    },
    ensure_ascii=False,
)

_REVIEW_FOUR = json.dumps(
    {
        "findings": [
            {
                "file": "buffer.c",
                "line": 1,
                "severity": "一般",
                "description": "轻问题",
                "evidence": "e1",
                "suggestion": "s1",
            },
            {
                "file": "buffer.c",
                "line": 2,
                "severity": "致命",
                "description": "重问题",
                "evidence": "e2",
                "suggestion": "s2",
            },
            {
                "file": "buffer.c",
                "line": 3,
                "severity": "严重",
                "description": "中问题",
                "evidence": "e3",
                "suggestion": "s3",
            },
            {
                "file": "buffer.h",
                "line": 1,
                "severity": "提示",
                "description": "待定问题",
                "evidence": "e4",
                "suggestion": "s4",
            },
        ]
    },
    ensure_ascii=False,
)

_REBUTTAL_MALFORMED = json.dumps(
    {
        "items": [
            {"id": "f1", "conclusion": "维持", "reason": "r1"},
            {"id": "f9", "conclusion": "维持", "reason": "编号不在清单里"},
            {"id": "f2", "conclusion": "看起来没问题", "reason": "结论不在闭集内"},
            {"id": "f3", "conclusion": "反驳", "reason": "   "},
        ]
    },
    ensure_ascii=False,
)

_REBUTTAL_FOUR = json.dumps(
    {
        "items": [
            {"id": "f1", "conclusion": "维持", "reason": "r1"},
            {"id": "f2", "conclusion": "反驳", "reason": "r2"},
            {"id": "f3", "conclusion": "维持", "reason": "r3"},
            {"id": "f4", "conclusion": "待确认", "reason": "r4"},
        ]
    },
    ensure_ascii=False,
)

_REBUTTAL_F1 = json.dumps(
    {"items": [{"id": "f1", "conclusion": "维持", "reason": "证据确凿"}]}, ensure_ascii=False
)

_REBUTTAL_BOTH = json.dumps(
    {
        "items": [
            {"id": "f1", "conclusion": "维持", "reason": "证据确凿"},
            {"id": "f2", "conclusion": "反驳", "reason": "上游已做长度校验"},
        ]
    },
    ensure_ascii=False,
)


class _FakeStream:
    """最小流式桩件：一个正文 chunk 加一个 usage chunk。"""

    def __init__(self, text: str):
        self._text = text

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self):
        delta = SimpleNamespace(content=self._text, reasoning_content=None)
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None
        )
        usage = SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            completion_tokens_details=None,
            prompt_tokens_details=None,
        )
        yield SimpleNamespace(choices=[], usage=usage)


class _FakeClient:
    """按调用顺序吐出预置回复，并记录每次收到的提示词。"""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: object) -> _FakeStream:
        self.prompts.append(kwargs["messages"][0]["content"])  # type: ignore[index]
        text = self._replies.pop(0) if self._replies else "{}"
        return _FakeStream(text)


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent2-flow-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        (self.project / "buffer.c").write_text(C_SOURCE, encoding="utf-8")
        (self.project / "buffer.h").write_text(H_SOURCE, encoding="utf-8")
        self.config = RunConfig(project_root=str(self.project), output_root=str(root / "output"))
        self.module = scan_project(self.project)[0]

    def _run(self, replies: list[str]) -> tuple[dict, _FakeClient]:
        client = _FakeClient(replies)
        original = llm._build_client
        llm._build_client = lambda spec, base_url=None: client
        self.addCleanup(setattr, llm, "_build_client", original)
        state = build_review_graph().invoke(
            {"config": self.config, "module": self.module, "background": ""}
        )
        return state, client

    def test_scan_pairs_c_with_h(self) -> None:
        modules = scan_project(self.project)
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["id"], "M01")
        self.assertEqual(modules[0]["files"], ["buffer.c", "buffer.h"])
        self.assertEqual(modules[0]["lines"], 3 + 1)

    def test_normal_flow_writes_report(self) -> None:
        state, client = self._run([_REVIEW_OK, _REBUTTAL_F1])

        self.assertFalse(state.get("failed"))
        self.assertEqual(len(client.prompts), 2)  # 检视 + 反驳，摘要不调模型
        checked = read_json(self.config.artifact_path("M01", "checked_findings"))
        self.assertEqual([item["id"] for item in checked], ["f1"])
        report = read_text(self.config.artifact_path("M01", "report"))
        # 摘要是确定性算出来的，直接引用产物里的统计数字
        self.assertIn("本模块共记录 1 条检视意见：维持 1、待确认 0、已被反驳 0", report)
        self.assertIn("**维持**", report)
        # 行号渲染成可跳转链接，指向项目内的绝对路径
        self.assertIn(f"#L2)", report)
        self.assertIn(str(self.project / "buffer.c"), report)

    def test_report_keeps_json_order(self) -> None:
        # 排序是展示层的事：JSON 产物保持模型给出的顺序，编号也按该顺序分配
        self._run([_REVIEW_FOUR, _REBUTTAL_FOUR])

        checked = read_json(self.config.artifact_path("M01", "checked_findings"))
        self.assertEqual([item["severity"] for item in checked], ["一般", "致命", "严重", "提示"])

    def test_report_groups_by_conclusion_then_severity(self) -> None:
        self._run([_REVIEW_FOUR, _REBUTTAL_FOUR])

        report = read_text(self.config.artifact_path("M01", "report"))
        self.assertIn("维持 2、待确认 1、已被反驳 1", report)  # 摘要与分块口径一致
        # 分块顺序：维持 → 待确认 → 已被反驳
        sustain = report.index("## 维持（2 条）")
        uncertain = report.index("## 待确认（1 条）")
        refuted = report.index("## 已被反驳（1 条）")
        self.assertLess(sustain, uncertain)
        self.assertLess(uncertain, refuted)
        # 块内按严重等级降序：严重的中问题排在该块内一般的轻问题之前
        self.assertLess(report.index("中问题"), report.index("轻问题"))
        # 分块优先于等级：致命的「重问题」被反驳了，只能待在最后一块
        self.assertGreater(report.index("重问题"), refuted)

    def test_bad_severity_ends_the_module(self) -> None:
        state, client = self._run([_REVIEW_BAD_SEVERITY])

        self.assertTrue(state.get("failed"))
        self.assertIn("严重等级", state.get("fail_reason", ""))
        self.assertEqual(len(client.prompts), 1)  # 没有进入反驳
        self.assertFalse(self.config.artifact_path("M01", "report").exists())

    def test_missing_rebuttal_is_sent_back(self) -> None:
        state, client = self._run([_REVIEW_TWO, _REBUTTAL_F1, _REBUTTAL_BOTH])

        self.assertEqual(len(client.prompts), 3)  # 检视 + 两轮反驳
        conclusions = {item["id"]: item["conclusion"] for item in state["rebuttals"]}
        self.assertEqual(conclusions, {"f1": "维持", "f2": "反驳"})
        self.assertFalse(state.get("failed"))

    def test_malformed_rebuttal_items_are_rejected_and_logged(self) -> None:
        # 反驳产物与检视产物受同一套格式约束：编号非法、结论不在闭集、理由为空都被拒收，
        # 但不能静默丢掉——日志留痕，条目本身交由补做兜回。
        with self.assertLogs("agent2.nodes.rebuttal_check_tool", level="WARNING") as captured:
            state, _ = self._run([_REVIEW_FOUR, _REBUTTAL_MALFORMED])

        logged = "\n".join(captured.output)
        self.assertIn("编号不在本轮待反驳范围内", logged)
        self.assertIn("结论不在闭集内", logged)
        self.assertIn("理由为空", logged)

        conclusions = {item["id"]: item["conclusion"] for item in state["rebuttals"]}
        self.assertEqual(conclusions["f1"], "维持")  # 合法的那条被收下
        # 被拒的三条不会凭空消失，最终转为待确认
        self.assertEqual(conclusions["f2"], "待确认")
        self.assertEqual(conclusions["f3"], "待确认")
        self.assertEqual(conclusions["f4"], "待确认")
        checked = read_json(self.config.artifact_path("M01", "checked_rebuttals"))
        self.assertEqual(len(checked), 4)

    def test_unanswered_becomes_uncertain_after_the_limit(self) -> None:
        # 反驳始终只回 f1，f2 要等补做达上限后被标为待确认
        state, _ = self._run([_REVIEW_TWO, _REBUTTAL_F1, _REBUTTAL_F1, _REBUTTAL_F1])

        self.assertFalse(state.get("failed"))
        conclusions = {item["id"]: item["conclusion"] for item in state["rebuttals"]}
        self.assertEqual(conclusions["f1"], "维持")
        self.assertEqual(conclusions["f2"], "待确认")
        report = read_text(self.config.artifact_path("M01", "report"))
        self.assertIn("f2", report)  # 待确认的条目照样进报告，不静默丢弃


if __name__ == "__main__":
    unittest.main()
