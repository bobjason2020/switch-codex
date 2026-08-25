"""Header 转发与透传测试（纯透传模式下仍有效的部分）。"""
from __future__ import annotations

import unittest

import asyncio
import httpx

from sy.proxy import (
    _forward_once,
    _forward_request_headers,
    _upstream_response_headers,
)


class HeaderForwardingTests(unittest.TestCase):
    def test_preserves_codex_metadata_and_custom_headers(self):
        headers = _forward_request_headers(
            {
                "X-Codex-Turn-Metadata": '{"request_kind":"compaction"}',
                "X-Codex-Installation-Id": "install-1",
                "OpenAI-Beta": "responses=experimental",
                "Accept": "text/event-stream",
                "X-Custom-Trace": "trace-1",
                "Authorization": "sk-client",
            }
        )
        self.assertEqual(headers["x-codex-turn-metadata"], '{"request_kind":"compaction"}')
        self.assertEqual(headers["x-codex-installation-id"], "install-1")
        self.assertEqual(headers["openai-beta"], "responses=experimental")
        self.assertEqual(headers["x-custom-trace"], "trace-1")
        self.assertNotIn("authorization", headers)

    def test_drops_connection_and_generated_framing_headers(self):
        headers = _forward_request_headers(
            {
                "Connection": "keep-alive, X-Internal-Hop",
                "X-Internal-Hop": "drop-me",
                "Host": "local.example",
                "Content-Length": "10",
                "Transfer-Encoding": "chunked",
                "X-Api-Key": "local-secret",
                "X-Request-Id": "req-1",
            }
        )
        self.assertEqual(headers, {"x-request-id": "req-1"})

    def test_preserves_end_to_end_upstream_response_headers(self):
        response = httpx.Response(
            200,
            headers={
                "X-RateLimit-Remaining": "7",
                "X-Request-Id": "upstream-1",
                "Content-Length": "3",
                "Transfer-Encoding": "chunked",
                "Content-Encoding": "gzip",
            },
        )
        headers = _upstream_response_headers(response, {"content-type": "text/event-stream"})
        self.assertEqual(headers["x-ratelimit-remaining"], "7")
        self.assertEqual(headers["x-request-id"], "upstream-1")
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertNotIn("content-length", headers)
        self.assertNotIn("transfer-encoding", headers)
        self.assertNotIn("content-encoding", headers)

    def test_forward_once_replaces_local_auth_and_keeps_metadata(self):
        async def run():
            seen = {}

            def handler(request):
                seen["headers"] = dict(request.headers)
                seen["url"] = str(request.url)
                return httpx.Response(200, request=request)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await _forward_once(
                    client,
                    {
                        "base_url": "https://upstream.example/v1",
                        "api_key": "upstream-key",
                        "model": "openai",
                    },
                    "responses",
                    b'{"model":"gpt-5.6-sol"}',
                    "application/json",
                    {
                        "authorization": "local-key",
                        "x-codex-turn-metadata": '{"request_kind":"compaction"}',
                        "session_id": "session-1",
                    },
                )
            finally:
                await client.aclose()
            self.assertEqual(seen["headers"]["authorization"], "Bearer upstream-key")
            self.assertEqual(
                seen["headers"]["x-codex-turn-metadata"],
                '{"request_kind":"compaction"}',
            )
            self.assertEqual(seen["headers"]["session_id"], "session-1")
            self.assertEqual(seen["url"], "https://upstream.example/v1/responses")

    def test_forward_once_anthropic_uses_x_api_key(self):
        async def run():
            seen = {}

            def handler(request):
                seen["headers"] = dict(request.headers)
                seen["url"] = str(request.url)
                return httpx.Response(200, request=request)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await _forward_once(
                    client,
                    {
                        "base_url": "https://upstream.example",
                        "api_key": "anthropic-key",
                        "model": "openai",
                    },
                    "messages",
                    b'{"model":"claude","messages":[]}',
                    "application/json",
                    {},
                    anthropic=True,
                )
            finally:
                await client.aclose()
            self.assertEqual(seen["headers"]["x-api-key"], "anthropic-key")
            self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
            self.assertEqual(seen["url"], "https://upstream.example/messages")


if __name__ == "__main__":
    unittest.main()
