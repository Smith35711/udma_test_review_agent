"""检视节点的确定性部分：分批、锚点填充、位置校验、去重、编号、漏斗与漏检记账。

LLM 是桩件——这里要钉住的恰恰是**模型之外**的那一层。实测最刺眼的两个症状都出在
这一层而非模型：同一处 strcpy 缺陷在三张卡上给出三种结论（去重键含自赋 category），
以及 observations 的 `o1..o4` 每个都在两个批次里各出现一次（编号从没重来过）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent.nodes.common import (
    _module_arch_path,
    _tool_evidence_path,
    _write_json,
)
from udma_test_review_agent.nodes.llm import review as rv
from udma_test_review_agent.schemas import GlobalArchitecture, ReviewCheck

from _support import (
    TempProject,
    arch_payload,
    checklist_item,
    env,
    finding,
    llm_by_schema,
    raw_findings,
    write_models_yaml,
)

_SOURCE = {
    "a.c": """
        #include <stdlib.h>
        #include <string.h>

        void copy(char *src) {
            char dst[8];
            strcpy(dst, src);
        }

        void leak(void) {
            char *p = malloc(4);
        }
        """,
    # 同名 .h 会被自动并入 M01——`unattributable` 与 large 路径的 header 签名都靠它。
    "a.h": "void copy(char *src);\nint parse(const char *text);\n",
    "b.c": """
        #include <string.h>

        int parse(const char *text) {
            char name[8];
            memcpy(name, text, 8);
            return 0;
        }
        """,
}
"""`a.c:7` 的 strcpy 与 `b.c:5` 的 memcpy 是两处真实缺陷的替身，行号被下面的常量引用。"""

_STRCPY_LINE = 6
"""`a.c` 里 `strcpy(dst, src);` 的行号（`_SOURCE` 去掉首行空行后从 1 数起）。"""

_MEMCPY_LINE = 5
"""`b.c` 里 `memcpy(name, text, 8);` 的行号。"""


class _ReviewHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TempProject(_SOURCE)
        self.addCleanup(self.project.cleanup)
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-review-")
        self.addCleanup(self._tmp.cleanup)
        with env(MODEL_CONFIG=str(write_models_yaml(Path(self._tmp.name))), TEST_API_KEY="k"):
            self.cfg = self.project.config(output_root=str(Path(self._tmp.name) / "out"))
        self.scan = self.project.scan()

    def write_arch(self, module: str = "M01", **overrides):
        payload = arch_payload(module, **overrides)
        _write_json(_module_arch_path(self.cfg, module), payload)
        return payload

    def run_review(
        self,
        llm: object,
        *,
        module: str = "M01",
        size_class: str = "small",
        global_arch: GlobalArchitecture | None = None,
        state_paths: list[dict] | None = None,
    ) -> dict:
        original = rv._llm
        rv._llm = llm  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(rv, "_llm", original))
        return rv._review_module_raw(
            self.scan,
            global_arch or GlobalArchitecture(layers=[], modules=[], trust_entries=[], concurrency_hotspots=[]),
            module,
            size_class,
            self.cfg,
            state_paths or [],
        )

    def small_llm(self, *batches, passed: bool = True):
        """小模块路径的桩件：检视返回 `batches`（逐次取用），内容审查直接判通过。"""
        return llm_by_schema(
            {
                rv.RawFindings: list(batches),
                ReviewCheck: ReviewCheck(passed=passed, issues=[]),
            }
        )


class SmallPathTest(_ReviewHarness):
    def test_findings_and_observations_are_renumbered_from_one(self):
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [finding(id="模型自赋的怪 id", line=_STRCPY_LINE)],
                    observations=[finding(id="o1", line=_MEMCPY_LINE, channel="observation")],
                )
            )
        )
        self.assertEqual([f["id"] for f in out["findings"]], ["f1"])
        self.assertEqual([o["id"] for o in out["observations"]], ["o1"])

    def test_channel_observation_findings_move_to_observations(self):
        """模型把条目放进 `findings` 数组却标了 `channel=observation` 时，归位到观察项——
        观察项不经反驳流程核验，混进 findings 会让精确率虚高。"""
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [
                        finding(id="f1", file="a.c", line=_STRCPY_LINE),
                        finding(id="f2", file="b.c", line=_MEMCPY_LINE, channel="observation"),
                    ]
                )
            )
        )
        self.assertEqual([f["file"] for f in out["findings"]], ["a.c"])
        self.assertEqual([o["file"] for o in out["observations"]], ["b.c"])

    def test_anchor_fingerprint_comes_from_the_source_line(self):
        """锚点身份必须由源码算出，与模型措辞无关；源码一改锚点即变。"""
        self.write_arch()
        out = self.run_review(self.small_llm(raw_findings([finding(line=_STRCPY_LINE)])))
        result = out["findings"][0]
        self.assertEqual(result["evidence_status"], "verified")
        self.assertTrue(result["anchor_fingerprint"])
        self.assertEqual(result["anchor"], f"a.c:{_STRCPY_LINE}#{result['anchor_fingerprint']}")

    def test_invalid_location_is_demoted_to_observation_with_reason(self):
        self.write_arch()
        out = self.run_review(self.small_llm(raw_findings([finding(line=9999)])))
        self.assertEqual(out["findings"], [])
        self.assertEqual(len(out["observations"]), 1)
        self.assertIn("位置校验未通过", out["observations"][0]["description"])
        self.assertEqual(out["funnel"]["location_invalid"], 1)

    def test_unattributable_location_is_kept_as_finding(self):
        """头文件声明、宏、多行签名都会落进 `unattributable`——那是扫描器的能力边界而非
        模型错误。按「无效」处理会把整类真实缺陷（头文件契约缺陷）系统性丢掉。"""
        self.write_arch()
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.h", line=1)])))
        self.assertEqual(len(out["findings"]), 1)
        self.assertEqual(out["findings"][0]["evidence_status"], "unattributable")

    def test_model_path_variants_are_normalized(self):
        self.write_arch()
        out = self.run_review(self.small_llm(raw_findings([finding(file="./a.c", line=_STRCPY_LINE)])))
        self.assertEqual(out["findings"][0]["file"], "a.c")

    def test_content_review_failure_retries_then_uses_the_last_version(self):
        """重试到上限仍不通过时采用最后一版并告警，**不是**抛错丢掉整个模块。"""
        self.write_arch()
        llm = llm_by_schema(
            {
                rv.RawFindings: [
                    raw_findings([finding(id="x", file="a.c", line=_STRCPY_LINE)]),
                    raw_findings([finding(id="y", file="b.c", line=_MEMCPY_LINE)]),
                ],
                ReviewCheck: ReviewCheck(passed=False, issues=["格式不对"], feedback="重来"),
            }
        )
        out = self.run_review(llm)
        self.assertEqual(len(out["findings"]), 1)
        self.assertEqual(out["findings"][0]["file"], "b.c")  # 最后一版
        self.assertEqual(llm.counters[rv.RawFindings], self.cfg.max_review_attempts)

    def test_content_review_pass_stops_retrying(self):
        self.write_arch()
        llm = llm_by_schema(
            {
                rv.RawFindings: raw_findings([finding(line=_STRCPY_LINE)]),
                ReviewCheck: [ReviewCheck(passed=False, issues=["x"], feedback="y"), ReviewCheck(passed=True)],
            }
        )
        self.run_review(llm)
        self.assertEqual(llm.counters[rv.RawFindings], 2)


class LargePathTest(_ReviewHarness):
    def test_headers_are_passed_to_the_domain_prompt(self):
        """`header_signatures_text` 此前从未被执行过（样例模块全是 small），
        它一旦抛错整条 large 路径就不可用。"""
        self.write_arch()
        seen: list[str] = []

        def _spy(prompt, schema, *args, **kwargs):
            seen.append(prompt)
            return raw_findings() if schema is rv.RawFindings else ReviewCheck(passed=True)

        self.run_review(_spy, size_class="large")
        self.assertEqual(len(seen), 1)
        self.assertIn("a.h", seen[0])
        self.assertIn("copy", seen[0])

    def test_ids_are_unique_across_batches(self):
        """原实现里每个批次都被要求「全局唯一」，跨批次不可能做到——
        实测三个模块全部出现 id 冲突（`o1..o4` 各出现两次）。
        两个批次由「按域检视 + 高风险专项检视」构成。"""
        self.write_arch()
        llm = llm_by_schema(
            {
                rv.RawFindings: [
                    raw_findings([finding(id="o1", file="a.c", line=_STRCPY_LINE)]),
                    raw_findings([finding(id="o1", file="b.c", line=_MEMCPY_LINE)]),
                ],
                ReviewCheck: ReviewCheck(passed=True),
            }
        )
        out = self.run_review(llm, size_class="large", global_arch=self._located_trust())
        self.assertEqual(llm.counters[rv.RawFindings], 2)
        self.assertEqual(sorted(f["id"] for f in out["findings"]), ["f1", "f2"])

    def test_batches_are_deduplicated_by_claim_not_by_line(self):
        """同一锚点 + 同一缺陷种类才是同一主张；同锚点的不同种类必须各自保留。"""
        self.write_arch()
        llm = llm_by_schema(
            {
                rv.RawFindings: [
                    raw_findings(
                        [
                            finding(id="a", file="a.c", line=_STRCPY_LINE, defect_kind="unbounded_write"),
                            finding(id="b", file="a.c", line=_STRCPY_LINE, defect_kind="path_trust"),
                        ]
                    ),
                    raw_findings(
                        [finding(id="c", file="a.c", line=_STRCPY_LINE, defect_kind="unbounded_write")]
                    ),
                ],
                ReviewCheck: ReviewCheck(passed=True),
            }
        )
        out = self.run_review(llm, size_class="large", global_arch=self._located_trust())
        kinds = sorted(f["defect_kind"] for f in out["findings"])
        self.assertEqual(kinds, ["path_trust", "unbounded_write"])
        self.assertEqual(out["funnel"]["after_dedup"], 2)

    @staticmethod
    def _located_trust() -> GlobalArchitecture:
        return GlobalArchitecture(
            layers=[],
            modules=[],
            trust_entries=[
                {
                    "file": "a.c",
                    "line": _STRCPY_LINE,
                    "kind": "parse",
                    "description": "外部输入直接进入拷贝",
                    "evidence": f"a.c:{_STRCPY_LINE} strcpy(dst, src);",
                }
            ],
            concurrency_hotspots=[],
        )


class CapTest(_ReviewHarness):
    def test_cap_keeps_the_most_severe(self):
        """原实现按批次插入顺序截断：大模块里第三批次的致命缺陷会被第一批次的提示挤掉，
        且被丢的是哪条只留在调试日志里。"""
        with env(MODEL_CONFIG=str(write_models_yaml(Path(self._tmp.name))), TEST_API_KEY="k"):
            self.cfg = self.project.config(
                output_root=str(Path(self._tmp.name) / "out"), max_findings_per_pass=1
            )
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [
                        finding(id="a", file="a.c", line=_STRCPY_LINE, severity="提示"),
                        finding(id="b", file="b.c", line=_MEMCPY_LINE, severity="致命"),
                    ]
                )
            )
        )
        self.assertEqual(len(out["findings"]), 1)
        self.assertEqual(out["findings"][0]["severity"], "致命")
        self.assertEqual(out["funnel"]["capped"], 1)


class ObservationHygieneTest(_ReviewHarness):
    def test_observation_duplicating_a_finding_is_dropped(self):
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [finding(id="f", file="a.c", line=_STRCPY_LINE)],
                    observations=[finding(id="o", file="a.c", line=_STRCPY_LINE, channel="observation")],
                )
            )
        )
        self.assertEqual(out["observations"], [])
        self.assertEqual(out["funnel"]["obs_redundant"], 1)

    def test_duplicate_observations_collapse(self):
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [],
                    observations=[
                        finding(id="o1", file="b.c", line=_MEMCPY_LINE, channel="observation"),
                        finding(id="o2", file="b.c", line=_MEMCPY_LINE, channel="observation"),
                    ],
                )
            )
        )
        self.assertEqual(len(out["observations"]), 1)
        self.assertEqual(out["observations"][0]["id"], "o1")

    def test_observations_are_sorted_by_severity(self):
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [],
                    observations=[
                        finding(id="a", file="a.c", line=_STRCPY_LINE, severity="提示", channel="observation"),
                        finding(id="b", file="b.c", line=_MEMCPY_LINE, severity="致命", channel="observation"),
                    ],
                )
            )
        )
        self.assertEqual([o["severity"] for o in out["observations"]], ["致命", "提示"])


class ChecklistGapTest(_ReviewHarness):
    def test_located_item_without_response_is_recorded(self):
        """架构阶段已把检查项定位到 file:line，检视阶段整条无回应即信息在链路上丢失——
        这是最像漏报的一类，必须进产物而不是只进日志。"""
        self.write_arch(checklist=[checklist_item(file="b.c", line=_MEMCPY_LINE)])
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)])))
        self.assertEqual(len(out["checklist_gaps"]), 1)
        self.assertIn("b.c:5", out["checklist_gaps"][0])

    def test_nearby_response_closes_the_gap(self):
        """模型的 finding 行号常与检查项差一两行（指向语句开头而非关键字），
        比对时留容差，否则记账虚高、失去督促作用。"""
        self.write_arch(checklist=[checklist_item(file="a.c", line=_STRCPY_LINE + 1)])
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)])))
        self.assertEqual(out["checklist_gaps"], [])

    def test_unlocated_item_is_not_tracked(self):
        self.write_arch(checklist=[checklist_item(file="", line=0)])
        out = self.run_review(self.small_llm(raw_findings([])))
        self.assertEqual(out["checklist_gaps"], [])

    def test_header_declaration_of_a_covered_function_closes_the_gap(self):
        """检查项锚在头文件的**函数声明行**、结论落在该函数的实现里，算已回应。

        实测 M01 的 3 条 buffer.h 检查项全是这种形态（上游锚在 `buffer.h:6` 的
        `void copy_name(...)`，检视在 `buffer.c:7` 的函数体里出结论），按位置比对
        一律误判成「无回应」，于是「优先人工复核」那一节整节都是假警报。
        """
        self.write_arch(checklist=[checklist_item(item="核对 copy 的边界", file="a.h", line=1)])
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)])))
        self.assertEqual(out["checklist_gaps"], [])

    def test_header_item_naming_no_covered_function_still_gaps(self):
        """声明行里没有本轮出过结论的函数名时照旧记账——销账只认声明行，不认正文提及。"""
        self.write_arch(checklist=[checklist_item(item="核对 parse 的边界", file="a.h", line=2)])
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)])))
        self.assertEqual(len(out["checklist_gaps"]), 1)
        self.assertIn("a.h:2", out["checklist_gaps"][0])


class ChecklistRepairTest(_ReviewHarness):
    """已定位检查项的**闭环补检**：首轮无同关切族结论时追加定向检视。

    这不是收尾记账，而是流程动作：上游架构已经把线索定位到 file:line，若首轮开放式
    检视没回应，就带着这些线索定向再问一次。判据按**关切族**而非仅位置——同一行上可以
    并存多个都成立的缺陷，按位置销账会把它们一并抹掉。
    """

    def test_same_line_different_concern_triggers_a_repair_pass(self):
        """同一行上首轮报了 logic，上游的 trust_boundary 检查项仍算未回应。"""
        self.write_arch(
            checklist=[
                checklist_item(category="trust_boundary", item="校验外部长度", file="a.c", line=_STRCPY_LINE)
            ]
        )
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [finding(file="a.c", line=_STRCPY_LINE, category="logic", defect_kind="index_not_advanced")]
                ),
                raw_findings(
                    [finding(file="a.c", line=_STRCPY_LINE, category="trust_boundary", defect_kind="path_trust")]
                ),
            )
        )
        self.assertEqual(out["funnel"]["checklist_repair"], 1)
        self.assertIn("path_trust", {f["defect_kind"] for f in out["findings"]})

    def test_same_concern_does_not_trigger_repair(self):
        """首轮已在同位置给出同关切族的结论，就不必再花一次调用。"""
        self.write_arch(
            checklist=[checklist_item(category="memory", item="核对越界", file="a.c", line=_STRCPY_LINE)]
        )
        out = self.run_review(
            self.small_llm(
                raw_findings([finding(file="a.c", line=_STRCPY_LINE, category="memory", defect_kind="unbounded_write")])
            )
        )
        self.assertEqual(out["funnel"]["checklist_repair"], 0)

    def test_repair_pass_closes_the_gap_when_it_answers(self):
        """补检给出同关切族结论后，最终记账不再把该项列为漏检。"""
        self.write_arch(
            checklist=[
                checklist_item(category="trust_boundary", item="校验外部长度", file="a.c", line=_STRCPY_LINE)
            ]
        )
        out = self.run_review(
            self.small_llm(
                raw_findings([]),
                raw_findings(
                    [finding(file="a.c", line=_STRCPY_LINE, category="trust_boundary", defect_kind="path_trust")]
                ),
            )
        )
        self.assertEqual(out["funnel"]["checklist_repair"], 1)
        self.assertEqual(out["checklist_gaps"], [])


class ToolInjectionTest(_ReviewHarness):
    def _tool_report(self, entries: list[dict]) -> None:
        _write_json(
            _tool_evidence_path(self.cfg),
            {"tool_available": True, "entries": entries, "notes": []},
        )

    _UNCOVERED_LINE = 2
    """`a.c` 的 `#include <string.h>`：距模型报的 `_STRCPY_LINE` 足够远，
    且落在函数之外（`unattributable` 而非 `invalid`），是「工具独立报了一处」的理想替身。"""

    def _entry(self, rule: str = "-Wanalyzer-out-of-bounds", line: int | None = None) -> dict:
        return {
            "tool": "gcc-analyzer",
            "file": "a.c",
            "line": line if line is not None else self._UNCOVERED_LINE,
            "rule": rule,
            "severity": "严重",
            "message": "writing 8 bytes into a region of size 8",
        }

    def test_uncovered_tool_hit_becomes_a_candidate_finding(self):
        """工具报了、模型一字未提 → 补为候选。这是纯增益的召回提升。"""
        self.write_arch()
        self._tool_report([self._entry()])
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)])))
        sources = {f["source"] for f in out["findings"]}
        self.assertIn("tool", sources)
        self.assertEqual(out["funnel"]["tool_added"], 1)
        injected = next(f for f in out["findings"] if f["source"] == "tool")
        self.assertEqual(injected["defect_kind"], "unbounded_write")
        self.assertEqual(injected["severity"], "严重")
        self.assertEqual(injected["source_pass"], "tool")

    def test_location_the_model_already_covered_is_not_injected(self):
        """模型已提到该语句时，工具的作用是印证或裁决（在 review_check），
        再注入一条只会让同一条语句重复出卡。"""
        self.write_arch()
        self._tool_report([self._entry(line=_STRCPY_LINE)])
        out = self.run_review(self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)])))
        self.assertEqual(out["funnel"]["tool_added"], 0)
        self.assertEqual(len(out["findings"]), 1)

    def test_unmappable_rule_is_not_injected(self):
        """归不了闭集词表的规则只作印证之用：finding 的类别与种类是聚类的键，
        编造一个只会污染整套判定。"""
        self.write_arch()
        self._tool_report([self._entry(rule="someUnknownStyleRule")])
        out = self.run_review(self.small_llm(raw_findings([])))
        self.assertEqual(out["funnel"]["tool_added"], 0)

    def test_tool_hit_in_another_module_is_not_injected(self):
        self.write_arch()
        self._tool_report([{**self._entry(), "file": "b.c", "line": _MEMCPY_LINE}])
        out = self.run_review(self.small_llm(raw_findings([])), module="M01")
        self.assertEqual(out["funnel"]["tool_added"], 0)

    _LEAK_SIG_LINE = 9
    """`a.c` 里 `leak` 的函数签名行——在函数内，但距 `_LEAK_END_LINE` 两行以上，
    故不会命中「同位置」那条判定，能把「同函数同种类」这条单独试出来。"""
    _LEAK_END_LINE = 11
    """`leak` 的收尾 `}`。"""

    def test_same_claim_in_the_same_function_is_not_injected(self):
        """同一函数内的同种缺陷算同一主张，不因报在不同行而补第二张卡。

        实测 `resource.c` 的 fd 泄漏被报成三张已确认卡：模型报在 `fopen`（:7），
        gcc 对两条 return 各报一条（:10、:15）。只看行号，这三条互不相识，
        一次泄漏在报告里出现三次。
        """
        self.write_arch()
        self._tool_report([self._entry(rule="-Wanalyzer-file-leak", line=self._LEAK_END_LINE)])
        out = self.run_review(
            self.small_llm(
                raw_findings([finding(file="a.c", line=self._LEAK_SIG_LINE, defect_kind="missing_release")])
            )
        )
        self.assertEqual(out["funnel"]["tool_added"], 0)
        self.assertEqual(out["funnel"]["tool_redundant"], 1)
        self.assertEqual(len(out["findings"]), 1)

    def test_same_kind_in_another_function_is_still_injected(self):
        """跨函数的同种缺陷不受影响：不同函数里的两处泄漏本来就是两件事。"""
        self.write_arch()
        self._tool_report([self._entry(rule="-Wanalyzer-file-leak", line=_STRCPY_LINE)])
        out = self.run_review(
            self.small_llm(
                raw_findings([finding(file="a.c", line=self._LEAK_SIG_LINE, defect_kind="missing_release")])
            )
        )
        self.assertEqual(out["funnel"]["tool_added"], 1)
        self.assertEqual(out["funnel"]["tool_redundant"], 0)

    def test_other_kind_on_the_same_line_is_still_a_reason_not_to_inject(self):
        """同位置已有别的主张时也不补：工具报的是不是同一件事由 review_check 裁决。

        实测 `resource.c:21` 上 gcc 同时报 double-free 与 use-after-free，而模型只报了
        double free——按「同函数同种类」判定会放行后者，凭空多出一张卡。
        """
        self.write_arch()
        self._tool_report([self._entry(rule="-Wanalyzer-file-leak", line=self._LEAK_END_LINE)])
        out = self.run_review(
            self.small_llm(
                raw_findings([finding(file="a.c", line=self._LEAK_END_LINE, defect_kind="other")])
            )
        )
        self.assertEqual(out["funnel"]["tool_added"], 0)
        self.assertEqual(out["funnel"]["tool_redundant"], 1)

    def test_invalid_tool_location_is_counted_as_rejected(self):
        """位置校验未通过的候选进 `tool_rejected`——「补漏 0 条」必须能区分
        「工具没报」「全被判重复」与「位置对不上」三种情形。"""
        self.write_arch()
        self._tool_report([self._entry(line=9999)])
        out = self.run_review(self.small_llm(raw_findings([])))
        self.assertEqual(out["funnel"]["tool_added"], 0)
        self.assertEqual(out["funnel"]["tool_rejected"], 1)


class FocusedPassTest(_ReviewHarness):
    def test_located_trust_entry_triggers_a_focused_pass(self):
        self.write_arch()
        arch = GlobalArchitecture(
            layers=[],
            modules=[],
            trust_entries=[
                {
                    "file": "a.c",
                    "line": _STRCPY_LINE,
                    "kind": "parse",
                    "description": "外部输入直接进入拷贝",
                    "evidence": f"a.c:{_STRCPY_LINE} strcpy(dst, src);",
                }
            ],
            concurrency_hotspots=[],
        )
        llm = self.small_llm(raw_findings([finding(file="a.c", line=_STRCPY_LINE)]))
        self.run_review(llm, global_arch=arch)
        self.assertEqual(llm.counters[rv.RawFindings], 2, "应额外发起一次专项检视")

    def test_entry_without_evidence_does_not_trigger_it(self):
        """一条凭函数名猜出来的「并发热点」若触发专项检视，代价是白花钱 + 产出一整类假阳性。
        判据是「有行号且有代码依据」，两者缺一即为推测。"""
        self.write_arch()
        arch = GlobalArchitecture(
            layers=[],
            modules=[],
            trust_entries=[],
            concurrency_hotspots=[
                {
                    "file": "a.c",
                    "line": _STRCPY_LINE,
                    "kind": "shared_state",
                    "description": "函数名暗示共享状态",
                    "evidence": "",
                }
            ],
        )
        llm = self.small_llm(raw_findings([]))
        self.run_review(llm, global_arch=arch)
        self.assertEqual(llm.counters[rv.RawFindings], 1)

    def test_entry_for_another_module_does_not_trigger_it(self):
        self.write_arch()
        arch = GlobalArchitecture(
            layers=[],
            modules=[],
            trust_entries=[
                {
                    "file": "b.c",
                    "line": _MEMCPY_LINE,
                    "kind": "parse",
                    "description": "属于 M02",
                    "evidence": f"b.c:{_MEMCPY_LINE} memcpy(...);",
                }
            ],
            concurrency_hotspots=[],
        )
        llm = self.small_llm(raw_findings([]))
        self.run_review(llm, module="M01", global_arch=arch)
        self.assertEqual(llm.counters[rv.RawFindings], 1)

    def test_module_basename_lookup_follows_the_shared_helper(self):
        """模型常把 `src/parser.c` 写成 `parser.c`：basename 在**本模块内唯一**命中即算归属。"""
        self.write_arch()
        arch = GlobalArchitecture(
            layers=[],
            modules=[],
            trust_entries=[],
            concurrency_hotspots=[
                {
                    "file": "src/a.c",
                    "line": _STRCPY_LINE,
                    "kind": "shared_state",
                    "description": "路径写法不同但就是本模块",
                    "evidence": f"a.c:{_STRCPY_LINE} strcpy(dst, src);",
                }
            ],
        )
        llm = self.small_llm(raw_findings([]))
        self.run_review(llm, module="M01", global_arch=arch)
        self.assertEqual(llm.counters[rv.RawFindings], 2)


class FunnelTest(_ReviewHarness):
    def test_funnel_totals_are_consistent(self):
        self.write_arch()
        out = self.run_review(
            self.small_llm(
                raw_findings(
                    [
                        finding(id="a", file="a.c", line=_STRCPY_LINE),
                        finding(id="b", file="a.c", line=9999),
                    ],
                    observations=[finding(id="o1", file="b.c", line=_MEMCPY_LINE, channel="observation")],
                )
            )
        )
        funnel = out["funnel"]
        self.assertEqual(funnel["model_findings"], 2)
        self.assertEqual(funnel["location_invalid"], 1)
        self.assertEqual(funnel["after_dedup"], 1)
        self.assertEqual(funnel["kept_findings"], 1)
        self.assertEqual(funnel["kept_observations"], 2)  # 降级的一条 + 原生的一条


class MissingArchTest(_ReviewHarness):
    def test_missing_arch_artifact_raises_for_isolation(self):
        """架构产物是检视的硬输入：缺了要给可诊断的错误交失败隔离跳过该模块，
        而不是让裸的 JSONDecodeError 冒到批次层。"""
        with self.assertRaises(FileNotFoundError):
            self.run_review(self.small_llm(raw_findings([])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
