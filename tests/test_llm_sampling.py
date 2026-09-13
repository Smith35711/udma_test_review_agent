"""采样参数组装：显式入参 > 模型条目，且「不指定」与「指定为 0」必须可区分。

采样参数曾**完全不生效**：配置里的 `temperature` 只走 `create_llm` 那条 langchain
路径，而生产节点全部走流式路径，等于实际使用供应商默认值。修复后两个字段都透传，
本文件钉住它们的取值方向与「缺席」语义——那是这层最容易悄悄写错的地方：
把 `seed` 未指定当成 0 发送，与「不发送、由供应商自由采样」是相反的行为。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent.llm import _sampling_params
from udma_test_review_agent.model_config import ModelSpec


def spec(**overrides) -> ModelSpec:
    data = {
        "name": "m",
        "model": "mm",
        "base_url": "https://example.invalid/v1",
        "temperature": 0.7,
        "seed": None,
    }
    data.update(overrides)
    return ModelSpec.model_validate(data)


class SamplingParamsTest(unittest.TestCase):
    def test_temperature_falls_back_to_the_model_entry(self):
        self.assertEqual(_sampling_params(spec(temperature=0.3), None, None)["temperature"], 0.3)

    def test_explicit_temperature_wins_over_the_entry(self):
        self.assertEqual(_sampling_params(spec(temperature=0.3), 1.2, None)["temperature"], 1.2)

    def test_seed_falls_back_to_the_model_entry(self):
        self.assertEqual(_sampling_params(spec(seed=42), None, None)["seed"], 42)

    def test_explicit_seed_wins_over_the_entry(self):
        self.assertEqual(_sampling_params(spec(seed=42), None, 7)["seed"], 7)

    def test_seed_is_omitted_when_neither_layer_specifies_it(self):
        """**不发送 ≠ 发送 0**：0 是一个合法种子，发送它等于把采样固定住。"""
        params = _sampling_params(spec(seed=None), None, None)
        self.assertNotIn("seed", params)
        self.assertIn("temperature", params)

    def test_seed_zero_is_sent_rather_than_dropped(self):
        """反过来：明确写下的 0 必须发出去，不能被假值判空吃掉。"""
        self.assertEqual(_sampling_params(spec(seed=0), None, None)["seed"], 0)
        self.assertEqual(_sampling_params(spec(), None, 0)["seed"], 0)

    def test_temperature_is_always_present(self):
        """temperature 总有值（条目默认 0.7），故它与 seed 的缺席语义不同。"""
        self.assertEqual(_sampling_params(spec(), None, None), {"temperature": 0.7})


if __name__ == "__main__":
    unittest.main(verbosity=2)
