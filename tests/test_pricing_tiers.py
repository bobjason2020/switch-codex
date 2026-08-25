"""Tiered (long-context) billing tests."""
from __future__ import annotations

import unittest
from unittest import mock

from sy import core

SOL = {
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

LUNA = {
    "input_per_m": 0.2,
    "output_per_m": 1.2,
    "cache_read_per_m": 0.025,
}

CACHE_READ = 50000
CACHE_CREATE = 50000
OUTPUT = 1000


def _make_entry(input_tokens, client_model="gpt-5.6-sol"):
    return {
        "pool": "gpt",
        "client_model": client_model,
        "input_tokens": input_tokens,
        "output_tokens": OUTPUT,
        "cache_read_tokens": CACHE_READ,
        "cache_creation_tokens": CACHE_CREATE,
    }


def _expected(input_tokens, prices):
    uncached = max(input_tokens - CACHE_READ - CACHE_CREATE, 0)
    return (
        uncached * prices["input_per_m"]
        + CACHE_READ * prices["cache_read_per_m"]
        + CACHE_CREATE * prices["cache_creation_per_m"]
        + OUTPUT * prices["output_per_m"]
    ) / 1_000_000


class LongContextTierTests(unittest.TestCase):
    def setUp(self):
        self.pricing = {"gpt-5.6-sol": dict(SOL), "gpt-5.6-luna": dict(LUNA)}
        patcher = mock.patch.object(core, "load_pricing", return_value=self.pricing)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_below_threshold_uses_base_prices(self):
        e = _make_entry(200000)
        self.assertAlmostEqual(
            core.compute_cost_usd(e),
            _expected(200000, SOL),
            places=6,
        )

    def test_exactly_threshold_uses_base_prices(self):
        e = _make_entry(272000)
        self.assertAlmostEqual(
            core.compute_cost_usd(e),
            _expected(272000, SOL),
            places=6,
        )

    def test_above_threshold_uses_long_context_prices(self):
        e = _make_entry(272001)
        self.assertAlmostEqual(
            core.compute_cost_usd(e),
            _expected(272001, SOL["long_context"]),
            places=6,
        )

    def test_model_without_long_context_stays_flat(self):
        e = _make_entry(300000, client_model="gpt-5.6-luna")
        # luna 无 cache_creation 价 -> 回退到输入价
        prices = {
            "input_per_m": LUNA["input_per_m"],
            "output_per_m": LUNA["output_per_m"],
            "cache_read_per_m": LUNA["cache_read_per_m"],
            "cache_creation_per_m": LUNA["input_per_m"],
        }
        self.assertAlmostEqual(core.compute_cost_usd(e), _expected(300000, prices), places=6)

    def test_long_context_missing_key_falls_back_to_base(self):
        lc = dict(SOL["long_context"])
        del lc["cache_creation_per_m"]
        self.pricing["gpt-5.6-sol"]["long_context"] = lc
        e = _make_entry(300000)
        prices = dict(lc)
        prices["cache_creation_per_m"] = SOL["cache_creation_per_m"]
        self.assertAlmostEqual(core.compute_cost_usd(e), _expected(300000, prices), places=6)

    def test_breakdown_tier_marking(self):
        b = core.cost_breakdown(_make_entry(300000))
        self.assertEqual(b["tier"], "long_context")
        self.assertEqual(b["long_context_threshold"], 272000)
        self.assertAlmostEqual(b["total"], _expected(300000, SOL["long_context"]), places=6)

        b2 = core.cost_breakdown(_make_entry(100000))
        self.assertEqual(b2["tier"], "standard")
        self.assertEqual(b2["long_context_threshold"], 272000)

    def test_breakdown_rows_use_tier_unit_prices(self):
        b = core.cost_breakdown(_make_entry(300000))
        by_label = {r["label"]: r["unit_price"] for r in b["rows"]}
        self.assertEqual(by_label["输入"], 10.0)
        self.assertEqual(by_label["缓存读取"], 1.0)
        self.assertEqual(by_label["缓存写入"], 12.5)
        self.assertEqual(by_label["输出"], 45.0)

    def test_pricing_for_flat_model_has_no_long_context(self):
        pr = core.pricing_for("gpt", "gpt-5.6-luna")
        self.assertIsNone(pr["long_context"])

    def test_threshold_defaults_to_272000(self):
        lc = dict(SOL["long_context"])
        del lc["threshold"]
        self.pricing["gpt-5.6-sol"]["long_context"] = lc
        pr = core.pricing_for("gpt", "gpt-5.6-sol")
        self.assertEqual(pr["long_context"]["threshold"], 272000)
        self.assertTrue(core._is_long_context(pr, 300000))
        self.assertFalse(core._is_long_context(pr, 200000))

    def test_long_context_without_prices_is_ignored(self):
        self.pricing["gpt-5.6-sol"]["long_context"] = {"threshold": 100000}
        pr = core.pricing_for("gpt", "gpt-5.6-sol")
        self.assertIsNone(pr["long_context"])
        self.assertFalse(core._is_long_context(pr, 300000))

    def test_missing_input_tokens_uses_base_tier(self):
        e = _make_entry(300000)
        e["input_tokens"] = None
        self.assertAlmostEqual(core.compute_cost_usd(e), _expected(0, SOL), places=6)


if __name__ == "__main__":
    unittest.main()
