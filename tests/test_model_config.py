"""模型配置：密钥回退链、角色路由、分层合并与缓存。

`--list-models` 曾把配置里用 `${DEEPSEEK_API_KEY}` 引用（展开后为空）的模型显示成
「未配置」，而运行期 `llm.py` 还会继续回退到环境变量与 opencode 全局配置——于是用户会
据此误判「该换模型」。修法是让两边共用同一条回退链，本文件钉住那条链本身。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import udma_test_review_agent.model_config as mc

from _support import env, write_models_yaml


class _ModelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-models-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def load(self, yaml_text: str, name: str = "custom.yaml"):
        path = self.dir / name
        path.write_text(yaml_text, encoding="utf-8")
        with env(MODEL_CONFIG=str(path), TEST_API_KEY=None, DEEPSEEK_API_KEY=None, LLM_API_KEY=None):
            return mc.load_models_config(str(path))


_BASE = """
active: cheap
providers:
  p:
    base_url: https://example.invalid/v1
models:
  - name: cheap
    provider: p
    model: cheap-model
  - name: strong
    provider: p
    model: strong-model
"""


class ApiKeyFallbackTest(_ModelsTest):
    def test_literal_key_wins(self):
        config = self.load(_BASE.replace("model: cheap-model", "model: cheap-model\n    api_key: literal"))
        with env(DEEPSEEK_API_KEY="from-env"):
            self.assertEqual(mc.resolve_api_key(config.get("cheap")), "literal")

    def test_empty_reference_falls_back_to_environment(self):
        """`${SOME_UNSET_KEY}` 展开为空（变量未设）时，运行期仍会读环境变量，
        `--list-models` 必须看到同一结论，否则会谎报「未配置」。"""
        path = self.dir / "ref.yaml"
        path.write_text(
            _BASE.replace(
                "  p:\n    base_url: https://example.invalid/v1",
                "  p:\n    base_url: https://example.invalid/v1\n    api_key: ${SOME_UNSET_KEY}",
            ),
            encoding="utf-8",
        )
        with env(MODEL_CONFIG=str(path), SOME_UNSET_KEY=None, DEEPSEEK_API_KEY="from-env", LLM_API_KEY=None):
            config = mc.load_models_config(str(path))
            self.assertEqual(config.get("cheap").api_key, "")
            self.assertEqual(mc.resolve_api_key(config.get("cheap")), "from-env")

    def test_llm_api_key_is_the_last_environment_fallback(self):
        path = self.dir / "ref2.yaml"
        path.write_text(_BASE.replace("  cheap-model", "  cheap-model\n    api_key: ${UNSET}"), encoding="utf-8")
        with env(MODEL_CONFIG=str(path), UNSET=None, DEEPSEEK_API_KEY=None, LLM_API_KEY="llm-env"):
            config = mc.load_models_config(str(path))
            self.assertEqual(mc.resolve_api_key(config.get("cheap")), "llm-env")

    def test_opencode_global_config_is_the_final_fallback(self):
        """三条环境回退都空时才读 opencode 全局配置——顺序反了会让显式环境变量失效。"""
        global_config = self.dir / "opencode.json"
        global_config.write_text('{"provider": {"deepseek": {"options": {"apiKey": "from-file"}}}}')
        path = self.dir / "ref3.yaml"
        path.write_text(_BASE, encoding="utf-8")
        with env(
            MODEL_CONFIG=str(path),
            DEEPSEEK_API_KEY=None,
            LLM_API_KEY=None,
            OPENCODE_CONFIG=str(global_config),
        ):
            config = mc.load_models_config(str(path))
            self.assertEqual(mc.resolve_api_key(config.get("cheap")), "from-file")

    def test_missing_key_resolves_to_empty_not_an_error(self):
        path = self.dir / "nokey.yaml"
        path.write_text(_BASE, encoding="utf-8")
        oc = self.dir / "empty.json"
        oc.write_text("{}")
        with env(MODEL_CONFIG=str(path), DEEPSEEK_API_KEY=None, LLM_API_KEY=None, OPENCODE_CONFIG=str(oc)):
            config = mc.load_models_config(str(path))
            self.assertEqual(mc.resolve_api_key(config.get("cheap")), "")

    def test_broken_global_config_degrades_to_empty(self):
        broken = self.dir / "broken.json"
        broken.write_text("{不是 JSON")
        self.assertEqual(mc.load_deepseek_config(str(broken)), {})


class RolesTest(_ModelsTest):
    def test_roles_are_loaded_and_resolvable(self):
        path = self.dir / "roles.yaml"
        path.write_text(_BASE + "roles:\n  review: strong\n  summary: cheap\n", encoding="utf-8")
        with env(MODEL_CONFIG=str(path)):
            self.assertEqual(mc.load_models_config(str(path)).roles,
                             {"review": "strong", "summary": "cheap"})
            self.assertEqual(mc.resolve_role("review", str(path)), "strong")
            self.assertEqual(mc.resolve_role("rebuttal", str(path)), "")

    def test_unlisted_role_resolves_to_empty(self):
        """空串而非回退 active：调用方要区分「显式指定了模型」与「没配，走默认链」——
        后者必须让位给 LLM_PROFILE，把回退做在这里就丢掉了这个区别。"""
        path = self.dir / "roles-empty.yaml"
        path.write_text(_BASE + "roles: {}\n", encoding="utf-8")
        with env(MODEL_CONFIG=str(path)):
            self.assertEqual(mc.resolve_role("review"), "")

    def test_unknown_role_name_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.load(_BASE + "roles:\n  reveiw: strong\n")
        self.assertIn("未知角色", str(ctx.exception))

    def test_role_pointing_at_a_missing_model_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.load(_BASE + "roles:\n  review: nonexistent\n")
        self.assertIn("不存在的模型", str(ctx.exception))

    def test_roles_default_to_empty_when_omitted(self):
        self.assertEqual(self.load(_BASE).roles, {})


class LayeredMergeTest(_ModelsTest):
    def test_model_entry_overrides_provider_and_defaults(self):
        config = self.load(
            """
active: m
defaults:
  type: openai
  temperature: 0.1
  timeout: 11
providers:
  p:
    base_url: https://example.invalid/v1
    temperature: 0.5
models:
  - name: m
    provider: p
    model: mm
    temperature: 0.9
"""
        )
        spec = config.get("m")
        self.assertEqual(spec.temperature, 0.9)
        self.assertEqual(spec.timeout, 11)
        self.assertEqual(spec.base_url, "https://example.invalid/v1")

    def test_blank_value_means_unset_and_inherits(self):
        """YAML 里 `timeout:` 得到 None、`base_url: ""` 得到空串，两者都视为未填写；
        否则手滑的空行会把下层取值顶成 None 并报出下层字段错。"""
        config = self.load(
            """
active: m
defaults:
  type: openai
  timeout: 11
providers:
  p:
    base_url: https://example.invalid/v1
models:
  - name: m
    provider: p
    model: mm
    timeout:
"""
        )
        self.assertEqual(config.get("m").timeout, 11)

    def test_env_placeholder_default_value(self):
        path = self.dir / "def.yaml"
        path.write_text(
            _BASE.replace("    base_url: https://example.invalid/v1",
                          "    base_url: ${UNSET_BASE:-https://fallback.invalid}"),
            encoding="utf-8",
        )
        with env(MODEL_CONFIG=str(path), UNSET_BASE=None):
            self.assertEqual(mc.load_models_config(str(path)).get("cheap").base_url, "https://fallback.invalid")


class ResolutionTest(_ModelsTest):
    def test_cli_profile_beats_environment(self):
        path = self.dir / "res.yaml"
        path.write_text(_BASE, encoding="utf-8")
        with env(MODEL_CONFIG=str(path), LLM_PROFILE="strong"):
            self.assertEqual(mc.resolve_model("cheap").name, "cheap")
            self.assertEqual(mc.resolve_model(None).name, "strong")
            self.assertEqual(mc.resolve_model().name, "strong")

    def test_unknown_profile_is_rejected_with_the_option_list(self):
        path = self.dir / "res2.yaml"
        path.write_text(_BASE, encoding="utf-8")
        with env(MODEL_CONFIG=str(path), LLM_PROFILE=None):
            with self.assertRaises(ValueError) as ctx:
                mc.resolve_model("nope")
        self.assertIn("cheap", str(ctx.exception))

    def test_config_cache_tracks_file_content(self):
        """缓存键是 (mtime_ns, size)：同路径改内容必须让缓存失效，否则改了配置看不到效果。"""
        path = self.dir / "cache.yaml"
        path.write_text(_BASE, encoding="utf-8")
        with env(MODEL_CONFIG=str(path)):
            self.assertEqual(mc.load_models_config(str(path)).active, "cheap")
            path.write_text(_BASE.replace("active: cheap", "active: strong # 改过了"), encoding="utf-8")
            self.assertEqual(mc.load_models_config(str(path)).active, "strong")

    def test_config_cache_is_shared_for_the_same_path(self):
        path = self.dir / "shared.yaml"
        path.write_text(_BASE, encoding="utf-8")
        with env(MODEL_CONFIG=str(path)):
            self.assertIs(mc.load_models_config(str(path)), mc.load_models_config(str(path)))

    def test_reset_config_cache_is_available(self):
        self.assertIsNone(mc.reset_config_cache())


class OverviewTest(_ModelsTest):
    def test_overview_marks_active_and_effective(self):
        path = write_models_yaml(self.dir, active="strong", roles={"review": "cheap"})
        with env(MODEL_CONFIG=str(path), TEST_API_KEY="k", LLM_PROFILE=None):
            overview = mc.describe_models()
        self.assertEqual(overview.active, "strong")
        self.assertEqual(overview.effective, "strong")
        self.assertEqual(overview.roles, {"review": "cheap"})
        marks = {item.name: item.active for item in overview.items}
        self.assertEqual(marks, {"cheap": False, "strong": True})

    def test_overview_reports_the_resolved_key_state(self):
        """密钥走引用式配置、只由环境变量提供时，总览必须显示「已配置」。

        两次用**不同路径的配置**：`load_models_config` 按 (mtime_ns, size) 缓存，
        同一路径换环境变量不会重新展开 `${...}`——真实场景里 `--list-models` 是独立进程，
        但同进程内改环境再看总览会命中这份缓存。
        """
        configured = write_models_yaml(self.dir)
        with env(MODEL_CONFIG=str(configured), TEST_API_KEY="k", DEEPSEEK_API_KEY=None, LLM_API_KEY=None):
            overview = mc.describe_models()
        self.assertTrue(all(item.has_api_key for item in overview.items))

        unconfigured = write_models_yaml(self.dir)
        with env(MODEL_CONFIG=str(unconfigured), TEST_API_KEY=None, DEEPSEEK_API_KEY=None,
                 LLM_API_KEY=None, OPENCODE_CONFIG=str(self.dir / "none.json")):
            overview = mc.describe_models()
        self.assertFalse(any(item.has_api_key for item in overview.items))

    def test_overview_exposes_only_the_known_roles(self):
        self.assertEqual(mc.ROLES, ("arch", "review", "rebuttal", "summary"))


class SeedLayeringTest(_ModelsTest):
    """`seed` 与 `temperature` 同为条目级采样参数，走同一套分层继承。

    它此前**完全没有入口**：`ReviewConfig.seed` 一路传到 `llm.py` 的请求参数，
    但 CLI 没有 `--seed`、models.yaml 也没有这个字段，于是真实运行里它恒为 None、
    那几行代码永远不执行——「透传写完了却没接上」，与 P1 修的死配置同一类问题。
    """

    def test_seed_is_inherited_through_every_layer(self):
        spec = self.load(
            """
active: m
defaults:
  type: openai
  seed: 111
providers:
  p:
    base_url: https://example.invalid/v1
models:
  - name: m
    provider: p
    model: mm
"""
        ).get("m")
        self.assertEqual(spec.seed, 111)

    def test_model_entry_overrides_provider_and_defaults(self):
        spec = self.load(
            """
active: m
defaults:
  type: openai
  seed: 111
providers:
  p:
    base_url: https://example.invalid/v1
    seed: 222
models:
  - name: m
    provider: p
    model: mm
    seed: 333
"""
        ).get("m")
        self.assertEqual(spec.seed, 333)

    def test_seed_zero_is_a_real_value_not_an_empty_one(self):
        """0 是合法种子。若按假值判空，`seed: 0` 会被静默丢掉、退回不发送——
        而「不发送」意味着供应商自由采样，与「固定为 0」是相反的行为。"""
        spec = self.load(
            """
active: m
defaults:
  type: openai
  seed: 0
providers:
  p:
    base_url: https://example.invalid/v1
models:
  - name: m
    provider: p
    model: mm
"""
        ).get("m")
        self.assertEqual(spec.seed, 0)

    def test_omitting_seed_leaves_it_unset(self):
        spec = self.load(_BASE).get("cheap")
        self.assertIsNone(spec.seed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
