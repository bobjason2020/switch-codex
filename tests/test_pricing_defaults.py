"""内置默认单价 DEFAULT_PRICING 的合并/落库语义（不写真实 DB）。"""
from __future__ import annotations

import unittest
from unittest import mock

from sy import core, db
from sy.const import DEFAULT_CLIENT_MODELS, DEFAULT_PRICING, GROK_CLIENT_MODELS


class FakeConfigStore:
    """把 db.load_config_raw / save_config_raw 换成内存字典。"""

    def __init__(self, initial=None):
        self.cfg = dict(initial or {})

    def load(self):
        return dict(self.cfg)

    def save(self, cfg):
        self.cfg = dict(cfg)


class PricingDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeConfigStore()
        patches = [
            mock.patch.object(db, "load_config_raw", side_effect=self.store.load),
            mock.patch.object(db, "save_config_raw", side_effect=self.store.save),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_empty_database_falls_back_to_defaults(self):
        pricing = core.load_pricing()
        self.assertEqual(pricing, DEFAULT_PRICING)
        for model in tuple(DEFAULT_CLIENT_MODELS) + tuple(GROK_CLIENT_MODELS):
            self.assertIn(model, pricing)
            self.assertIsNotNone(pricing[model].get("input_per_m"))
            self.assertIsNotNone(pricing[model].get("output_per_m"))

    def test_defaults_are_not_mutated_by_callers(self):
        pricing = core.load_pricing()
        pricing["gpt-5.6-sol"]["input_per_m"] = 999.0
        pricing["gpt-5.6-sol"]["long_context"]["threshold"] = 1
        self.assertEqual(DEFAULT_PRICING["gpt-5.6-sol"]["input_per_m"], 5.0)
        self.assertEqual(
            DEFAULT_PRICING["gpt-5.6-sol"]["long_context"]["threshold"], 272000
        )

    def test_database_override_replaces_whole_entry(self):
        core.save_pricing(
            {**core.default_pricing(), "gpt-5.6-luna": {"input_per_m": 9.9, "output_per_m": 99.0}}
        )
        # 只有覆盖项落库
        self.assertEqual(
            self.store.cfg["pricing"],
            {"gpt-5.6-luna": {"input_per_m": 9.9, "output_per_m": 99.0}},
        )
        pricing = core.load_pricing()
        # 整条替换，不按字段回填默认的 cache/long_context
        self.assertEqual(
            pricing["gpt-5.6-luna"], {"input_per_m": 9.9, "output_per_m": 99.0}
        )
        # 其他模型仍走默认
        self.assertEqual(pricing["gpt-5.6-terra"], DEFAULT_PRICING["gpt-5.6-terra"])

    def test_saving_default_values_clears_override(self):
        core.save_pricing({**core.default_pricing(), "gpt-5.6-luna": {"input_per_m": 9.9}})
        self.assertIn("gpt-5.6-luna", self.store.cfg["pricing"])
        core.save_pricing(core.default_pricing())
        self.assertEqual(self.store.cfg["pricing"], {})
        self.assertEqual(core.load_pricing(), DEFAULT_PRICING)

    def test_custom_model_is_kept_alongside_defaults(self):
        core.save_pricing(
            {**core.default_pricing(), "my-model": {"input_per_m": 1.0, "output_per_m": 2.0}}
        )
        self.assertEqual(list(self.store.cfg["pricing"]), ["my-model"])
        pricing = core.load_pricing()
        self.assertEqual(pricing["my-model"], {"input_per_m": 1.0, "output_per_m": 2.0})
        self.assertEqual(pricing["gpt-5.6-sol"], DEFAULT_PRICING["gpt-5.6-sol"])

    def test_cost_computable_on_fresh_install(self):
        entry = {
            "pool": "openai",
            "client_model": "gpt-5.6-terra",
            "input_tokens": 1000,
            "output_tokens": 1000,
        }
        # 1000 * 2.0 / 1e6 + 1000 * 12.0 / 1e6
        self.assertAlmostEqual(core.compute_cost_usd(entry), 0.014)


if __name__ == "__main__":
    unittest.main()
