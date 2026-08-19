"""首选渠道重试判定测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import asyncio
import httpx

from sy.proxy import (
    _forward_request_headers,
    _is_preferred_retryable,
    _stream_with_preoutput_retry,
    _upstream_response_headers,
)


class _FakeResponse:
    def __init__(self, chunks=(), status_code=200):
        self.chunks = list(chunks)
        self.status_code = status_code
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


class PreferredRetryableTests(unittest.TestCase):
    def test_connection_error_retryable(self):
        self.assertTrue(_is_preferred_retryable(None, False))

    def test_transient_5xx_retryable(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(_is_preferred_retryable(status, False))

    def test_capacity_error_retryable(self):
        self.assertTrue(_is_preferred_retryable(400, True))

    def test_auth_errors_never_retryable(self):
        self.assertFalse(_is_preferred_retryable(401, False))
        self.assertFalse(_is_preferred_retryable(403, True))

    def test_other_4xx_not_retryable(self):
        self.assertFalse(_is_preferred_retryable(400, False))
        self.assertFalse(_is_preferred_retryable(404, False))

    def test_success_not_retryable(self):
        self.assertFalse(_is_preferred_retryable(200, False))


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
                return httpx.Response(200, request=request)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                from sy.proxy import _forward_once

                await _forward_once(
                    client,
                    {
                        "base_url": "https://upstream.example/v1",
                        "api_key": "upstream-key",
                        "model": "openai",
                    },
                    "POST",
                    "responses",
                    b'{"model":"gpt-5.6-sol"}',
                    "application/json",
                    {
                        "authorization": "local-key",
                        "x-codex-turn-metadata": '{"request_kind":"compaction"}',
                        "session_id": "session-1",
                    },
                    "gpt-5.6-sol",
                )
            finally:
                await client.aclose()
            self.assertEqual(seen["headers"]["authorization"], "Bearer upstream-key")
            self.assertEqual(
                seen["headers"]["x-codex-turn-metadata"],
                '{"request_kind":"compaction"}',
            )
            self.assertEqual(seen["headers"]["session_id"], "session-1")

        asyncio.run(run())


class StreamPreoutputRetryTests(unittest.TestCase):
    def test_retries_before_first_output(self):
        async def run():
            opened = []
            initial = _FakeResponse([RuntimeError("断流")])

            async def open_retry():
                response = _FakeResponse(
                    [RuntimeError("断流")]
                    if len(opened) < 1
                    else [b"data: done\\n\\n"]
                )
                opened.append(response)
                return response

            state = {"downstream_started": False, "stream_retries": 0}
            completed = {"value": False}

            async def consume(response):
                async for chunk in response.aiter_bytes():
                    if chunk == b"data: done\\n\\n":
                        completed["value"] = True
                    yield chunk

            with patch("sy.proxy.STREAM_RETRY_BASE_DELAY_SEC", 0):
                output = [
                    chunk
                    async for chunk in _stream_with_preoutput_retry(
                        initial,
                        open_retry,
                        state,
                        consume,
                        lambda: completed["value"],
                        "test stream",
                    )
                ]
            self.assertEqual(output, [b"data: done\\n\\n"])
            self.assertEqual(state["stream_retries"], 2)
            self.assertEqual(len(opened), 2)

        asyncio.run(run())

    def test_does_not_retry_after_first_output(self):
        async def run():
            opened = []
            initial = _FakeResponse([b"partial", RuntimeError("断流")])

            async def open_retry():
                opened.append(True)
                return _FakeResponse([b"should not be used"])

            state = {"downstream_started": False, "stream_retries": 0}
            output = []

            async def consume(response):
                async for chunk in response.aiter_bytes():
                    yield chunk

            with patch("sy.proxy.STREAM_RETRY_BASE_DELAY_SEC", 0):
                with self.assertRaises(RuntimeError):
                    async for chunk in _stream_with_preoutput_retry(
                        initial,
                        open_retry,
                        state,
                        consume,
                        lambda: False,
                        "test stream",
                    ):
                        output.append(chunk)
            self.assertEqual(output, [b"partial"])
            self.assertEqual(opened, [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
