"""Security behavior for authenticated upstream probes."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from sy import probes


class ProbeRedirectTests(unittest.TestCase):
    def test_upstream_probe_does_not_follow_redirects(self):
        seen = {}

        class Client:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, **kwargs):
                return httpx.Response(302, headers={"location": "https://other.example"})

        target = {
            "id": "upstream-1",
            "name": "test",
            "base_url": "https://upstream.example/v1",
            "api_key": "secret",
            "model": "openai",
        }
        with patch("sy.probes.httpx.AsyncClient", Client):
            result = asyncio.run(probes._probe_upstream(target, record_log=False))

        self.assertFalse(seen["follow_redirects"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 302)

    def test_newapi_ratio_probe_does_not_follow_redirects(self):
        seen = []

        class Client:
            def __init__(self, **kwargs):
                seen.append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                return httpx.Response(302, headers={"location": "https://other.example"})

        with patch("sy.probes.httpx.Client", Client):
            result = probes._fetch_newapi_group_ratio(
                base_url="https://upstream.example",
                group="default",
                access_token="secret",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(seen)
        self.assertTrue(all(not kwargs["follow_redirects"] for kwargs in seen))


if __name__ == "__main__":
    unittest.main()
