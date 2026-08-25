"""PUT /api/pricing cleaning logic tests (no DB writes)."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from sy import core
from sy.api import PricingIn, set_pricing


def _run(body):
    saved = {}
    with mock.patch.object(
        core, "save_pricing", side_effect=lambda p: saved.update(p or {})
    ):
        asyncio.run(set_pricing(PricingIn(pricing=body)))
    return saved


class SetPricingCleaningTests(unittest.TestCase):
    def test_accepts_cache_creation_and_long_context(self):
        body = {
            "gpt-5.6-sol": {
                "input_per_m": 5.0,
                "output_per_m": 30.0,
                "cache_read_per_m": 0.5,
                "cache_creation_per_m": 6.25,
                "long_context": {
                    "threshold": 272000,
                    "input_per_m": 10.0,
                    "output_per_m": 45.0,
                    "cache_read_per_m": 1.0,
                    "cache_creation_per_m": 12.5,
                },
            }
        }
        saved = _run(body)
        sol = saved["gpt-5.6-sol"]
        self.assertEqual(sol["cache_creation_per_m"], 6.25)
        self.assertEqual(sol["long_context"]["threshold"], 272000)
        self.assertEqual(sol["long_context"]["input_per_m"], 10.0)
        self.assertEqual(sol["long_context"]["output_per_m"], 45.0)
        self.assertEqual(sol["long_context"]["cache_creation_per_m"], 12.5)

    def test_drops_non_numeric_and_unknown_keys(self):
        body = {
            "m": {
                "input_per_m": "abc",  # 非数字 -> 丢弃
                "output_per_m": 3.0,
                "cache_read_per_m": 0.1,
                "bogus": 99,  # 未知基础键 -> 丢弃
                "long_context": {
                    "threshold": "x",  # 非数字 -> 丢弃（阈值随后用官方默认 272000）
                    "input_per_m": 6.0,
                    "weird": 7,
                },
            }
        }
        saved = _run(body)
        m = saved["m"]
        self.assertNotIn("input_per_m", m)
        self.assertNotIn("bogus", m)
        self.assertEqual(m["output_per_m"], 3.0)
        lc = m["long_context"]
        self.assertEqual(lc["input_per_m"], 6.0)
        self.assertNotIn("threshold", lc)
        self.assertNotIn("weird", lc)

    def test_long_context_without_any_value_is_dropped(self):
        body = {"m": {"input_per_m": 1.0, "output_per_m": 2.0, "long_context": {}}}
        saved = _run(body)
        self.assertNotIn("long_context", saved["m"])

    def test_empty_model_row_is_skipped(self):
        body = {"m": {}, "n": {"input_per_m": 0.1, "output_per_m": 0.2}}
        saved = _run(body)
        self.assertNotIn("m", saved)
        self.assertIn("n", saved)


if __name__ == "__main__":
    unittest.main()
