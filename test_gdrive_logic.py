#!/usr/bin/env python3
"""Regression tests for the two README/code mismatches.

Run: python3 test_gdrive_logic.py
"""

import unittest

from gdrive_logic import (
    BAD_REQUEST,
    EXPIRED_SESSION,
    INVALID_PARENT,
    RATE_LIMIT,
    RETRYABLE,
    classify_resumable_http_error,
    retry_delay_seconds,
)


class ExponentialBackoffTests(unittest.TestCase):
    def test_delays_are_exponential_not_linear(self):
        delays = [retry_delay_seconds(i) for i in range(1, 5)]
        self.assertEqual(delays, [5, 10, 20, 40])
        # This is the sequence the old code / README actually produced:
        self.assertNotEqual(delays, [5, 10, 15, 20])

    def test_fifth_attempt_keeps_doubling_until_cap(self):
        self.assertEqual(retry_delay_seconds(5), 80)
        self.assertEqual(retry_delay_seconds(6), 120)  # 160 capped

    def test_rate_limit_is_triple_exponential_then_capped(self):
        self.assertEqual(retry_delay_seconds(1, rate_limited=True), 15)
        self.assertEqual(retry_delay_seconds(2, rate_limited=True), 30)
        self.assertEqual(retry_delay_seconds(3, rate_limited=True), 60)
        self.assertEqual(retry_delay_seconds(4, rate_limited=True), 120)  # 40*3
        self.assertEqual(retry_delay_seconds(5, rate_limited=True), 120)  # 80*3 capped

    def test_old_linear_rate_limit_formula_is_gone(self):
        # Old code: RETRY_BACKOFF * attempt * 3  →  15, 30, 45, 60
        old_linear = [5 * attempt * 3 for attempt in range(1, 5)]
        new_exp = [retry_delay_seconds(i, rate_limited=True) for i in range(1, 5)]
        self.assertNotEqual(new_exp, old_linear)


class NotFoundClassificationTests(unittest.TestCase):
    def test_404_before_any_chunk_is_invalid_parent_folder(self):
        self.assertEqual(classify_resumable_http_error(404, 0), INVALID_PARENT)

    def test_404_after_progress_is_expired_session_not_bad_folder(self):
        # OLD BUG: `if response is None` was True here (Drive only sets
        # `response` on the final chunk), so chunk-2 404 was raised as
        # "Is the --folder-id valid?" instead of restarting the session.
        self.assertEqual(classify_resumable_http_error(404, 1), EXPIRED_SESSION)
        self.assertEqual(classify_resumable_http_error(404, 2), EXPIRED_SESSION)
        self.assertEqual(classify_resumable_http_error(404, 8), EXPIRED_SESSION)

    def test_410_is_always_expired_session_even_on_first_chunk(self):
        self.assertEqual(classify_resumable_http_error(410, 0), EXPIRED_SESSION)
        self.assertEqual(classify_resumable_http_error(410, 3), EXPIRED_SESSION)

    def test_rate_limits(self):
        self.assertEqual(classify_resumable_http_error(403, 0), RATE_LIMIT)
        self.assertEqual(classify_resumable_http_error(429, 4), RATE_LIMIT)

    def test_400_and_other(self):
        self.assertEqual(classify_resumable_http_error(400, 1), BAD_REQUEST)
        self.assertEqual(classify_resumable_http_error(500, 1), RETRYABLE)
        self.assertEqual(classify_resumable_http_error(503, 0), RETRYABLE)

    def test_old_response_is_none_heuristic_would_fail_this_case(self):
        """Document the exact production failure mode.

        File is 25 MiB, chunk size 10 MiB. After chunk 1 succeeds:
          response is still None, chunks_accepted == 1.
        Network blip, Drive returns 404 on chunk 2.
        Old heuristic (response is None) → invalid_parent → abort.
        Correct heuristic (chunks_accepted > 0) → expired_session → retry.
        """
        response_is_none = True  # still true after chunk 1 of 3
        chunks_accepted = 1
        old_decision = "invalid_parent" if (response_is_none and True) else "expired_session"
        new_decision = classify_resumable_http_error(404, chunks_accepted)
        self.assertEqual(old_decision, "invalid_parent")
        self.assertEqual(new_decision, EXPIRED_SESSION)
        self.assertNotEqual(old_decision, new_decision)


if __name__ == "__main__":
    unittest.main()
