"""CLI 的退出码策略与 `--list-models` 输出。

退出码是这套工具接入构建门禁的唯一接口：检出致命 double free 却退 0，门禁就形同虚设。
默认 `--fail-on none` 保持 0，是为了不破坏既有用法——所以「默认不变」这条本身也要钉住。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# `_support` 必须先于 `run_review` 导入：后者会拉起整张图，而 `graph` 模块在 import 期就
# 打了一行「已编译」DEBUG。那行发生在 `_support` 把日志文件挪走之前，会直接落进真实的
# `output/llm_test.log`——import 顺序本身就是这条泄漏的开关。
from _support import TempProject, env, write_models_yaml

import run_review

_SOURCE = {"a.c": "int a(void) { return 1; }\n"}


class _CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TempProject(_SOURCE)
        self.addCleanup(self.project.cleanup)
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.models = write_models_yaml(self.dir)
        self._env = env(MODEL_CONFIG=str(self.models), TEST_API_KEY="k", LLM_PROFILE=None)
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self.cfg = self.project.config(output_root=str(self.dir / "out"))

    def write_findings(self, worst: str, counts: dict[str, int] | None = None) -> None:
        payload = {
            "schema_version": 1,
            "worst_confirmed_severity": worst,
            "counts": counts or {"confirmed": 1, "refuted": 0, "uncertain": 0, "observations": 0},
        }
        path = self.dir / "out" / "findings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def summary(self, fail_on: str, failed: dict[str, str] | None = None) -> dict:
        return run_review._run_summary(self.cfg, {"report_path": "r.md"}, failed or {}, fail_on)


class ExitCodeTest(_CliTest):
    def test_default_gate_is_off(self):
        """`--fail-on none` 下即使检出致命缺陷也退 0：这是既有用法的默认行为。"""
        self.write_findings("致命")
        self.assertEqual(self.summary("none")["exit_code"], 0)

    def test_fatal_triggers_the_gate(self):
        self.write_findings("致命")
        summary = self.summary("致命")
        self.assertEqual(summary["exit_code"], run_review.EXIT_SEVERITY_THRESHOLD)
        self.assertEqual(summary["worst_confirmed_severity"], "致命")

    def test_severe_meets_the_severe_gate(self):
        self.write_findings("严重")
        self.assertEqual(
            self.summary("严重")["exit_code"], run_review.EXIT_SEVERITY_THRESHOLD
        )

    def test_severe_does_not_meet_the_fatal_gate(self):
        """门槛取 `<=`：`--fail-on 致命` 只对致命命中，严重不得拦下构建。"""
        self.write_findings("严重")
        self.assertEqual(self.summary("致命")["exit_code"], 0)

    def test_general_severity_never_triggers(self):
        self.write_findings("一般")
        self.assertEqual(self.summary("严重")["exit_code"], 0)
        self.assertEqual(self.summary("致命")["exit_code"], 0)

    def test_no_confirmed_findings_never_triggers(self):
        self.write_findings("")
        self.assertEqual(self.summary("严重")["exit_code"], 0)

    def test_module_failure_beats_the_severity_gate(self):
        """结果不完整时退 1：此时严重度结论本身就不可信，门禁不该据此报「通过」。"""
        self.write_findings("致命")
        summary = self.summary("致命", failed={"M02": "反驳超时"})
        self.assertEqual(summary["exit_code"], run_review.EXIT_MODULE_FAILURE)
        self.assertEqual(summary["failed_modules"], {"M02": "反驳超时"})

    def test_missing_artifact_leaves_the_gate_inert(self):
        """读不到产物时放行，而不是凭一个读不到的产物拦下构建。

        宁可漏报也不能误拦——误拦会让整条流水线停在一个无法诊断的状态上。
        """
        self.assertEqual(self.summary("致命")["exit_code"], 0)

    def test_corrupt_artifact_leaves_the_gate_inert(self):
        path = self.dir / "out" / "findings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{不是 JSON", encoding="utf-8")
        self.assertEqual(self.summary("致命")["exit_code"], 0)

    def test_summary_carries_the_paths_a_consumer_needs(self):
        self.write_findings("严重")
        summary = self.summary("none")
        self.assertTrue(summary["findings_path"].endswith("findings.json"))
        self.assertTrue(summary["metrics_path"].endswith("metrics.json"))
        self.assertIn("llm_calls", summary)
        self.assertIn("total_tokens", summary)
        self.assertIn("wall_clock", summary)

    def test_rates_come_from_the_artifact(self):
        self.write_findings("严重", {"confirmed": 1, "refuted": 3, "uncertain": 0, "observations": 4})
        filter_block = self.summary("none")["filter"]
        self.assertEqual(filter_block["findings_total"], 4)
        self.assertEqual(filter_block["refuted_rate"], 0.75)
        self.assertEqual(filter_block["observations"], 4)


class FailOnChoicesTest(unittest.TestCase):
    def test_choices_match_the_rank_table(self):
        """`--fail-on` 的取值与 `FAIL_ON_RANK` 必须同源；加等级却忘了改 choices，
        用户就只能靠猜。"""
        parser = run_review._build_parser()
        action = next(a for a in parser._actions if a.dest == "fail_on")
        self.assertEqual(set(action.choices), {"none", *run_review.FAIL_ON_RANK})
        self.assertEqual(action.default, "none")

    def test_project_is_not_required_on_the_parser(self):
        """`--list-models` 要求无需 `--project`；必填校验放在 main() 里按分支处理。"""
        parser = run_review._build_parser()
        args = parser.parse_args(["--list-models"])
        self.assertEqual(args.project, "")

    def test_rank_table_only_holds_gating_levels(self):
        from udma_test_review_agent import findings_util as fu

        self.assertEqual(run_review.FAIL_ON_RANK["致命"], fu.severity_rank("致命"))
        self.assertLess(run_review.FAIL_ON_RANK["致命"], run_review.FAIL_ON_RANK["严重"])


class ListModelsTest(_CliTest):
    def _run(self, profile: str | None = None) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_review._print_models(profile)
        return code, buffer.getvalue()

    def test_lists_roles_and_the_priority_chain(self):
        code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("角色路由", text)
        for role in ("arch", "review", "rebuttal", "summary"):
            self.assertIn(role, text)
        self.assertIn("--model", text)
        self.assertIn("LLM_PROFILE", text)

    def test_configured_key_is_reported_as_configured(self):
        """配置里用 `${TEST_API_KEY}` 引用、值由环境变量提供——总览必须显示「已配置」，
        否则用户会据此误判该换模型。"""
        _, text = self._run()
        key_column = [line for line in text.splitlines() if line.startswith(("cheap ", "strong "))]
        self.assertEqual(len(key_column), 2)
        for line in key_column:
            with self.subTest(line=line):
                self.assertTrue(line.endswith("已配置"), line)

    def test_unknown_profile_reports_an_error_and_exits_nonzero(self):
        code, text = self._run("nope")
        self.assertEqual(code, 1)
        self.assertIn("nope", text)

    def test_role_targets_are_shown_when_configured(self):
        routed = write_models_yaml(self.dir, roles={"review": "strong"})
        with env(MODEL_CONFIG=str(routed)):
            _, text = self._run()
        self.assertIn("review    -> strong", text)
        self.assertIn("arch      -> (未配置)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
