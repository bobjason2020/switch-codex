"""Grok managed TOML block must escape quotes."""
from __future__ import annotations

import unittest

from sy.grok_sync import (
    _managed_block,
    _parse_config,
    _read_config_text,
    _toml_str,
    transform_config_toml,
)


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


class WindowsTomlTests(unittest.TestCase):
    def test_bom_parses(self):
        obj = _parse_config("\ufeff[models]\ndefault = \"grok-4.6\"\n")
        self.assertEqual(obj["models"]["default"], "grok-4.6")

    def test_empty_after_bom(self):
        self.assertEqual(_parse_config("\ufeff"), {})

    def test_utf16_le_file(self):
        import tempfile
        from pathlib import Path

        text = '[models]\ndefault = "grok-4.6"\n'
        with tempfile.TemporaryDirectory() as raw:
            p = Path(raw) / "config.toml"
            p.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
            self.assertEqual(_read_config_text(p), text)
            out, _ = transform_config_toml(_read_config_text(p), "grok-4.6")
            self.assertIn('[model."grok-4.6"]', out)


if __name__ == "__main__":
    unittest.main()
