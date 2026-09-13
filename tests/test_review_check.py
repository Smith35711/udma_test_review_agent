"""确定性闸门：降级、工具证据校正、主张冲突归并、漏结论兜底。

本节点是 `findings_verified.json` 的唯一写出方，模型给出的结论在此被统一复核。它是
**精确率的最后一道关**：闸门漏掉一条不可核验的 confirmed，报告就会把它算进精确率。
故这里逐条钉住每一种降级条件，并特别钉住**不该降级**的方向——工具没报不等于证伪，
过度降级会把真实缺陷洗成 uncertain，那是另一种失真。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent.nodes.tool import review_check as rc
from udma_test_review_agent.schemas import RawFindings

from _support import TempProject, arch_payload, env, finding, verdict, write_models_yaml

_SOURCE = {
    "a.c": """
        #include <stdlib.h>
        #include <string.h>
        void copy(char *src) {
            char dst[8];
            strcpy(dst, src);
        }
        void use(void) {
            char *p = malloc(4);
            free(p);
            free(p);
        }
        """,
}


class _GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TempProject(_SOURCE)
        self.addCleanup(self.project.cleanup)
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-gate-")
        self.addCleanup(self._tmp.cleanup)
        with env(MODEL_CONFIG=str(write_models_yaml(Path(self._tmp.name))), TEST_API_KEY="k"):
            self.cfg = self.project.config(output_root=str(Path(self._tmp.name) / "out"))
        self.scan = self.project.scan()
        (self.project.root / "a.c").read_text(encoding="utf-8")

    def write_raw(self, module: str, findings: list[dict], observations: list[dict] | None = None):
        from udma_test_review_agent.nodes.common import _findings_raw_path, _write_json

        payload = RawFindings.model_validate(
            {"findings": findings, "observations": observations or []}
        ).model_dump()
        _write_json(_findings_raw_path(self.cfg, module), payload)
        return payload

    def run_gate(self, module: str, results: list[dict], raw_payload: dict) -> dict:
        state = {
            "config": self.cfg,
            "module_results": {module: {"results": results}},
            "module_order": [module],
            "module_raw": {},
        }
        out = rc._review_check_one(self.cfg, state, module)
        return out["module_results"][module]


class DemotionTest(_GateTest):
    def test_low_confidence_confirmed_is_demoted(self):
        """实测两条误报的置信度是 0.50/0.55，而真正的致命缺陷是 0.95。
        模型自己的置信度此前没有任何消费者，于是误报照样计入精确率。"""
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01", [{"finding": item, "verdict": verdict(id="f1", confidence=0.55)}], raw
        )
        result = payload["results"][0]["verdict"]
        self.assertEqual(result["conclusion"], "uncertain")
        self.assertIn("置信度", result["demoted_reason"])

    def test_confidence_at_threshold_is_kept(self):
        """边界取 `>=`：等于阈值算达标，否则阈值语义会整体偏移一档。"""
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01",
            [{"finding": item, "verdict": verdict(id="f1", confidence=self.cfg.confirmed_confidence_threshold)}],
            raw,
        )
        self.assertEqual(payload["results"][0]["verdict"]["conclusion"], "confirmed")

    def test_contract_dependent_confirmed_is_demoted(self):
        """依赖本模块外不可见契约的条目，反驳阶段无从独立核验，不得计入精确率。"""
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01",
            [{"finding": item, "verdict": verdict(id="f1", confidence=0.99, contract_dependent=True)}],
            raw,
        )
        result = payload["results"][0]["verdict"]
        self.assertEqual(result["conclusion"], "uncertain")
        self.assertIn("契约", result["demoted_reason"])

    def test_refuted_is_never_touched_by_demotion(self):
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01",
            [{"finding": item, "verdict": verdict(id="f1", conclusion="refuted", confidence=0.1)}],
            raw,
        )
        self.assertEqual(payload["results"][0]["verdict"]["conclusion"], "refuted")

    def test_unparseable_confidence_degrades_to_zero(self):
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01",
            [{"finding": item, "verdict": verdict(id="f1", confidence="高")}],
            raw,
        )
        result = payload["results"][0]["verdict"]
        self.assertEqual(result["conclusion"], "uncertain")


class MissingVerdictTest(_GateTest):
    def test_finding_without_verdict_falls_back_to_uncertain(self):
        """整批反驳调用失败时全部条目会落到这里：confirmed=0/uncertain=N 看起来「健康」，
        必须留痕，否则失败被伪装成正常结果。"""
        items = [finding(id="f1"), finding(id="f2", line=8)]
        raw = self.write_raw("M01", items)
        payload = self.run_gate("M01", [], raw)
        self.assertEqual(len(payload["results"]), 2)
        for result in payload["results"]:
            self.assertEqual(result["verdict"]["conclusion"], "uncertain")
            self.assertEqual(result["verdict"]["verdict_source"], "fallback")
            self.assertEqual(result["verdict"]["confidence"], 0.0)

    def test_finding_absent_from_raw_is_dropped(self):
        """反驳返回了一条本轮检视里不存在的条目：以检视产物为准，多余结论不得混入。"""
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01",
            [
                {"finding": item, "verdict": verdict(id="f1")},
                {"finding": finding(id="f9"), "verdict": verdict(id="f9")},
            ],
            raw,
        )
        self.assertEqual([r["finding"]["id"] for r in payload["results"]], ["f1"])

    def test_invalid_conclusion_string_is_treated_as_missing(self):
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01", [{"finding": item, "verdict": verdict(id="f1", conclusion="maybe")}], raw
        )
        self.assertEqual(payload["results"][0]["verdict"]["conclusion"], "uncertain")
        self.assertEqual(payload["results"][0]["verdict"]["verdict_source"], "fallback")


class ClaimConflictTest(_GateTest):
    def test_confirmed_vs_refuted_on_one_claim_becomes_uncertain(self):
        """同一主张（锚点 + 缺陷种类）上两个方向的强主张冲突：静默择一会同时带来
        假阳性与漏报两种风险，故一律降级并保留双方原文交人工裁决。"""
        left = finding(id="f1", line=3)
        right = finding(id="f2", line=3)
        raw = self.write_raw("M01", [left, right])
        payload = self.run_gate(
            "M01",
            [
                {"finding": left, "verdict": verdict(id="f1", conclusion="confirmed")},
                {"finding": right, "verdict": verdict(id="f2", conclusion="refuted")},
            ],
            raw,
        )
        for result in payload["results"]:
            self.assertEqual(result["verdict"]["conclusion"], "uncertain")
            self.assertTrue(result["verdict"]["inconsistent"])
            self.assertEqual(len(result["verdict"]["sibling_verdicts"]), 1)

    def test_duplicate_ids_collapse_to_one_result(self):
        """`existing` 按 id 建索引：反驳产物里同 id 出现两次时后者覆盖前者，
        结果条数由**检视产物**决定，不会凭空多出一条。"""
        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01",
            [
                {"finding": item, "verdict": verdict(id="f1", conclusion="confirmed")},
                {"finding": item, "verdict": verdict(id="f1", conclusion="refuted")},
            ],
            raw,
        )
        self.assertEqual(len(payload["results"]), 1)

    def test_same_anchor_different_kind_is_not_a_conflict(self):
        left = finding(id="f1", line=3, defect_kind="unbounded_write")
        right = finding(id="f2", line=3, defect_kind="missing_error_check")
        raw = self.write_raw("M01", [left, right])
        payload = self.run_gate(
            "M01",
            [
                {"finding": left, "verdict": verdict(id="f1", conclusion="confirmed")},
                {"finding": right, "verdict": verdict(id="f2", conclusion="refuted")},
            ],
            raw,
        )
        self.assertFalse(any(r["verdict"].get("inconsistent") for r in payload["results"]))

    def test_different_defect_kinds_on_one_line_are_not_a_conflict(self):
        """实测 parser.c:13 上并存三个都成立的缺陷、结论各不相同。
        按行号判矛盾会把三个真实缺陷一起洗成 uncertain——那是净损失。"""
        left = finding(id="f1", defect_kind="index_not_advanced")
        right = finding(id="f2", defect_kind="no_nul_termination")
        raw = self.write_raw("M01", [left, right])
        payload = self.run_gate(
            "M01",
            [
                {"finding": left, "verdict": verdict(id="f1", conclusion="confirmed")},
                {"finding": right, "verdict": verdict(id="f2", conclusion="refuted")},
            ],
            raw,
        )
        conclusions = {r["finding"]["id"]: r["verdict"]["conclusion"] for r in payload["results"]}
        self.assertEqual(conclusions, {"f1": "confirmed", "f2": "refuted"})


class ToolEvidenceTest(_GateTest):
    def _with_tool_evidence(self, entries: list[dict], item: dict, verdict_overrides: dict):
        from udma_test_review_agent.nodes.common import _tool_evidence_path, _write_json

        _write_json(
            _tool_evidence_path(self.cfg),
            {"tool_available": True, "entries": entries, "notes": []},
        )
        raw = self.write_raw("M01", [item])
        payload = self.run_gate(
            "M01", [{"finding": item, "verdict": verdict(id=item["id"], **verdict_overrides)}], raw
        )
        return payload["results"][0]

    def test_same_kind_refutation_is_escalated_to_confirmed(self):
        """工具独立复现了模型的这条具体判断，且工具不会因为「拿不准」而报错。"""
        item = finding(id="f1", file="a.c", line=8, category="memory", defect_kind="double_free")
        result = self._with_tool_evidence(
            [
                {
                    "tool": "gcc-analyzer",
                    "file": "a.c",
                    "line": 8,
                    "rule": "double-free",
                    "severity": "致命",
                    "message": "double free of 'p'",
                }
            ],
            item,
            {"conclusion": "refuted", "confidence": 0.4},
        )
        self.assertEqual(result["verdict"]["conclusion"], "confirmed")
        self.assertEqual(result["verdict"]["verdict_source"], "tool")
        self.assertGreaterEqual(result["verdict"]["confidence"], 0.9)
        self.assertTrue(result["finding"]["tool_evidence"])

    def test_same_kind_confirmation_survives_low_confidence(self):
        """工具已证实，模型自评的置信度不再重要——否则闸门会把已证实的条目降级。"""
        item = finding(id="f1", file="a.c", line=8, category="memory", defect_kind="double_free")
        result = self._with_tool_evidence(
            [
                {
                    "tool": "cppcheck",
                    "file": "a.c",
                    "line": 8,
                    "rule": "doubleFree",
                    "severity": "严重",
                    "message": "Memory pointed to by 'p' is freed twice.",
                }
            ],
            item,
            {"conclusion": "confirmed", "confidence": 0.2},
        )
        self.assertEqual(result["verdict"]["conclusion"], "confirmed")

    def test_same_place_only_blocks_refutation_but_does_not_confirm(self):
        """工具在同一行报的是**另一件事**：该语句确实有问题，但工具的话不能替模型的
        这条断言背书。故只阻止「被证伪」，不升为 confirmed。"""
        item = finding(id="f1", file="a.c", line=3, category="trust_boundary", defect_kind="path_trust")
        result = self._with_tool_evidence(
            [
                {
                    "tool": "cppcheck",
                    "file": "a.c",
                    "line": 3,
                    "rule": "bufferAccessOutOfBounds",
                    "severity": "严重",
                    "message": "Buffer is accessed out of bounds.",
                }
            ],
            item,
            {"conclusion": "refuted", "confidence": 0.8},
        )
        self.assertEqual(result["verdict"]["conclusion"], "uncertain")
        self.assertTrue(result["verdict"]["inconsistent"])
        self.assertEqual(result["verdict"]["verdict_source"], "tool")

    def test_absence_of_tool_evidence_is_not_refutation(self):
        """两条最重要的实测缺陷（buffer.c:7 无界 strcpy）两个工具都抓不到。
        用缺席当反证会直接造成漏报。"""
        item = finding(id="f1", file="a.c", line=3, category="memory", defect_kind="unbounded_write")
        result = self._with_tool_evidence([], item, {"conclusion": "confirmed", "confidence": 0.95})
        self.assertEqual(result["verdict"]["conclusion"], "confirmed")

    def test_tool_evidence_two_lines_away_does_not_match(self):
        item = finding(id="f1", file="a.c", line=3, category="memory", defect_kind="double_free")
        result = self._with_tool_evidence(
            [
                {
                    "tool": "gcc-analyzer",
                    "file": "a.c",
                    "line": 9,
                    "rule": "double-free",
                    "severity": "致命",
                    "message": "double free of 'p'",
                }
            ],
            item,
            {"conclusion": "refuted", "confidence": 0.8},
        )
        self.assertEqual(result["verdict"]["conclusion"], "refuted")


class PayloadTest(_GateTest):
    def test_observations_and_gaps_are_carried_into_the_artifact(self):
        """checklist_gaps 与 funnel 是「上游已知线索在下游丢失」的记账，只停在日志里
        等于人工永远看不到。"""
        from udma_test_review_agent.nodes.common import _findings_raw_path, _write_json

        item = finding(id="f1")
        payload = RawFindings.model_validate(
            {
                "findings": [item],
                "observations": [finding(id="o1", channel="observation")],
                "checklist_gaps": ["a.c:3 [memory] 核对边界"],
            }
        ).model_dump()
        payload["funnel"] = {"model_findings": 3, "kept_findings": 1}
        _write_json(_findings_raw_path(self.cfg, "M01"), payload)

        out = self.run_gate("M01", [{"finding": item, "verdict": verdict(id="f1")}], payload)
        self.assertEqual(out["checklist_gaps"], ["a.c:3 [memory] 核对边界"])
        self.assertEqual(out["funnel"]["model_findings"], 3)
        self.assertEqual(len(out["observations"]), 1)

    def test_results_are_sorted_by_severity_then_position(self):
        items = [
            finding(id="f1", file="a.c", line=8, severity="提示"),
            finding(id="f2", file="a.c", line=3, severity="致命"),
        ]
        raw = self.write_raw("M01", items)
        payload = self.run_gate(
            "M01",
            [
                {"finding": items[0], "verdict": verdict(id="f1", severity="提示")},
                {"finding": items[1], "verdict": verdict(id="f2", severity="致命")},
            ],
            raw,
        )
        self.assertEqual([r["finding"]["id"] for r in payload["results"]], ["f2", "f1"])

    def test_written_artifact_matches_the_returned_payload(self):
        from udma_test_review_agent.nodes.common import _findings_verified_path, _load_json

        item = finding(id="f1")
        raw = self.write_raw("M01", [item])
        payload = self.run_gate("M01", [{"finding": item, "verdict": verdict(id="f1")}], raw)
        self.assertEqual(_load_json(_findings_verified_path(self.cfg, "M01")), payload)


class ModuleFailureTest(_GateTest):
    def test_missing_raw_artifact_raises_for_isolation(self):
        """缺产物要抛出可诊断的错误交失败隔离跳过该模块，而不是让裸的 KeyError
        冒到批次层、把整轮拖垮。"""
        state = {"config": self.cfg, "module_results": {"M01": {"results": []}}, "module_order": ["M01"]}
        with self.assertRaises(FileNotFoundError):
            rc._review_check_one(self.cfg, state, "M01")


class ArchFixtureSanityTest(unittest.TestCase):
    """`arch_payload` 夹具本身必须能被 schema 接受，否则各测试的失败原因会被它掩盖。"""

    def test_arch_payload_validates(self):
        from udma_test_review_agent.schemas import ModuleArchitecture

        ModuleArchitecture.model_validate(arch_payload())


if __name__ == "__main__":
    unittest.main(verbosity=2)
