"""Auth / path sanitization tests."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from sy.auth import _keys_equal, login_client_ip
from sy.logbook import _extract_reasoning_effort, _extract_session_context, _extract_session_id
from sy.proxy import _safe_generic_path, _safe_responses_path
from sy.core import upstream_supports_standalone_web_search


class KeyCompareTests(unittest.TestCase):
    def test_equal(self):
        self.assertTrue(_keys_equal("sk-abc", "sk-abc"))

    def test_empty_never_matches(self):
        self.assertFalse(_keys_equal("", "sk-abc"))
        self.assertFalse(_keys_equal("sk-abc", ""))
        self.assertFalse(_keys_equal("", ""))

    def test_length_mismatch(self):
        self.assertFalse(_keys_equal("short", "much-longer-key"))


class LoginClientIpTests(unittest.TestCase):
    def _request(self, headers=None, host="127.0.0.1"):
        return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=host))

    @patch("sy.auth.core.load_public_config")
    def test_ignores_proxy_headers_unless_explicitly_trusted(self, load_public_config):
        load_public_config.return_value = {"trust_proxy_headers": False}
        request = self._request({"cf-connecting-ip": "198.51.100.8"})
        self.assertEqual(login_client_ip(request), "127.0.0.1")

    @patch("sy.auth.core.load_public_config")
    def test_uses_proxy_headers_when_explicitly_trusted(self, load_public_config):
        load_public_config.return_value = {"trust_proxy_headers": True}
        request = self._request(
            {
                "x-forwarded-for": "198.51.100.8, 10.0.0.1",
                "cf-connecting-ip": "198.51.100.9",
            }
        )
        self.assertEqual(login_client_ip(request), "198.51.100.8")


class ResponsesPathTests(unittest.TestCase):
    def test_collection(self):
        self.assertEqual(_safe_responses_path("", "POST"), "responses")
        self.assertEqual(_safe_responses_path("/", "GET"), "responses")

    def test_accept_nested_post_for_compaction_and_other_endpoints(self):
        self.assertEqual(_safe_responses_path("compact", "POST"), "responses/compact")
        self.assertEqual(_safe_responses_path("abc", "POST"), "responses/abc")

    def test_reject_traversal(self):
        with self.assertRaises(HTTPException) as ctx:
            _safe_responses_path("../secret", "GET")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_accept_safe_id(self):
        self.assertEqual(_safe_responses_path("resp_abc-1", "GET"), "responses/resp_abc-1")


class GenericPathTests(unittest.TestCase):
    def test_accepts_openai_style_endpoint(self):
        self.assertEqual(_safe_generic_path("models"), "models")
        self.assertEqual(_safe_generic_path("files/file_123"), "files/file_123")

    def test_rejects_traversal_and_empty_paths(self):
        for path in ("", "../secret", "files\\secret"):
            with self.assertRaises(HTTPException) as ctx:
                _safe_generic_path(path)
            self.assertEqual(ctx.exception.status_code, 400)


class StandaloneSearchCapabilityTests(unittest.TestCase):
    def test_openai_responses_upstream_is_supported_by_default(self):
        self.assertTrue(upstream_supports_standalone_web_search({"model": "openai"}))

    def test_adapters_are_excluded(self):
        self.assertFalse(upstream_supports_standalone_web_search({"model": "openai", "chat_completions": True}))
        self.assertFalse(upstream_supports_standalone_web_search({"model": "openai", "anthropic_messages": True}))

    def test_explicit_opt_out(self):
        self.assertFalse(upstream_supports_standalone_web_search({"model": "openai", "standalone_web_search": False}))

    def test_non_openai_pool_is_excluded(self):
        self.assertFalse(upstream_supports_standalone_web_search({"model": "grok"}))


class GrokSessionAndEffortTests(unittest.TestCase):
    def test_prompt_cache_key_is_session(self):
        sid = "019ff74d-dee1-7091-8be3-7bfe60c091c5"
        self.assertEqual(
            _extract_session_id({}, {"prompt_cache_key": sid, "reasoning": {"summary": "concise"}}),
            sid,
        )

    def test_grok_session_header(self):
        self.assertEqual(
            _extract_session_id({"x-grok-session-id": "sess-1"}, {}),
            "sess-1",
        )

    def test_reasoning_effort_from_object(self):
        self.assertEqual(
            _extract_reasoning_effort({"reasoning": {"effort": "xhigh", "summary": "concise"}}),
            "xhigh",
        )

    def test_reasoning_summary_only_is_not_effort(self):
        self.assertIsNone(_extract_reasoning_effort({"reasoning": {"summary": "concise"}}))

    def test_codex_subagent_context_keeps_root_and_child(self):
        import json

        context = _extract_session_context(
            {
                "x-codex-turn-metadata": json.dumps(
                    {
                        "session_id": "root",
                        "thread_id": "child",
                        "parent_thread_id": "root",
                        "root_thread_id": "root",
                        "thread_source": "subagent",
                    }
                )
            },
            {},
        )
        self.assertEqual(context["session_id"], "root")
        self.assertEqual(context["thread_id"], "child")
        self.assertEqual(context["cache_session_id"], "child")
        self.assertEqual(context["session_path"], ["root", "child"])

    def test_codex_nested_subagent_context_preserves_chain(self):
        import json

        context = _extract_session_context(
            {
                "x-codex-turn-metadata": json.dumps(
                    {
                        "session_id": "root",
                        "thread_id": "leaf",
                        "parent_thread_id": "middle",
                        "root_thread_id": "root",
                        "thread_source": "subagent",
                    }
                )
            },
            {},
        )
        self.assertEqual(context["session_path"], ["root", "middle", "leaf"])
        self.assertEqual(context["cache_session_id"], "leaf")


if __name__ == "__main__":
    unittest.main()
