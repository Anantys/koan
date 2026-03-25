"""Tests for app.retry — retry-with-backoff utility."""

import subprocess
from unittest.mock import patch

import pytest

from app.retry import (
    retry_with_backoff,
    is_gh_transient,
    is_gh_secondary_rate_limit,
    parse_retry_after,
)


class TestRetryWithBackoff:
    """Core retry_with_backoff() behaviour."""

    @patch("app.retry.time.sleep")
    def test_succeeds_first_try(self, mock_sleep):
        result = retry_with_backoff(
            lambda: "ok",
            retryable=(RuntimeError,),
        )
        assert result == "ok"
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    def test_retries_on_retryable_exception(self, mock_sleep):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return "recovered"

        result = retry_with_backoff(
            flaky,
            retryable=(RuntimeError,),
            label="test",
        )
        assert result == "recovered"
        assert calls["n"] == 3
        assert mock_sleep.call_count == 2

    @patch("app.retry.time.sleep")
    def test_uses_backoff_delays(self, mock_sleep):
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            raise OSError("down")

        with pytest.raises(OSError, match="down"):
            retry_with_backoff(
                always_fail,
                max_attempts=3,
                backoff=(1, 2, 4),
                retryable=(OSError,),
            )

        assert mock_sleep.call_args_list[0][0] == (1,)
        assert mock_sleep.call_args_list[1][0] == (2,)

    @patch("app.retry.time.sleep")
    def test_raises_last_exception_on_exhaustion(self, mock_sleep):
        def always_fail():
            raise RuntimeError("persistent")

        with pytest.raises(RuntimeError, match="persistent"):
            retry_with_backoff(
                always_fail,
                max_attempts=2,
                retryable=(RuntimeError,),
            )

    @patch("app.retry.time.sleep")
    def test_non_retryable_exception_propagates_immediately(self, mock_sleep):
        def bad():
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            retry_with_backoff(
                bad,
                retryable=(RuntimeError,),
            )
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    def test_is_transient_filter(self, mock_sleep):
        """When is_transient returns False, exception is re-raised immediately."""
        def fail():
            raise RuntimeError("not found")

        with pytest.raises(RuntimeError, match="not found"):
            retry_with_backoff(
                fail,
                retryable=(RuntimeError,),
                is_transient=lambda e: "timeout" in str(e),
            )
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    def test_is_transient_allows_retry(self, mock_sleep):
        """When is_transient returns True, retry proceeds."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("connection timeout")
            return "ok"

        result = retry_with_backoff(
            flaky,
            retryable=(RuntimeError,),
            is_transient=lambda e: "timeout" in str(e),
        )
        assert result == "ok"
        assert mock_sleep.call_count == 1

    @patch("app.retry.time.sleep")
    def test_single_attempt_no_retry(self, mock_sleep):
        def fail():
            raise RuntimeError("once")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                fail,
                max_attempts=1,
                retryable=(RuntimeError,),
            )
        mock_sleep.assert_not_called()


class TestNonRetryable:
    """Tests for the non_retryable parameter in retry_with_backoff()."""

    @patch("app.retry.time.sleep")
    def test_non_retryable_aborts_immediately(self, mock_sleep):
        """When non_retryable returns True, exception is re-raised without retry."""
        def fail():
            raise RuntimeError("secondary rate limit")

        with pytest.raises(RuntimeError, match="secondary rate limit"):
            retry_with_backoff(
                fail,
                max_attempts=3,
                retryable=(RuntimeError,),
                non_retryable=lambda e: "secondary" in str(e),
            )
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    def test_non_retryable_false_allows_retry(self, mock_sleep):
        """When non_retryable returns False, normal retry proceeds."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("connection timeout")
            return "ok"

        result = retry_with_backoff(
            flaky,
            retryable=(RuntimeError,),
            non_retryable=lambda e: "secondary" in str(e),
        )
        assert result == "ok"
        assert calls["n"] == 2

    @patch("app.retry.time.sleep")
    def test_non_retryable_checked_before_is_transient(self, mock_sleep):
        """non_retryable takes precedence over is_transient."""
        transient_called = {"called": False}

        def fail():
            raise RuntimeError("secondary rate limit timeout")

        def is_transient(exc):
            transient_called["called"] = True
            return True

        with pytest.raises(RuntimeError, match="secondary rate limit"):
            retry_with_backoff(
                fail,
                max_attempts=3,
                retryable=(RuntimeError,),
                non_retryable=lambda e: "secondary" in str(e),
                is_transient=is_transient,
            )
        # non_retryable should short-circuit before is_transient is consulted
        assert not transient_called["called"]

    @patch("app.retry.time.sleep")
    def test_secondary_rate_limit_never_retried(self, mock_sleep):
        """Integration: is_gh_secondary_rate_limit as non_retryable blocks retry."""
        def fail():
            raise RuntimeError("gh failed: ... — You have exceeded a secondary rate limit")

        with pytest.raises(RuntimeError, match="secondary rate limit"):
            retry_with_backoff(
                fail,
                max_attempts=3,
                retryable=(RuntimeError,),
                non_retryable=is_gh_secondary_rate_limit,
                is_transient=is_gh_transient,
            )
        mock_sleep.assert_not_called()


class TestIsGhTransient:
    """Tests for is_gh_transient() keyword detection."""

    @pytest.mark.parametrize("msg", [
        "gh failed: gh pr view... — connection reset by peer",
        "gh failed: gh api... — connection timed out",
        "gh failed: gh pr list... — timeout waiting for response",
        "gh failed: gh api... — 502 Bad Gateway",
        "gh failed: gh api... — 503 Service Unavailable",
        "gh failed: gh api... — 429 rate limit exceeded",
        "gh failed: gh pr... — SSL handshake error",
        "gh failed: gh api... — dns resolution failed",
    ])
    def test_transient_errors(self, msg):
        assert is_gh_transient(RuntimeError(msg)) is True

    @pytest.mark.parametrize("msg", [
        "gh failed: gh pr view... — not found",
        "gh failed: gh api... — permission denied",
        "gh failed: gh pr... — authentication required",
        "gh failed: gh issue... — repository not found",
    ])
    def test_permanent_errors(self, msg):
        assert is_gh_transient(RuntimeError(msg)) is False


class TestIsGhSecondaryRateLimit:
    """Tests for is_gh_secondary_rate_limit() detection."""

    @pytest.mark.parametrize("msg", [
        "gh failed: gh pr create... — You have exceeded a secondary rate limit",
        "gh failed: gh api... — abuse detection triggered",
        "gh failed: gh issue create... — abuse rate limit",
    ])
    def test_secondary_rate_limit_errors(self, msg):
        assert is_gh_secondary_rate_limit(RuntimeError(msg)) is True

    @pytest.mark.parametrize("msg", [
        "gh failed: gh api... — 429 rate limit exceeded",
        "gh failed: gh pr view... — not found",
        "gh failed: gh api... — connection timed out",
    ])
    def test_non_secondary_errors(self, msg):
        assert is_gh_secondary_rate_limit(RuntimeError(msg)) is False


class TestParseRetryAfter:
    """Tests for parse_retry_after() header extraction."""

    @pytest.mark.parametrize("msg,expected", [
        ("gh failed: ... — Retry-After: 60", 60.0),
        ("gh failed: ... — retry-after: 120", 120.0),
        ("gh failed: ... — Retry After 30", 30.0),
        ("gh failed: ... — retry after: 90.5", 90.5),
    ])
    def test_parses_retry_after_value(self, msg, expected):
        result = parse_retry_after(RuntimeError(msg))
        assert result == expected

    @pytest.mark.parametrize("msg", [
        "gh failed: ... — connection timed out",
        "gh failed: ... — not found",
        "gh failed: ... — 429 rate limit exceeded",
    ])
    def test_returns_none_when_absent(self, msg):
        assert parse_retry_after(RuntimeError(msg)) is None


class TestRetryWithBackoffGetRetryDelay:
    """Tests for retry_with_backoff() get_retry_delay parameter."""

    @patch("app.retry.time.sleep")
    def test_uses_explicit_delay_from_get_retry_delay(self, mock_sleep):
        """When get_retry_delay returns a value, it overrides the backoff schedule."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("Retry-After: 45")
            return "ok"

        result = retry_with_backoff(
            flaky,
            retryable=(RuntimeError,),
            get_retry_delay=parse_retry_after,
            label="test",
        )
        assert result == "ok"
        # Should sleep for 45s (from Retry-After), not default backoff 1s
        mock_sleep.assert_called_once_with(45.0)

    @patch("app.retry.time.sleep")
    def test_falls_back_to_backoff_when_no_retry_after(self, mock_sleep):
        """When get_retry_delay returns None, default backoff is used."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("connection timed out")
            return "ok"

        result = retry_with_backoff(
            flaky,
            retryable=(RuntimeError,),
            get_retry_delay=parse_retry_after,
            backoff=(5, 10),
            is_transient=is_gh_transient,
            label="test",
        )
        assert result == "ok"
        mock_sleep.assert_called_once_with(5)
