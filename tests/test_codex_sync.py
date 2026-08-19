"""Codex provider config convergence tests."""
from __future__ import annotations

import unittest

from sy.codex_sync import _ensure_simple_provider


class SimpleProviderTests(unittest.TestCase):
    def test_existing_legacy_provider_is_upgraded(self):
        text = """model_provider = \"simple\"\n\n[model_providers.simple]\nname = \"simple\"\nbase_url = \"http://old/v1\"\nwire_api = \"responses\"\nrequires_openai_auth = true\n"""
        out, report = _ensure_simple_provider(text)
        self.assertIn('name = "OpenAI"', out)
        self.assertIn('supports_standalone_web_search = true', out)
        self.assertIn('base_url = "http://127.0.0.1:4100/v1"', out)
        self.assertTrue(any("name" in item for item in report))

    def test_new_provider_gets_search_capability(self):
        out, _ = _ensure_simple_provider('model_provider = "simple"\n')
        self.assertIn('[model_providers.simple]', out)
        self.assertIn('name = "OpenAI"', out)
        self.assertIn('supports_standalone_web_search = true', out)


if __name__ == "__main__":
    unittest.main()
