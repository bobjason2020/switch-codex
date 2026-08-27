"""TPS calculation tests."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from sy import core, logbook


class TpsTests(unittest.TestCase):
    def test_uses_time_after_first_token(self):
        entry = {"output_tokens": 600, "duration_ms": 5000, "ttft_ms": 2000}
        self.assertEqual(core.tps_output_seconds(entry), 3.0)
        self.assertEqual(core.compute_tps(entry), 200.0)

    def test_missing_ttft_is_not_calculable(self):
        entry = {"output_tokens": 600, "duration_ms": 5000}
        self.assertIsNone(core.tps_output_seconds(entry))
        self.assertIsNone(core.compute_tps(entry))

    def test_non_positive_output_window_is_not_calculable(self):
        for entry in (
            {"output_tokens": 600, "duration_ms": 2000, "ttft_ms": 2000},
            {"output_tokens": 600, "duration_ms": 1000, "ttft_ms": 2000},
        ):
            self.assertIsNone(core.tps_output_seconds(entry))
            self.assertIsNone(core.compute_tps(entry))

    def test_aggregate_uses_weighted_output_window(self):
        items = [
            {"output_tokens": 300, "duration_ms": 5000, "ttft_ms": 2000},
            {"output_tokens": 100, "duration_ms": 4000, "ttft_ms": 1000},
            {"output_tokens": 999, "duration_ms": 1000},
        ]
        with mock.patch.object(core, "load_pricing", return_value={}):
            stats = logbook._aggregate_stats(
                items, datetime(2026, 8, 27, tzinfo=timezone.utc)
            )
        self.assertEqual(stats["avg_tps"], round(400 / 6, 1))


if __name__ == "__main__":
    unittest.main()
