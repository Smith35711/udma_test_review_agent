"""总报告的确定性渲染与机器可读产物。

`findings.json` 是 CI 门禁与后续基线抑制的接口，`report.md` 是人读的那份。两者共用
同一批读盘结果（各读一遍会让报告与机器产物互相矛盾），故这里从同一份夹具同时钉住两边：
稳定身份、退出码依据、以及**链接基准**——总报告曾把模块 Markdown 原样内联，模块报告的
相对链接按 `<模块>/` 算，内联到 `<输出根>/` 后每一条都错两级。
"""

from __future__ import annotations

from dataclasses import asdict
import json
import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent.nodes.common import (
    _findings_verified_path,
    _manifest_path,
    _metrics_path,
    _project_findings_path,
    _write_json,
)
from udma_test_review_agent.nodes.llm.module_report import _location_link
from udma_test_review_agent.nodes.tool import report as rp

from _support import TempProject, env, finding, result_item, verdict, write_models_yaml

_SOURCE = {
    "src/a.c": """
        #include <string.h>

        void copy(char *src) {
            char dst[8];
            strcpy(dst, src);
        }
        """,
    "inc/a.h": "void copy(char *src);\n",
}
"""路径刻意带一层目录：链接基准写错时相对层级才会显形。`strcpy` 在第 6 行。"""

_STRCPY_LINE = 6


class _ReportHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TempProject(_SOURCE)
        self.addCleanup(self.project.cleanup)
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-report-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "out"
        # 环境变量整场景常驻：`_config` 会从 state 里的 asdict 重建 ReviewConfig，
        # 重建时同样要读到本项目的模块映射，否则会去读真实的 config/modules.yaml。
        self._env = env(
            MODEL_CONFIG=str(write_models_yaml(Path(self._tmp.name))),
            TEST_API_KEY="k",
            MODULES_CONFIG=str(self.project.modules_config),
        )
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self.cfg = self.project.config(output_root=str(self.out))

    def write_module(self, module: str, results: list[dict], **extra) -> None:
        payload = {"results": results, **extra}
        _write_json(_findings_verified_path(self.cfg, module), payload)

    def run_report(self, order: list[str], failures: dict[str, str] | None = None) -> dict:
        # state["config"] 是 `asdict()` 产物（见 run_review.py），节点据此重建配置。
        state = {
            "config": asdict(self.cfg),
            "module_order": order,
            "module_failures": failures or {},
        }
        return rp.report_node(state)

    def export(self) -> dict:
        return json.loads(_project_findings_path(self.cfg).read_text(encoding="utf-8"))


class ExportTest(_ReportHarness):
    @staticmethod
    def _anchored(**overrides) -> dict:
        """带锚点的 finding（检视阶段 `_enrich` 的真实产物形状）。"""
        item = finding(file="src/a.c", line=_STRCPY_LINE, **overrides)
        item.setdefault("anchor_fingerprint", "abcd1234")
        item.setdefault("anchor", f"{item['file']}:{item['line']}#{item['anchor_fingerprint']}")
        return item

    def test_identity_is_anchor_plus_closed_set_kind(self):
        """CI 做基线抑制要靠 identity 跨运行匹配，而不是靠 `f1` 这类当次运行的序号。"""
        self.write_module("M01", [result_item(self._anchored())])
        self.run_report(["M01"])
        row = self.export()["findings"][0]
        self.assertEqual(row["defect_kind"], "unbounded_write")
        self.assertEqual(row["identity"], "src/a.c:6#abcd1234|unbounded_write")
        self.assertEqual(row["identity"], f"{row['anchor']}|{row['defect_kind']}")

    def test_identity_degrades_to_file_line_without_a_fingerprint(self):
        """缺锚点时退化为 `file:line`，而不是编一个假指纹——编了会让跨运行匹配失准。"""
        self.write_module("M01", [result_item(finding(file="src/a.c", line=_STRCPY_LINE))])
        self.run_report(["M01"])
        self.assertEqual(self.export()["findings"][0]["identity"], "src/a.c:6|unbounded_write")

    def test_identity_survives_a_rerun_with_different_ids(self):
        first_ids = []
        for run_id in ("f1", "f7"):
            self.write_module("M01", [result_item(finding(id=run_id, file="src/a.c", line=_STRCPY_LINE))])
            self.run_report(["M01"])
            first_ids.append(self.export()["findings"][0]["identity"])
        self.assertEqual(first_ids[0], first_ids[1])

    def test_verdict_severity_wins_over_finding_severity(self):
        """反驳阶段复核后的等级才是结论；模型自报的等级只是输入。"""
        self.write_module(
            "M01",
            [result_item(finding(file="src/a.c", line=_STRCPY_LINE, severity="提示"), severity="致命")],
        )
        self.run_report(["M01"])
        self.assertEqual(self.export()["findings"][0]["severity"], "致命")

    def test_free_form_severity_is_normalized(self):
        self.write_module(
            "M01",
            [result_item(finding(file="src/a.c", line=_STRCPY_LINE), severity="CRITICAL")],
        )
        self.run_report(["M01"])
        self.assertEqual(self.export()["findings"][0]["severity"], "致命")

    def test_counts_and_worst_confirmed_severity(self):
        self.write_module(
            "M01",
            [
                result_item(finding(id="f1", file="src/a.c", line=_STRCPY_LINE), severity="严重"),
                result_item(finding(id="f2", file="src/a.c", line=2), conclusion="refuted"),
                result_item(finding(id="f3", file="src/a.c", line=4), conclusion="uncertain"),
            ],
            observations=[finding(id="o1", file="inc/a.h", line=1, channel="observation")],
            checklist_gaps=["src/a.c:2 [memory] 核对边界"],
        )
        self.run_report(["M01"])
        export = self.export()
        self.assertEqual(export["counts"]["confirmed"], 1)
        self.assertEqual(export["counts"]["refuted"], 1)
        self.assertEqual(export["counts"]["uncertain"], 1)
        self.assertEqual(export["counts"]["observations"], 1)
        self.assertEqual(export["counts"]["checklist_gaps"], 1)
        # 只统计 confirmed：uncertain 是要人看的，不该让构建失败。
        self.assertEqual(export["worst_confirmed_severity"], "严重")

    def test_worst_confirmed_ignores_refuted_fatal(self):
        self.write_module(
            "M01",
            [result_item(finding(file="src/a.c", line=_STRCPY_LINE), conclusion="refuted", severity="致命")],
        )
        self.run_report(["M01"])
        self.assertEqual(self.export()["worst_confirmed_severity"], "")

    def test_observations_keep_their_own_identity(self):
        self.write_module(
            "M01",
            [],
            observations=[
                finding(
                    id="o1",
                    file="inc/a.h",
                    line=1,
                    channel="observation",
                    anchor="inc/a.h:1#beefcafe",
                    anchor_fingerprint="beefcafe",
                )
            ],
        )
        self.run_report(["M01"])
        row = self.export()["observations"][0]
        self.assertEqual(row["anchor"], "inc/a.h:1#beefcafe")
        self.assertEqual(row["id"], "o1")
        self.assertEqual(row["identity"], "inc/a.h:1#beefcafe|unbounded_write")

    def test_schema_version_and_manifest_digest_are_recorded(self):
        self.write_module("M01", [])
        self.run_report(["M01"])
        export = self.export()
        self.assertEqual(export["schema_version"], rp.EXPORT_SCHEMA_VERSION)
        self.assertTrue(export["generated_at"])
        self.assertEqual(export["manifest_digest"], "")  # 未写清单时不编造指纹

    def test_failed_module_is_recorded_and_excluded_from_counts(self):
        self.write_module("M01", [result_item(finding(file="src/a.c", line=_STRCPY_LINE))])
        result = self.run_report(["M01", "M02"], failures={"M02": "反驳超时"})
        export = self.export()
        self.assertEqual(export["failed_modules"], {"M02": "反驳超时"})
        self.assertEqual(export["counts"]["confirmed"], 1)
        self.assertEqual(result["module_failures"], {"M02": "反驳超时"})

    def test_module_without_artifact_is_marked_failed_not_fatal(self):
        """阶段未报错却无有效产物（被中断或写入截断）时按失败处理，而不是让整轮崩掉。"""
        self.write_module("M01", [])
        result = self.run_report(["M01", "M09"])
        self.assertIn("M09", result["module_failures"])
        self.assertTrue(self.export()["failed_modules"])


class MetricsExportTest(_ReportHarness):
    def test_funnel_is_summed_across_modules(self):
        funnel = {
            "model_findings": 5,
            "location_invalid": 1,
            "tool_added": 2,
            "after_dedup": 4,
            "capped": 0,
            "kept_findings": 4,
        }
        self.write_module("M01", [result_item(finding(file="src/a.c", line=_STRCPY_LINE))], funnel=funnel)
        self.write_module("M02", [result_item(finding(file="src/a.c", line=2))], funnel=funnel)
        self.run_report(["M01", "M02"])
        metrics = json.loads(_metrics_path(self.cfg).read_text(encoding="utf-8"))
        self.assertEqual(metrics["filter"]["funnel"]["model_findings"], 10)
        self.assertEqual(metrics["filter"]["funnel"]["tool_added"], 4)
        self.assertEqual(len(metrics["modules"]), 2)

    def test_rates_are_reported_and_zero_safe(self):
        self.write_module("M01", [])
        self.run_report(["M01"])
        metrics = json.loads(_metrics_path(self.cfg).read_text(encoding="utf-8"))
        self.assertEqual(metrics["filter"]["refuted_rate"], 0.0)
        self.assertEqual(metrics["filter"]["observation_rate"], 0.0)

    def test_observation_rate_counts_both_denominators(self):
        self.write_module(
            "M01",
            [result_item(finding(file="src/a.c", line=_STRCPY_LINE))],
            observations=[finding(id="o1", file="inc/a.h", line=1, channel="observation")],
        )
        self.run_report(["M01"])
        metrics = json.loads(_metrics_path(self.cfg).read_text(encoding="utf-8"))
        self.assertEqual(metrics["filter"]["observation_rate"], 0.5)


class ReportRenderingTest(_ReportHarness):
    def test_report_records_provenance(self):
        """原报告完全没有记录用了什么模型、什么阈值、看了哪份源码，
        事后无法回答「这份结论是怎么来的」。"""
        self.write_module("M01", [result_item(finding(file="src/a.c", line=_STRCPY_LINE))])
        self.run_report(["M01"])
        text = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("本次运行", text)
        self.assertIn(self.cfg.project_root, text)
        self.assertIn("阈值", text)
        self.assertIn("过程指标", text)

    def test_gaps_section_is_rendered_prominently(self):
        self.write_module(
            "M01", [result_item(finding(file="src/a.c", line=_STRCPY_LINE))],
            checklist_gaps=["src/a.c:2 [memory] 核对边界"],
        )
        self.run_report(["M01"])
        text = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("已定位但无回应的检查项", text)
        self.assertIn("src/a.c:2 [memory] 核对边界", text)

    def test_cross_module_candidates_are_rendered(self):
        self.write_module("M01", [])
        self.run_report(["M01"])  # 建好目录与产物
        rp.report_node(
            {
                "config": asdict(self.cfg),
                "module_order": ["M01"],
                "module_failures": {},
                "cross_module_candidates": [
                    {"title": "跨模块契约", "file": "inc/a.h", "description": "待人工确认"}
                ],
            }
        )
        self.assertIn("跨模块候选", (self.out / "report.md").read_text(encoding="utf-8"))

    def test_failed_module_section_is_rendered(self):
        self.write_module("M01", [])
        self.run_report(["M01", "M09"])
        text = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("未完成模块", text)
        self.assertIn("M09", text)


class LinkBaseTest(unittest.TestCase):
    """链接基准：模块报告在 `<模块>/report.md`，总报告在 `<输出根>/report.md`，
    两者到被测源码的相对层级不同（实测差了两级）。"""

    def test_link_is_relative_to_the_reading_directory(self):
        project = Path("/proj")
        module_dir = Path("/out/M01")
        overall_dir = Path("/out")
        from_module = _location_link(str(project), module_dir, "src/a.c", 6)
        from_overall = _location_link(str(project), overall_dir, "src/a.c", 6)
        self.assertIn("../../proj/src/a.c", from_module)
        self.assertIn("../proj/src/a.c", from_overall)
        self.assertNotEqual(from_module, from_overall)

    def test_line_anchor_is_preserved(self):
        self.assertTrue(_location_link("/proj", Path("/out"), "a.c", 42).endswith("#L42)"))

    def test_degrades_to_plain_text_without_project_root(self):
        self.assertEqual(_location_link("", Path("/out"), "a.c", 3), "`a.c:3`")

    def test_degrades_to_plain_text_without_file(self):
        self.assertEqual(_location_link("/proj", Path("/out"), "", 3), "`:3`")


class ModuleReportLinkTest(_ReportHarness):
    def test_module_report_links_are_relative_to_the_module_directory(self):
        from udma_test_review_agent.nodes.llm.module_report import render_module_report

        payload = {"results": [result_item(finding(file="src/a.c", line=_STRCPY_LINE))]}
        text = render_module_report(
            "M01", payload, {"size_class": "small", "lines": 10}, self.cfg.project_root, self.out / "M01"
        )
        self.assertIn("../../", text)
        self.assertNotIn("](src/a.c", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
