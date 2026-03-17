"""Retry-with-backoff utility for transient network failures.

Provides a generic retry wrapper used by send_telegram() and run_gh()
to handle transient errors (connection resets, DNS failures, timeouts)
instead of failing silently on the first attempt.
"""

import re
import sys
import time
from typing import Callable, Optional, Sequence, Tuple, Type


DEFAULT_BACKOFF = (1, 2, 4)
DEFAULT_MAX_ATTEMPTS = 3


def retry_with_backoff(
    fn: Callable,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: Sequence[float] = DEFAULT_BACKOFF,
    retryable: Tuple[Type[BaseException], ...] = (),
    is_transient: Optional[Callable[[BaseException], bool]] = None,
    get_retry_delay: Optional[Callable[[BaseException], Optional[float]]] = None,
    label: str = "",
):
    """Call fn() with exponential backoff on transient failures.

    Args:
        fn: Zero-argument callable to invoke.
        max_attempts: Maximum number of attempts (default 3).
        backoff: Sleep durations between retries (seconds).
        retryable: Exception types that trigger a retry.
        is_transient: Optional predicate for finer filtering of retryable
            exceptions. If provided and returns False, the exception is
            re-raised immediately without retry.
        get_retry_delay: Optional callable that extracts a specific delay
            (in seconds) from an exception. When provided and returns a
            non-None value, that delay overrides the default backoff schedule.
        label: Label for log messages.

    Returns:
        The return value of fn().

    Raises:
        The last exception if all attempts fail.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except retryable as exc:
            if is_transient and not is_transient(exc):
                raise
            last_exc = exc
            if attempt < max_attempts - 1:
                # Use explicit delay from Retry-After if available, else backoff
                delay: float
                if get_retry_delay is not None:
                    explicit = get_retry_delay(exc)
                    delay = explicit if explicit is not None else backoff[min(attempt, len(backoff) - 1)]
                else:
                    delay = backoff[min(attempt, len(backoff) - 1)]
                print(
                    f"[retry] {label or 'call'} failed "
                    f"(attempt {attempt + 1}/{max_attempts}): {exc} "
                    f"— retrying in {delay}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
    raise last_exc


# -- Transient error detection ------------------------------------------------

# Keywords in stderr/error messages that suggest transient network issues.
_TRANSIENT_KEYWORDS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "name resolution",
    "dns",
    "temporary failure",
    "timed out",
    "timeout",
    "eof",
    "broken pipe",
    "ssl",
    "503",
    "502",
    "429",
)

# Patterns that indicate a GitHub secondary rate limit (abuse detection).
# These are non-idempotent-safe: retrying a write could create duplicates.
_SECONDARY_RATE_LIMIT_KEYWORDS = (
    "secondary rate limit",
    "abuse detection",
    "abuse",
)


def is_gh_transient(exc: BaseException) -> bool:
    """Return True if a RuntimeError from run_gh looks like a transient failure."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _TRANSIENT_KEYWORDS)


def is_gh_secondary_rate_limit(exc: BaseException) -> bool:
    """Return True if the error is a GitHub secondary (abuse) rate limit."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _SECONDARY_RATE_LIMIT_KEYWORDS)


def parse_retry_after(exc: BaseException) -> Optional[float]:
    """Extract a ``Retry-After`` delay (seconds) from a gh CLI error message.

    GitHub's ``gh`` CLI surfaces the ``Retry-After`` header value in its
    stderr output when a primary rate limit is hit.  This helper parses
    that value so the retry loop can honour it instead of using the default
    backoff schedule.

    Args:
        exc: Exception whose string representation may contain a
            ``Retry-After: <seconds>`` fragment.

    Returns:
        Delay in seconds as a float, or ``None`` if not found / not parseable.
    """
    msg = str(exc)
    match = re.search(r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)", msg, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None
