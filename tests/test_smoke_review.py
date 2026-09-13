"""端到端冒烟：对真实样例项目跑一遍完整图，真实调用 API。

**默认 skip**，需 `RUN_REAL_LLM_TESTS=1`（与根目录 `tests/test_llm_demo.py` 同一个开关）。
它消耗额度，不该混进随手一句 `unittest discover` 里。

断言只钉**确定性不变量**，不评质量：图上跑通、产物齐全且可解析、报告里的每个位置都指向
真实存在的代码行。质量指标（recall/precision）由 `scripts/eval_review.py` 回答，那是另一
把尺子——把两者混在一起，一次模型波动就会让这个文件变红，而它想报的其实是「管线断了」。

只跑 M01：跨模块阶段（global_arch / module_archs / critical_paths）无论如何都会跑，
单模块已足以覆盖全图，而三模块是三次检视 + 三次反驳的额度。
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import TempProject  # noqa: F401  （导入即完成日志重定向）

from udma_test_review_agent.configuration import ReviewConfig
from udma_test_review_agent.graph import graph
from udma_test_review_agent.manifest import MANIFEST_FILE
from udma_test_review_agent.model_config import resolve_api_key, resolve_model
from udma_test_review_agent.nodes.common import (
    _findings_raw_path,
    _metrics_path,
    _project_findings_path,
)
from udma_test_review_agent.schemas import RawFindings
from udma_test_review_agent.stats import reset_stats
from udma_test_review_agent.versioning import prepare_run

REAL_LLM_ENV = "RUN_REAL_LLM_TESTS"
PROJECT = Path(__file__).resolve().parents[2] / "test_project"
MODULE = "M01"


def _enabled() -> bool:
    return bool(os.getenv(REAL_LLM_ENV, "").strip())


def _key_available() -> bool:
    try:
        return bool(resolve_api_key(resolve_model(None)))
    except Exception:  # 模型配置本身坏了，交给下面那条 skip 的说明去表达
        return False


@unittest.skipUnless(_enabled(), f"真实联网评审，需 {REAL_LLM_ENV}=1 才启用")
class SmokeReviewTest(unittest.TestCase):
    """一次真实评审。断言见模块 docstring。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not PROJECT.is_dir():
            raise unittest.SkipTest(f"样例项目不存在: {PROJECT}")
        if not _key_available():
            raise unittest.SkipTest("未解析到 API 密钥（DEEPSEEK_API_KEY / LLM_API_KEY / opencode 配置）")
        cls._tmp = tempfile.TemporaryDirectory(prefix="udma-smoke-")
        cls.out = Path(cls._tmp.name) / "out"
        cls.cfg = ReviewConfig(
            project_root=str(PROJECT), modules=MODULE, output_root=str(cls.out)
        )
        prepare_run(cls.cfg.output_root, clean=False)
        reset_stats()
        cls.result = graph.invoke({"config": asdict(cls.cfg)})

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def export(self) -> dict:
        return json.loads(_project_findings_path(self.cfg).read_text(encoding="utf-8"))

    def test_the_run_completed_without_module_failures(self):
        self.assertEqual(self.result.get("module_failures") or {}, {})

    def test_report_and_machine_readable_artifacts_exist(self):
        report = Path(self.result["report_path"])
        self.assertTrue(report.is_file(), report)
        self.assertGreater(report.stat().st_size, 0)
        self.assertTrue(_project_findings_path(self.cfg).is_file())
        self.assertTrue(_metrics_path(self.cfg).is_file())
        self.assertTrue((self.out / MANIFEST_FILE).is_file())

    def test_raw_findings_validate_against_the_schema(self):
        """产物是阶段间的接口；跑通一轮后它必须仍满足 schema，否则断点续跑会静默降级。"""
        payload = json.loads(_findings_raw_path(self.cfg, MODULE).read_text(encoding="utf-8"))
        parsed = RawFindings.model_validate(payload)
        self.assertIsNotNone(parsed.funnel)

    def test_every_reported_location_points_at_real_code(self):
        """报告里的每个位置都必须真实存在——这是「可用」的底线。

        不检查位置是否**正确**（那是质量），只检查它没有指向空气：行号越界或文件不存在
        都会让点击链接的人扑空，而这种错在 prompt 改动后最容易悄悄出现。
        """
        export = self.export()
        rows = [*export["findings"], *export.get("observations", [])]
        self.assertTrue(rows, "一轮真实检视不应产出空结果")
        for row in rows:
            with self.subTest(identity=row.get("identity")):
                path = PROJECT / row["file"]
                self.assertTrue(path.is_file(), f"报告引用了不存在的文件: {row['file']}")
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                self.assertGreaterEqual(row["line"], 1, row)
                self.assertLessEqual(row["line"], len(lines), row)

    def test_identities_are_unique_and_verdicts_are_closed_set(self):
        findings = self.export()["findings"]
        identities = [row["identity"] for row in findings]
        self.assertEqual(len(identities), len(set(identities)), "同一主张在报告里出现了两次")
        allowed = {"confirmed", "refuted", "uncertain"}
        for row in findings:
            with self.subTest(identity=row["identity"]):
                self.assertIn(row["conclusion"], allowed)
                self.assertTrue(row["defect_kind"], "缺陷种类为空，跨运行匹配会失准")

    def test_severity_gate_variables_are_present(self):
        """CLI 的 `--fail-on` 读这两个字段；报告节点漏写会让门禁永远不触发。"""
        export = self.export()
        self.assertIn("worst_confirmed_severity", export)
        self.assertIn("counts", export)
        self.assertEqual(
            export["counts"]["confirmed"] + export["counts"]["refuted"] + export["counts"]["uncertain"],
            len(export["findings"]),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
