from __future__ import annotations

import unittest
from unittest import mock

from sy import core, migrate_astra


class AstraMigrationTests(unittest.TestCase):
    def test_new_openai_upstream_default_model_map_includes_astra(self):
        self.assertIn(
            {"model": "gpt-6-astra", "actual": "gpt-6-astra"},
            core.default_model_map_for("openai"),
        )

    def test_appends_astra_only_to_openai_upstreams_that_have_a_model_map(self):
        items = [
            {
                "id": "openai-1",
                "name": "openai-one",
                "model": "openai",
                "model_map": [{"model": "gpt-5.6-sol", "actual": "vendor-sol"}],
            },
            {
                "id": "openai-2",
                "name": "openai-two",
                "model": "openai",
                "model_map": [{"model": "gpt-6-astra", "actual": "vendor-astra"}],
            },
            {
                "id": "deepseek-1",
                "name": "deepseek-one",
                "model": "deepseek",
                "model_map": [{"model": "deepseek-v4-pro", "actual": "deepseek-v4-pro"}],
            },
            {"id": "openai-3", "name": "unrestricted", "model": "openai", "model_map": []},
        ]
        saved = []

        with (
            mock.patch.object(migrate_astra.db, "get_setting", return_value=None),
            mock.patch.object(migrate_astra.db, "load_upstreams", return_value=items),
            mock.patch.object(migrate_astra.db, "save_upstreams", side_effect=saved.append),
            mock.patch.object(migrate_astra.db, "load_config_raw", return_value={"active_model": "gpt-6-astra"}),
            mock.patch.object(migrate_astra.db, "save_config_raw"),
            mock.patch.object(migrate_astra.db, "set_setting") as set_setting,
        ):
            result = migrate_astra.migrate()

        self.assertEqual(result["upstreams_updated"], 1)
        self.assertEqual(
            items[0]["model_map"],
            [
                {"model": "gpt-5.6-sol", "actual": "vendor-sol"},
                {"model": "gpt-6-astra", "actual": "gpt-6-astra"},
            ],
        )
        self.assertEqual(items[1]["model_map"][0]["actual"], "vendor-astra")
        self.assertEqual(items[2]["model_map"][0]["model"], "deepseek-v4-pro")
        self.assertEqual(items[3]["model_map"], [])
        self.assertEqual(len(saved), 1)
        self.assertIn(
            mock.call(migrate_astra.MIGRATION_KEY, {"applied": True, "upstreams_updated": 1}),
            set_setting.call_args_list,
        )

    def test_does_not_repeat_after_marker_is_present(self):
        with (
            mock.patch.object(migrate_astra.db, "get_setting", return_value={"applied": True}),
            mock.patch.object(migrate_astra.db, "load_upstreams") as load_upstreams,
            mock.patch.object(migrate_astra.db, "load_config_raw", return_value={"active_model": "gpt-6-astra"}),
            mock.patch.object(migrate_astra.db, "save_config_raw"),
        ):
            result = migrate_astra.migrate()

        self.assertEqual(result, {"migrated": False, "reason": "already-migrated"})
        load_upstreams.assert_not_called()

    def test_project_default_is_astra(self):
        cfg = {"active_model": "openai", "pricing": {"keep": {"input_per_m": 1}}}
        saved = []
        with (
            mock.patch.object(migrate_astra.db, "load_config_raw", return_value=cfg),
            mock.patch.object(migrate_astra.db, "save_config_raw", side_effect=saved.append),
            mock.patch.object(migrate_astra.db, "get_setting", return_value=None),
            mock.patch.object(migrate_astra.db, "set_setting") as set_setting,
        ):
            changed = migrate_astra.ensure_project_default()
        self.assertTrue(changed)
        self.assertEqual(saved[0]["active_model"], "gpt-6-astra")
        set_setting.assert_called_once_with(
            migrate_astra.DEFAULT_KEY,
            {"applied": True, "active_model": "gpt-6-astra"},
        )
