"""finding 身份、去重与一致性判定的确定性测试。

这些是 P3 的地基：finding 身份不成立时，报告里的卡片会互相矛盾、跨运行无法比对，
而这类错误**不会有任何报错**——只是安静地给出错误结论。故用实测数据里出现过的形状
（同一行上多个缺陷、行号 ±1 漂移、模型自赋 category 抢戏）逐个钉住。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent import findings_util as fu

from _support import finding


class AnchorTest(unittest.TestCase):
    def test_fingerprint_normalizes_whitespace(self):
        self.assertEqual(fu.line_fingerprint("  strcpy(a, b);  "), fu.line_fingerprint("strcpy(a, b);"))

    def test_fingerprint_of_blank_line_is_empty(self):
        self.assertEqual(fu.line_fingerprint("   "), "")

    def test_anchor_keeps_line_number(self):
        """纯内容哈希会让两行 `free(p);` 相撞——行号必须留在 id 里。"""
        left = fu.anchor_id("resource.c", 20, fu.line_fingerprint("free(p);"))
        right = fu.anchor_id("resource.c", 21, fu.line_fingerprint("free(p);"))
        self.assertNotEqual(left, right)
        self.assertTrue(left.startswith("resource.c:20#"))

    def test_anchor_without_fingerprint_degrades_to_file_line(self):
        self.assertEqual(fu.anchor_id("a.c", 3), "a.c:3")


class AnchorsMatchTest(unittest.TestCase):
    def test_same_fingerprint_matches_despite_line_drift(self):
        """实测同一缺陷在六次运行里报在 buffer.c:11 与 12，指纹相同即同一处语句。"""
        left = {"file": "buffer.c", "line": 11, "anchor_fingerprint": "abcd1234"}
        right = {"file": "buffer.c", "line": 12, "anchor_fingerprint": "abcd1234"}
        self.assertTrue(fu.anchors_match(left, right))

    def test_different_fingerprint_does_not_match(self):
        left = {"file": "a.c", "line": 3, "anchor_fingerprint": "aaaaaaaa"}
        right = {"file": "a.c", "line": 3, "anchor_fingerprint": "bbbbbbbb"}
        self.assertFalse(fu.anchors_match(left, right))

    def test_falls_back_to_line_tolerance_without_fingerprint(self):
        left = {"file": "a.c", "line": 3}
        right = {"file": "a.c", "line": 4}
        self.assertTrue(fu.anchors_match(left, right))
        self.assertFalse(fu.anchors_match(left, {"file": "a.c", "line": 9}))

    def test_different_file_never_matches(self):
        self.assertFalse(fu.anchors_match({"file": "a.c", "line": 3}, {"file": "b.c", "line": 3}))


class GroupByAnchorTest(unittest.TestCase):
    def test_exact_tolerance_keeps_adjacent_lines_separate(self):
        """容差刻意不放进运行内归并：11 连 12、12 连 13 会链式并组，把不同语句混为一谈。"""
        items = [finding(file="a.c", line=11), finding(file="a.c", line=12)]
        self.assertEqual(len(fu.group_by_anchor(items)), 2)

    def test_same_anchor_groups_and_sorts_by_position(self):
        items = [finding(file="b.c", line=2), finding(file="a.c", line=9), finding(file="a.c", line=1)]
        groups = fu.group_by_anchor(items)
        self.assertEqual([[items[i]["file"] for i in g] for g in groups], [["a.c"], ["a.c"], ["b.c"]])


class ClaimKeyTest(unittest.TestCase):
    def test_category_does_not_enter_the_key(self):
        """P3.2 的核心：同一缺陷换个模型自赋的 category 仍须判为同一主张。"""
        left = finding(file="buffer.c", line=7, category="memory", defect_kind="unbounded_write")
        right = finding(file="buffer.c", line=7, category="trust_boundary", defect_kind="unbounded_write")
        self.assertEqual(fu.claim_key(left), fu.claim_key(right))

    def test_defect_kind_does_enter_the_key(self):
        left = finding(file="parser.c", line=13, defect_kind="index_not_advanced")
        right = finding(file="parser.c", line=13, defect_kind="no_nul_termination")
        self.assertNotEqual(fu.claim_key(left), fu.claim_key(right))

    def test_unknown_kind_falls_back_to_family_sentinel(self):
        """回退到 `family:<族>` 而不是 other：不同族不得因缺字段而互相合并。"""
        self.assertEqual(fu.defect_kind(finding(defect_kind="", category="memory")), "family:memory")
        self.assertEqual(fu.defect_kind(finding(defect_kind="", category="ub")), "family:ub")

    def test_kind_synonyms_map_into_the_closed_set(self):
        self.assertEqual(fu.defect_kind(finding(defect_kind="buffer_overflow")), "unbounded_write")


class DedupTest(unittest.TestCase):
    def test_same_claim_merges_and_unions_evidence(self):
        left = finding(id="f1", file="a.c", line=3, severity="一般", evidence="证据甲")
        right = finding(id="f2", file="a.c", line=3, severity="危险级别不明", evidence="证据乙")
        merged = fu.dedup_claims([left, right])
        self.assertEqual(len(merged), 1)
        self.assertIn("证据甲", merged[0]["evidence"])
        self.assertIn("证据乙", merged[0]["evidence"])
        self.assertEqual(merged[0]["merged_from"], ["f1", "f2"])

    def test_different_kinds_on_one_line_are_not_merged(self):
        """实测 parser.c:13 上并存三个都成立的缺陷，合并是净损失。"""
        items = [
            finding(id="f1", file="parser.c", line=13, defect_kind="index_not_advanced"),
            finding(id="f2", file="parser.c", line=13, defect_kind="no_nul_termination"),
            finding(id="f3", file="parser.c", line=13, defect_kind="path_trust"),
        ]
        self.assertEqual(len(fu.dedup_claims(items)), 3)

    def test_merge_keeps_worst_severity_and_is_pure(self):
        left = finding(id="f1", file="a.c", line=3, severity="提示", root_cause="短")
        right = finding(id="f2", file="a.c", line=3, severity="致命", root_cause="更长的根因描述")
        merged = fu.dedup_claims([left, right])
        self.assertEqual(merged[0]["severity"], "致命")
        self.assertEqual(left["severity"], "提示", "入参不得被改写")
        self.assertNotIn("merged_from", left)


class SeverityTest(unittest.TestCase):
    def test_free_form_severity_is_normalized(self):
        self.assertEqual(fu.norm_severity("CRITICAL"), "致命")
        self.assertEqual(fu.norm_severity("high"), "严重")
        self.assertEqual(fu.norm_severity(""), "一般")
        self.assertEqual(fu.norm_severity("莫名字符串"), "一般")

    def test_worst_severity_picks_most_severe(self):
        self.assertEqual(fu.worst_severity(["提示", "严重", "一般"]), "严重")
        self.assertEqual(fu.worst_severity([]), "一般")

    def test_rank_orders_by_severity(self):
        self.assertLess(fu.severity_rank("致命"), fu.severity_rank("提示"))

    def test_rank_of_unrecognized_degrades_to_normal(self):
        """无法识别的等级落到「一般」而不是排到最后：`norm_severity` 已经收敛过，
        故 `severity_rank` 里那个 9 的兜底不可达。"""
        self.assertEqual(fu.severity_rank("未知"), fu.severity_rank("一般"))


class ResolveConclusionsTest(unittest.TestCase):
    def test_agreement_passes_through(self):
        self.assertEqual(fu.resolve_conclusions(["confirmed", "confirmed"]), ("confirmed", False))

    def test_confirmed_vs_refuted_becomes_contested_uncertain(self):
        conclusion, inconsistent = fu.resolve_conclusions(["confirmed", "refuted"])
        self.assertEqual(conclusion, "uncertain")
        self.assertTrue(inconsistent)

    def test_confirmed_beats_uncertain(self):
        self.assertEqual(fu.resolve_conclusions(["uncertain", "confirmed"]), ("confirmed", False))

    def test_refuted_beats_uncertain(self):
        self.assertEqual(fu.resolve_conclusions(["uncertain", "refuted"]), ("refuted", False))

    def test_no_known_conclusion_is_uncertain_and_not_contested(self):
        self.assertEqual(fu.resolve_conclusions(["", "???"]), ("uncertain", False))


class CategoryFamilyTest(unittest.TestCase):
    def test_synonyms_and_keywords_collapse(self):
        self.assertEqual(fu.category_family("buffer_overflow"), "memory")
        self.assertEqual(fu.category_family("信任面"), "trust_boundary")
        self.assertEqual(fu.category_family("thread"), "concurrency")
        self.assertEqual(fu.category_family("完全无法识别"), "logic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
