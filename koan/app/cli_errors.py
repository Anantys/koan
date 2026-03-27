"""CLI error classification for provider resilience.

Centralizes error classification from CLI subprocess output into
categories that drive retry/abort decisions. Delegates quota detection
to the existing ``quota_handler`` module to avoid duplication.

Categories:
- RETRYABLE: Transient server/network errors worth retrying
- TERMINAL: Permanent errors (auth, invalid request) — don't retry
- QUOTA: Quota/rate-limit exhaustion — handled by pause_manager
- UNKNOWN: Unrecognized errors — treated as terminal (don't retry
  what we don't understand)
"""

import re
from enum import Enum
from typing import Optional


class ErrorCategory(Enum):
    """Classification of CLI error outcomes."""
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    QUOTA = "quota"
    AUTH = "auth"
    UNKNOWN = "unknown"


# Patterns indicating transient server/network errors (worth retrying).
# Matched case-insensitively against combined stdout+stderr.
_RETRYABLE_PATTERNS = [
    r"HTTP\s+5\d\d",
    r"502\s+Bad\s+Gateway",
    r"503\s+Service\s+Unavailable",
    r"500\s+Internal\s+Server\s+Error",
    r"connection\s+reset",
    r"connection\s+refused",
    r"ECONNREFUSED",
    r"ETIMEDOUT",
    r"ECONNRESET",
    r"timeout",
    r"timed?\s*out",
    r"temporarily\s+unavailable",
    r"internal\s+server\s+error",
    r"bad\s+gateway",
    r"service\s+unavailable",
    r"network\s+is\s+unreachable",
    r"dns\s+resolution",
    r"name\s+resolution",
]

# Patterns indicating permanent errors (don't retry).
_TERMINAL_PATTERNS = [
    r"authentication\s+(failed|required|error)",
    r"unauthorized",
    r"invalid[\s._-]*api[\s._-]*key",
    r"permission\s+denied",
    r"context[\s._-]*window[\s._-]*exceeded",
    r"invalid\s+request",
    r"400\s+Bad\s+Request",
    r"401\s+Unauthorized",
    r"403\s+Forbidden",
]

# Patterns indicating Claude is logged out / OAuth expired — needs human
# intervention (re-login).  Checked before generic TERMINAL so we can
# distinguish "auth expired, requeue the mission" from "bad API key, give up".
_AUTH_PATTERNS = [
    r"please\s+run\s+/login",
    r"oauth\s+token\s+has\s+expired",
    r"please\s+obtain\s+a\s+new\s+token",
    r"refresh\s+your\s+existing\s+token",
    r"not\s+authenticated",
    r"please\s+log\s+in",
]

_AUTH_RE = re.compile("|".join(_AUTH_PATTERNS), re.IGNORECASE)
_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)
_TERMINAL_RE = re.compile("|".join(_TERMINAL_PATTERNS), re.IGNORECASE)


def classify_cli_error(
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> ErrorCategory:
    """Classify a CLI error based on exit code and output text.

    Args:
        exit_code: Subprocess exit code (0 = success, not classified).
        stdout: Captured stdout from the CLI process.
        stderr: Captured stderr from the CLI process.

    Returns:
        ErrorCategory indicating how the caller should handle the error.
        Returns ``UNKNOWN`` for exit_code == 0 only if called explicitly
        (callers should not classify successful runs).
    """
    if exit_code == 0:
        return ErrorCategory.UNKNOWN

    combined = f"{stdout}\n{stderr}"

    # Check quota first — quota_handler is the authority for quota detection.
    # A 429 could be rate-limiting or quota exhaustion; defer to the
    # specialized detector which has provider-specific patterns.
    from app.quota_handler import detect_quota_exhaustion

    if detect_quota_exhaustion(combined):
        return ErrorCategory.QUOTA

    # Auth errors — Claude is logged out, needs human intervention.
    # Checked before generic TERMINAL so "401 + OAuth expired" routes here
    # instead of falling into the generic "unauthorized" terminal bucket.
    if _AUTH_RE.search(combined):
        return ErrorCategory.AUTH

    # Terminal errors — don't retry
    if _TERMINAL_RE.search(combined):
        return ErrorCategory.TERMINAL

    # Retryable errors — worth retrying with backoff
    if _RETRYABLE_RE.search(combined):
        return ErrorCategory.RETRYABLE

    return ErrorCategory.UNKNOWN
