"""Pure retry / error-classification helpers. No Google imports.

These exist so the two contracts the README promises can be unit-tested:

1. Backoff is exponential (5, 10, 20, 40, …), not linear (5, 10, 15, 20).
2. A 404 is a bad folder only if ZERO chunks were accepted in the current
   resumable session. `response is None` is the wrong signal — Drive leaves
   `response` as None until the *last* chunk, so a mid-upload 404 would be
   misdiagnosed as an invalid --folder-id.
"""

from __future__ import annotations

RETRY_BACKOFF_BASE = 5
RETRY_BACKOFF_CAP = 120
MAX_RETRIES = 5

INVALID_PARENT = "invalid_parent"
EXPIRED_SESSION = "expired_session"
RATE_LIMIT = "rate_limit"
BAD_REQUEST = "bad_request"
RETRYABLE = "retryable"


def retry_delay_seconds(attempt: int, *, rate_limited: bool = False,
                        base: int = RETRY_BACKOFF_BASE,
                        cap: int = RETRY_BACKOFF_CAP) -> int:
    """Seconds to sleep after a failed attempt (1-based).

    Exponential: base * 2^(attempt-1)  →  5, 10, 20, 40, 80
    Rate-limit:  that value × 3, then capped  →  15, 30, 60, 120, 120
    """
    if attempt < 1:
        attempt = 1
    delay = base * (2 ** (attempt - 1))
    if rate_limited:
        delay *= 3
    return min(delay, cap)


def classify_resumable_http_error(status_code: int, chunks_accepted: int) -> str:
    """Classify a Drive resumable-upload HTTP error.

    `chunks_accepted` is the number of successful `next_chunk()` calls in the
    *current* upload session (reset to 0 when the session is recreated).
    It is NOT "whether `response` is None" — that stays None until completion.
    """
    if status_code in (403, 429):
        return RATE_LIMIT
    if status_code == 404 and chunks_accepted == 0:
        return INVALID_PARENT
    if status_code in (404, 410):
        return EXPIRED_SESSION
    if status_code == 400:
        return BAD_REQUEST
    return RETRYABLE
