"""Grok managed TOML block must escape quotes."""
from __future__ import annotations

import unittest

from sy.grok_sync import _managed_block, _toml_str


class TomlEscapeTests(unittest.TestCase):
    def test_quotes_and_backslash(self):
        self.assertEqual(_toml_str('a"b\\c'), r'"a\"b\\c"')

    def test_managed_block_parses(self):
        import tomllib

        slug = 'grok-4.6"evil'
        text = "\n".join(_managed_block(slug)) + "\n"
        obj = tomllib.loads(text)
        managed = obj["model"][slug]
        self.assertEqual(managed["model"], slug)
        self.assertTrue(managed["supports_reasoning_effort"])
        self.assertEqual(managed["reasoning_effort"], "xhigh")
        efforts = [e["value"] for e in managed["reasoning_efforts"]]
        self.assertEqual(efforts, ["low", "medium", "high", "xhigh"])


if __name__ == "__main__":
    unittest.main()
