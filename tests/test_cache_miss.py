"""Cache-miss detection, filtering, and stats tests."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from sy import core, db
from sy.const import CACHE_MISS_MAX_GAP_SEC

# import 时会触发掉缓存字段一次性回填,测试环境不碰真实数据库。
with mock.patch.object(
    db, "has_request_logs_missing_field", return_value=False
), mock.patch.object(db, "backup_db"), mock.patch.object(db, "replace_request_logs"):
    from sy import logbook

SOL = {
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


def ent(sid, ts, input_tokens, cache_read, status=200, output=100, cached=None, upstream=""):
    e = {
        "ts": ts,
        "session_id": sid,
        "pool": "gpt",
        "client_model": "gpt-5.6-sol",
        "upstream": upstream,
        "status": status,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "output_tokens": output,
        "duration_ms": 100,
    }
    if cached is not None:
        e["cached_tokens"] = cached
    return e


class AnnotateCacheMissTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(core, "load_pricing", return_value=SOL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_session_cache_drop_is_marked(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertTrue(items[1]["is_cache_miss"])
        self.assertEqual(items[1]["cache_miss_tokens"], 290304)  # 293120 - 2816
        # 长文本档差额 10 - 1.0 = 9.0
        self.assertAlmostEqual(items[1]["cache_miss_extra_usd"], 290304 * 9.0 / 1e6, places=6)

    def test_standard_tier_uses_base_price_diff(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 200000, 180000),
            ent("S", "2026-08-25T10:01:00+08:00", 200500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertTrue(items[1]["is_cache_miss"])
        self.assertEqual(items[1]["cache_miss_tokens"], 177184)  # 180000 - 2816
        # 基础档差额 5 - 0.5 = 4.5
        self.assertAlmostEqual(items[1]["cache_miss_extra_usd"], 177184 * 4.5 / 1e6, places=6)

    def test_input_shrank_not_marked(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:01:00+08:00", 200000, 195000),
        ]
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[1]["is_cache_miss"])

    def test_cache_grew_not_marked(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:01:00+08:00", 320000, 300000),
        ]
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[1]["is_cache_miss"])

    def test_different_session_or_no_session_not_marked(self):
        items = [
            ent("A", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("B", "2026-08-25T10:01:00+08:00", 300500, 2816),
            ent(None, "2026-08-25T10:02:00+08:00", 300500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertFalse(any(e["is_cache_miss"] for e in items))

    def test_null_usage_skipped_does_not_break_chain(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:01:00+08:00", None, None),
            ent("S", "2026-08-25T10:02:00+08:00", 300500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertTrue(items[2]["is_cache_miss"])  # 与 items[0] 对比

    def test_cross_upstream_same_session_not_compared(self):
        # 缓存按上游隔离:同会话但上游不同,前驱不可比
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 37334, 3840, upstream="lucen006"),
            ent("S", "2026-08-25T10:01:00+08:00", 38718, 2816, upstream="jucode"),
        ]
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[1]["is_cache_miss"])

    def test_gap_over_max_not_marked(self):
        # 相邻间隔超过 60 分钟 → 视为缓存自然过期,不判掉缓存
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816),
        ]
        items[0]["ts"] = "2026-08-25T10:00:00+08:00"
        items[1]["ts"] = "2026-08-25T12:00:00+08:00"
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[1]["is_cache_miss"])

    def test_gap_within_max_marked(self):
        # 间隔在上限内(59 分钟)仍判掉缓存
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:59:00+08:00", 300500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertTrue(items[1]["is_cache_miss"])

    def test_gap_exactly_max_marked(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T11:00:00+08:00", 300500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertTrue(items[1]["is_cache_miss"])

    def test_gap_with_null_usage_still_checked(self):
        # 无 usage 记录跳过不打断链条,间隔以上一条带 usage 的为准
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:30:00+08:00", None, None),
            ent("S", "2026-08-25T12:30:00+08:00", 300500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[2]["is_cache_miss"])

    def test_failed_request_not_marked(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120),
            ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816, status=500),
        ]
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[1]["is_cache_miss"])

    def test_defaults_attached_to_all(self):
        items = [ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120)]
        logbook._annotate_cache_misses(items)
        self.assertFalse(items[0]["is_cache_miss"])
        self.assertEqual(items[0]["cache_miss_tokens"], 0)
        self.assertEqual(items[0]["cache_miss_extra_usd"], 0.0)


class FilterCacheMissTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(core, "load_pricing", return_value=SOL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cache_miss_filter_is_success_subset(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 200000, 180000),
            ent("S", "2026-08-25T10:01:00+08:00", 200500, 2816),  # 掉缓存(成功)
            ent("S", "2026-08-25T10:02:00+08:00", 200600, 2816, status=500),  # 失败
            ent("T", "2026-08-25T10:03:00+08:00", 100000, 50000),  # 正常成功
        ]
        logbook._annotate_cache_misses(items)
        miss = logbook._filter_traffic_logs(items, status="cache_miss")
        self.assertEqual([e["ts"] for e in miss], ["2026-08-25T10:01:00+08:00"])
        ok = logbook._filter_traffic_logs(items, status="success")
        self.assertEqual(len(ok), 3)  # 10:00 / 10:01 / 10:03
        self.assertIn(miss[0], ok)  # 掉缓存 ⊆ 成功

    def test_other_filters_still_work(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 200000, 180000),
            ent("S", "2026-08-25T10:01:00+08:00", 200500, 2816),
        ]
        logbook._annotate_cache_misses(items)
        self.assertEqual(len(logbook._filter_traffic_logs(items, pool="gpt")), 2)
        self.assertEqual(len(logbook._filter_traffic_logs(items, pool="other")), 0)
        self.assertEqual(len(logbook._filter_traffic_logs(items, status="error")), 0)
        self.assertEqual(len(logbook._filter_traffic_logs(items, model="gpt-5.6-sol")), 2)
        self.assertEqual(len(logbook._filter_traffic_logs(items, model="未知模型")), 0)
        self.assertEqual(len(logbook._filter_traffic_logs(items, q="sol")), 2)
        self.assertEqual(len(logbook._filter_traffic_logs(items, q="zzz")), 0)


class AggregateStatsCacheMissTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(core, "load_pricing", return_value=SOL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stats_accumulate_cache_miss(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 200000, 180000),
            ent("S", "2026-08-25T10:01:00+08:00", 200500, 2816),
            ent("T", "2026-08-25T10:02:00+08:00", 100000, 90000),
        ]
        logbook._annotate_cache_misses(items)
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        stats = logbook._aggregate_stats(items, now)
        self.assertEqual(stats["cache_miss_count"], 1)
        self.assertEqual(stats["cache_miss_rate"], round(1 / 3, 4))
        self.assertEqual(stats["cache_miss_tokens"], 177184)
        self.assertAlmostEqual(stats["cache_miss_extra_usd"], round(177184 * 4.5 / 1e6, 6), places=6)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["success"], 3)

    def test_rate_excludes_no_session_and_no_usage(self):
        items = [
            ent("S", "2026-08-25T10:00:00+08:00", 200000, 180000),
            ent("S", "2026-08-25T10:01:00+08:00", 200500, 2816),  # 掉缓存
            ent(None, "2026-08-25T10:02:00+08:00", 200000, 180000),  # 无 session
            ent("T", "2026-08-25T10:03:00+08:00", None, None),  # 无 usage
        ]
        logbook._annotate_cache_misses(items)
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        stats = logbook._aggregate_stats(items, now)
        # 分母 = 有 session 且有 usage 的请求(2 条),掉缓存 1 条
        self.assertEqual(stats["cache_miss_base"], 2)
        self.assertEqual(stats["cache_miss_count"], 1)
        self.assertEqual(stats["cache_miss_rate"], 0.5)


class AttachCacheMissFieldsTests(unittest.TestCase):
    """写路径增量判定(_attach_cache_miss_fields):进程内维护前驱,启动后从库补种。"""

    def setUp(self):
        patcher = mock.patch.object(core, "load_pricing", return_value=SOL)
        patcher.start()
        self.addCleanup(patcher.stop)
        logbook._last_usage_state.clear()

    def test_drop_against_seeded_prev_is_marked(self):
        prev = ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120, upstream="jucode")
        with mock.patch.object(db, "load_last_usage_entry", return_value=prev):
            e = ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816, upstream="jucode")
            logbook._attach_cache_miss_fields(e)
        self.assertTrue(e["is_cache_miss"])
        self.assertEqual(e["cache_miss_tokens"], 290304)
        self.assertAlmostEqual(e["cache_miss_extra_usd"], 290304 * 9.0 / 1e6, places=6)

    def test_gap_over_max_not_marked(self):
        prev = ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120, upstream="jucode")
        with mock.patch.object(db, "load_last_usage_entry", return_value=prev):
            e = ent("S", "2026-08-25T12:00:00+08:00", 300500, 2816, upstream="jucode")
            logbook._attach_cache_miss_fields(e)
        self.assertFalse(e["is_cache_miss"])

    def test_no_prev_not_marked(self):
        with mock.patch.object(db, "load_last_usage_entry", return_value=None):
            e = ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816, upstream="jucode")
            logbook._attach_cache_miss_fields(e)
        self.assertFalse(e["is_cache_miss"])

    def test_no_session_or_usage_not_marked(self):
        with mock.patch.object(db, "load_last_usage_entry", return_value=None):
            e = ent(None, "2026-08-25T10:01:00+08:00", 300500, 2816)
            logbook._attach_cache_miss_fields(e)
            self.assertFalse(e["is_cache_miss"])
            e = ent("S", "2026-08-25T10:01:00+08:00", None, None)
            logbook._attach_cache_miss_fields(e)
            self.assertFalse(e["is_cache_miss"])

    def test_state_advances_without_reloading_db(self):
        prev = ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120, upstream="jucode")
        with mock.patch.object(db, "load_last_usage_entry", return_value=prev) as loader:
            e1 = ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816, upstream="jucode")
            logbook._attach_cache_miss_fields(e1)
            self.assertTrue(e1["is_cache_miss"])
            e2 = ent("S", "2026-08-25T10:02:00+08:00", 300600, 295000, upstream="jucode")
            logbook._attach_cache_miss_fields(e2)
            self.assertFalse(e2["is_cache_miss"])  # 与 e1 相比正常续上
            self.assertEqual(loader.call_count, 1)  # 只补种一次,后续用进程内状态

    def test_expired_state_reseeds_from_database(self):
        prev = ent("S", "2026-08-25T10:00:00+08:00", 300000, 293120, upstream="jucode")
        with mock.patch.object(db, "load_last_usage_entry", return_value=prev) as loader, mock.patch.object(
            logbook.time, "monotonic", side_effect=[10.0, 10.0 + logbook._CACHE_MISS_STATE_TTL_SEC + 1]
        ):
            e1 = ent("S", "2026-08-25T10:01:00+08:00", 300500, 2816, upstream="jucode")
            logbook._attach_cache_miss_fields(e1)
            e2 = ent("S", "2026-08-25T10:02:00+08:00", 300600, 2816, upstream="jucode")
            logbook._attach_cache_miss_fields(e2)
        self.assertEqual(loader.call_count, 2)

    def test_state_is_bounded(self):
        old_limit = logbook._CACHE_MISS_STATE_MAX_ENTRIES
        logbook._CACHE_MISS_STATE_MAX_ENTRIES = 2
        self.addCleanup(setattr, logbook, "_CACHE_MISS_STATE_MAX_ENTRIES", old_limit)
        with mock.patch.object(db, "load_last_usage_entry", return_value=None):
            for sid in ("one", "two", "three"):
                logbook._attach_cache_miss_fields(
                    ent(sid, "2026-08-25T10:01:00+08:00", 300500, 2816, upstream="jucode")
                )
        self.assertEqual(len(logbook._last_usage_state), 2)
        self.assertNotIn(("one", "jucode"), logbook._last_usage_state)


class LogMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.old_last_error_prune = logbook._last_error_prune
        logbook._last_error_prune = 0.0
        self.addCleanup(setattr, logbook, "_last_error_prune", self.old_last_error_prune)

    def test_error_prune_is_rate_limited(self):
        with mock.patch.object(db, "insert_error_log"), mock.patch.object(
            db, "prune_error_logs"
        ) as prune, mock.patch.object(logbook.time, "time", side_effect=[4000.0, 4001.0]):
            logbook._record_error_log()
            logbook._record_error_log()
        prune.assert_called_once()

    def test_cache_backfill_checks_all_persisted_fields(self):
        with mock.patch.object(
            db, "has_request_logs_missing_field", side_effect=[False, True]
        ), mock.patch.object(db, "load_request_log_rows_after", return_value=[]):
            self.assertEqual(logbook._backfill_cache_miss_fields(), 0)


if __name__ == "__main__":
    unittest.main()
