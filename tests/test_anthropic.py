"""Anthropic ↔ Responses conversion tests."""
from __future__ import annotations

import unittest

from sy import anthropic


class StopReasonTests(unittest.TestCase):
    def test_infer_tool_use_from_function_call(self):
        resp = {
            "status": "completed",
            "output": [{"type": "function_call", "name": "x", "arguments": "{}"}],
        }
        self.assertEqual(anthropic.infer_stop_reason(resp), "tool_use")

    def test_infer_max_tokens_from_incomplete(self):
        resp = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        self.assertEqual(anthropic.infer_stop_reason(resp), "max_tokens")

    def test_infer_end_turn_default(self):
        self.assertEqual(anthropic.infer_stop_reason({"status": "completed"}), "end_turn")


class ClassifierTests(unittest.TestCase):
    def test_classifier_shape(self):
        body = {
            "stream": False,
            "max_tokens": 80,
            "messages": [{"role": "user", "content": "hi"}],
        }
        self.assertTrue(anthropic.looks_like_classifier(body))

    def test_not_classifier_when_streaming(self):
        body = {
            "stream": True,
            "max_tokens": 80,
            "messages": [{"role": "user", "content": "hi"}],
        }
        self.assertFalse(anthropic.looks_like_classifier(body))

    def test_not_classifier_with_tools(self):
        body = {
            "max_tokens": 80,
            "tools": [{"name": "x"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        self.assertFalse(anthropic.looks_like_classifier(body))


class ContentWrapTests(unittest.TestCase):
    def test_text_blocks_wrapped_as_message(self):
        items = anthropic._content_blocks_to_input(
            "user",
            [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["role"], "user")
        self.assertEqual(len(items[0]["content"]), 2)

    def test_tool_use_is_top_level(self):
        items = anthropic._content_blocks_to_input(
            "assistant",
            [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "c1", "name": "fn", "input": {"a": 1}},
            ],
        )
        self.assertEqual(items[0]["role"], "assistant")
        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[1]["name"], "fn")


class ErrorEnvelopeTests(unittest.TestCase):
    def test_rate_limit_type(self):
        err = anthropic.anthropic_error_response(429, "slow down")
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"]["type"], "rate_limit_error")
        self.assertEqual(err["error"]["message"], "slow down")

    def test_invalid_request_type(self):
        err = anthropic.anthropic_error_response(400, "bad")
        self.assertEqual(err["error"]["type"], "invalid_request_error")


class UsageFloatTests(unittest.TestCase):
    def test_float_usage_accepted(self):
        inp, out = anthropic._parse_usage({"input_tokens": 1.0, "output_tokens": 2.9})
        self.assertEqual((inp, out), (1, 2))

    def test_bool_not_treated_as_int(self):
        inp, out = anthropic._parse_usage({"input_tokens": True, "output_tokens": False})
        self.assertEqual((inp, out), (0, 0))


if __name__ == "__main__":
    unittest.main()
