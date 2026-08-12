"""Auth / path sanitization tests."""
from __future__ import annotations

import unittest

from fastapi import HTTPException

from sy.auth import _keys_equal
from sy.logbook import _extract_reasoning_effort, _extract_session_id
from sy.proxy import _safe_responses_path


class KeyCompareTests(unittest.TestCase):
    def test_equal(self):
        self.assertTrue(_keys_equal("sk-abc", "sk-abc"))

    def test_empty_never_matches(self):
        self.assertFalse(_keys_equal("", "sk-abc"))
        self.assertFalse(_keys_equal("sk-abc", ""))
        self.assertFalse(_keys_equal("", ""))

    def test_length_mismatch(self):
        self.assertFalse(_keys_equal("short", "much-longer-key"))


class ResponsesPathTests(unittest.TestCase):
    def test_collection(self):
        self.assertEqual(_safe_responses_path("", "POST"), "responses")
        self.assertEqual(_safe_responses_path("/", "GET"), "responses")

    def test_reject_nested_post(self):
        with self.assertRaises(HTTPException) as ctx:
            _safe_responses_path("abc", "POST")
        self.assertEqual(ctx.exception.status_code, 405)

    def test_reject_traversal(self):
        with self.assertRaises(HTTPException) as ctx:
            _safe_responses_path("../secret", "GET")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_accept_safe_id(self):
        self.assertEqual(_safe_responses_path("resp_abc-1", "GET"), "responses/resp_abc-1")


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


if __name__ == "__main__":
    unittest.main()
