"""首选渠道重试判定测试。"""
from __future__ import annotations

import unittest

from sy.proxy import _is_preferred_retryable


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


if __name__ == "__main__":
    unittest.main()
