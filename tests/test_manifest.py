"""运行清单的构建、比对与断点失效判定。

断点续跑只问「产物文件在不在」，产物却不知道自己是在什么输入下生成的。于是改了源码、
换了模型、改了提示词之后重跑，架构产物被继续复用——一份报告里混着两套来源的结论，而
**这不会有任何报错**。本文件钉住的是「输入变了，指纹必须跟着变」这条唯一的防线。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent import manifest as mf

from _support import TempProject, env, write_models_yaml

_SOURCES = {
    "a.c": "int a(void) { return 1; }\n",
    "inc/a.h": "int a(void);\n",
}


class _ManifestTest(unittest.TestCase):
    """公共脚手架：临时项目 + 临时模型配置，环境变量在 `env()` 内生效。"""

    def setUp(self) -> None:
        self.project = TempProject(_SOURCES)
        self.addCleanup(self.project.cleanup)
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-models-")
        self.addCleanup(self._tmp.cleanup)
        self.models_dir = Path(self._tmp.name)

    def build(self, **config_overrides):
        models = write_models_yaml(self.models_dir)
        with env(MODEL_CONFIG=str(models), TEST_API_KEY="k", LLM_PROFILE=None):
            cfg = self.project.config(**config_overrides)
            return mf.build_manifest(cfg), cfg


class SourceHashTest(_ManifestTest):
    def test_hashes_relative_paths_of_c_and_h_only(self):
        self.project.write("notes.txt", "忽略我\n")
        self.project.write("build/generated.c", "int g(void);\n")
        with env(MODEL_CONFIG=str(write_models_yaml(self.models_dir)), TEST_API_KEY="k"):
            hashes = mf.source_hashes(self.project.config())
        self.assertEqual(sorted(hashes), ["a.c", "inc/a.h"])

    def test_content_change_changes_only_that_file_hash(self):
        with env(MODEL_CONFIG=str(write_models_yaml(self.models_dir)), TEST_API_KEY="k"):
            before = mf.source_hashes(self.project.config())
            self.project.write("a.c", "int a(void) { return 2; }\n")
            after = mf.source_hashes(self.project.config())
        self.assertNotEqual(before["a.c"], after["a.c"])
        self.assertEqual(before["inc/a.h"], after["inc/a.h"])

    def test_missing_file_hashes_as_missing_not_absent(self):
        """哈希失败必须留下显式取值：`missing` 与「文件不存在」在比对时不是一回事。"""
        self.assertEqual(mf._file_digest(self.project.root / "nope.c"), "missing")


class ManifestDigestTest(_ManifestTest):
    def test_same_inputs_produce_the_same_digest(self):
        first, _ = self.build()
        second, _ = self.build()
        self.assertEqual(first.digest, second.digest)

    def test_display_fields_do_not_enter_the_fingerprint(self):
        """`created_at` 每次都在变；它若参与指纹，每次重跑都会判为全量陈旧。"""
        first, _ = self.build()
        second, _ = self.build()
        self.assertNotEqual(first.created_at, "")  # 字段本身存在
        self.assertEqual(mf.fingerprint(first), mf.fingerprint(second))
        self.assertNotIn("created_at", mf.FINGERPRINT_FIELDS)

    def test_source_edit_invalidates_the_digest(self):
        before, _ = self.build()
        self.project.write("a.c", "int a(void) { return 3; }\n")
        after, _ = self.build()
        self.assertNotEqual(before.digest, after.digest)
        self.assertIn("源码内容变化", " ".join(mf.describe_changes(before, after)))
        self.assertIn("a.c", " ".join(mf.describe_changes(before, after)))

    def test_added_and_removed_files_are_named(self):
        before, _ = self.build()
        self.project.write("b.c", "int b(void) { return 1; }\n")
        added, _ = self.build()
        brief = " ".join(mf.describe_changes(before, added))
        self.assertIn("新增", brief)
        self.assertIn("b.c", brief)

        (self.project.root / "b.c").unlink()
        removed, _ = self.build()
        self.assertIn("删除", " ".join(mf.describe_changes(added, removed)))

    def test_threshold_change_is_reported(self):
        before, _ = self.build()
        after, _ = self.build(small_module_line_threshold=10)
        self.assertNotEqual(before.digest, after.digest)
        self.assertIn("阈值变化", " ".join(mf.describe_changes(before, after)))

    def test_concurrency_change_is_reported(self):
        before, _ = self.build()
        after, _ = self.build(max_parallel=3)
        self.assertIn("并发数", " ".join(mf.describe_changes(before, after)))

    def test_model_change_is_reported(self):
        before, _ = self.build()
        models = write_models_yaml(self.models_dir, active="strong")
        with env(MODEL_CONFIG=str(models), TEST_API_KEY="k", LLM_PROFILE=None):
            after = mf.build_manifest(self.project.config())
        self.assertNotEqual(before.model_digest, after.model_digest)
        self.assertIn("模型", " ".join(mf.describe_changes(before, after)))

    def test_api_key_does_not_enter_the_model_digest(self):
        """换密钥不影响任何结论，也不该写进会落盘的指纹里。"""
        models = write_models_yaml(self.models_dir)
        with env(MODEL_CONFIG=str(models), TEST_API_KEY="k1", LLM_PROFILE=None):
            first = mf.build_manifest(self.project.config())
        with env(MODEL_CONFIG=str(models), TEST_API_KEY="k2", LLM_PROFILE=None):
            second = mf.build_manifest(self.project.config())
        self.assertEqual(first.model_digest, second.model_digest)

    def test_role_routing_enters_the_model_digest(self):
        """角色路由是「哪个阶段用哪个模型」的一部分：改了它却不失效断点，
        就会拿旧路由产出的架构去配新路由产出的检视。"""
        plain = write_models_yaml(self.models_dir)
        routed = write_models_yaml(self.models_dir, roles={"review": "strong"})
        with env(MODEL_CONFIG=str(plain), TEST_API_KEY="k", LLM_PROFILE=None):
            before = mf.build_manifest(self.project.config())
        with env(MODEL_CONFIG=str(routed), TEST_API_KEY="k", LLM_PROFILE=None):
            after = mf.build_manifest(self.project.config())
        self.assertNotEqual(before.model_digest, after.model_digest)

    def test_prompt_edit_changes_the_prompt_digest(self):
        """提示词是行为的一部分：改了措辞而断点照旧命中，就是拿旧结论冒充新提示词的结果。"""
        with tempfile.TemporaryDirectory(prefix="udma-prompts-") as tmp:
            directory = Path(tmp)
            (directory / "alpha.py").write_text("A = 'x'\n", encoding="utf-8")
            original = mf._PROMPT_DIR
            mf._PROMPT_DIR = directory
            try:
                before = mf._prompts_digest()
                (directory / "alpha.py").write_text("A = 'y'\n", encoding="utf-8")
                self.assertNotEqual(before, mf._prompts_digest())
                (directory / "beta.py").write_text("B = 'z'\n", encoding="utf-8")
                self.assertNotEqual(before, mf._prompts_digest())
            finally:
                mf._PROMPT_DIR = original

    def test_real_prompt_digest_is_present(self):
        _, _ = self.build()
        self.assertNotEqual(mf._prompts_digest(), "none")
        self.assertRegex(mf._prompts_digest(), r"^[0-9a-f]{16}$")


class ChangeDescriptionTest(unittest.TestCase):
    def test_missing_baseline_says_so(self):
        self.assertEqual(len(mf.describe_changes(None, None)), 1)

    def test_identical_manifests_describe_nothing(self):
        payload = {
            "created_at": "t",
            "project_root": "/p",
            "sources_digest": "s",
            "module_map_digest": "m",
            "prompts_digest": "pr",
            "model_digest": "mo",
        }
        first = mf.RunManifest(**payload)
        first.digest = mf.fingerprint(first)
        self.assertEqual(mf.describe_changes(first, first.model_copy(deep=True)), [])

    def test_artifact_version_bump_is_reported(self):
        payload = {
            "created_at": "t",
            "project_root": "/p",
            "sources_digest": "s",
            "module_map_digest": "m",
            "prompts_digest": "pr",
            "model_digest": "mo",
        }
        old = mf.RunManifest(**payload)
        new = mf.RunManifest(**{**payload, "artifact_version": 99})
        self.assertIn("产物格式版本", " ".join(mf.describe_changes(old, new)))


class PersistenceTest(_ManifestTest):
    def test_round_trip_preserves_the_digest(self):
        manifest, cfg = self.build()
        path = Path(self._tmp.name) / "sub" / mf.MANIFEST_FILE
        mf.save_manifest(path, manifest)
        loaded = mf.load_manifest(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.digest, manifest.digest)

    def test_missing_manifest_is_not_an_error(self):
        self.assertIsNone(mf.load_manifest(Path(self._tmp.name) / "nope.json"))

    def test_corrupt_manifest_degrades_to_none(self):
        """读不出来时按「无有效断点依据」处理（全量重算），而不是让整轮崩掉。"""
        path = Path(self._tmp.name) / mf.MANIFEST_FILE
        path.write_text("{不是合法 JSON", encoding="utf-8")
        self.assertIsNone(mf.load_manifest(path))

    def test_manifest_json_is_written_utf8(self):
        manifest, _ = self.build()
        path = Path(self._tmp.name) / mf.MANIFEST_FILE
        mf.save_manifest(path, manifest)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["digest"], manifest.digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
