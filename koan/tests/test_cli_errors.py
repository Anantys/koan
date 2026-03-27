"""Tests for app.cli_errors — CLI error classification."""

import pytest

from app.cli_errors import ErrorCategory, classify_cli_error


class TestClassifyCliError:
    """Core classify_cli_error() behaviour."""

    # -- Success (exit_code=0) should not be classified -------------------------

    def test_exit_code_zero_returns_unknown(self):
        assert classify_cli_error(0) == ErrorCategory.UNKNOWN

    def test_exit_code_zero_ignores_stderr_content(self):
        """Even if stderr has error-like text, exit 0 means success."""
        assert classify_cli_error(0, stderr="timeout warning") == ErrorCategory.UNKNOWN

    # -- Retryable errors -------------------------------------------------------

    @pytest.mark.parametrize("stderr", [
        "Error: HTTP 502 Bad Gateway",
        "HTTP 503 Service Unavailable",
        "HTTP 500 Internal Server Error",
        "Error: connection reset by peer",
        "Error: connection refused",
        "connect ECONNREFUSED 127.0.0.1:443",
        "connect ETIMEDOUT 104.18.0.1:443",
        "connect ECONNRESET",
        "Error: request timed out after 30000ms",
        "Error: timeout waiting for response",
        "Error: read timeout",
        "server temporarily unavailable",
        "Error: internal server error",
        "Error: bad gateway",
        "Error: service unavailable",
        "Error: network is unreachable",
        "getaddrinfo: dns resolution failed",
        "Error: name resolution failed for api.anthropic.com",
    ])
    def test_retryable_errors(self, stderr):
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.RETRYABLE, f"Expected RETRYABLE for: {stderr}"

    def test_retryable_case_insensitive(self):
        assert classify_cli_error(1, stderr="CONNECTION RESET") == ErrorCategory.RETRYABLE

    def test_retryable_in_stdout(self):
        """Retryable patterns in stdout are also detected."""
        assert classify_cli_error(1, stdout="HTTP 503 error") == ErrorCategory.RETRYABLE

    # -- Terminal errors --------------------------------------------------------

    @pytest.mark.parametrize("stderr", [
        "Error: authentication failed",
        "Error: authentication required",
        "Error: authentication error",
        "Error: unauthorized",
        "Error: invalid api key",
        "Error: invalid-api-key provided",
        "Error: permission denied",
        "Error: context window exceeded",
        "Error: context_window_exceeded",
        "Error: invalid request body",
        "HTTP 400 Bad Request",
        "HTTP 401 Unauthorized",
        "HTTP 403 Forbidden",
    ])
    def test_terminal_errors(self, stderr):
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.TERMINAL, f"Expected TERMINAL for: {stderr}"

    def test_terminal_case_insensitive(self):
        assert classify_cli_error(1, stderr="PERMISSION DENIED") == ErrorCategory.TERMINAL

    # -- Quota errors -----------------------------------------------------------

    @pytest.mark.parametrize("stderr", [
        "Error: out of extra usage quota",
        "quota has been reached",
        "rate limit exceeded for this billing period",
        "too many requests",
        "HTTP 429 Too Many Requests",
        "usage limit reached",
        "retry-after: 3600",
    ])
    def test_quota_errors(self, stderr):
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.QUOTA, f"Expected QUOTA for: {stderr}"

    # -- Unknown errors ---------------------------------------------------------

    def test_unknown_for_unrecognized_error(self):
        result = classify_cli_error(1, stderr="Something went wrong")
        assert result == ErrorCategory.UNKNOWN

    def test_unknown_for_empty_output(self):
        result = classify_cli_error(1)
        assert result == ErrorCategory.UNKNOWN

    def test_unknown_for_generic_exit_code(self):
        result = classify_cli_error(42, stderr="")
        assert result == ErrorCategory.UNKNOWN

    # -- Priority: quota beats retryable ----------------------------------------

    def test_quota_takes_priority_over_retryable(self):
        """A 429 with quota text should be QUOTA, not RETRYABLE."""
        stderr = "HTTP 429 Too Many Requests — rate limit exceeded"
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.QUOTA

    # -- Priority: terminal beats retryable -------------------------------------

    def test_terminal_checked_before_retryable(self):
        """If both terminal and retryable patterns match, terminal wins."""
        stderr = "authentication failed after timeout"
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.TERMINAL

    # -- Combined stdout+stderr -------------------------------------------------

    def test_combined_stdout_and_stderr(self):
        result = classify_cli_error(
            1,
            stdout="partial output",
            stderr="HTTP 503 Service Unavailable",
        )
        assert result == ErrorCategory.RETRYABLE

    # -- Real-world error samples -----------------------------------------------

    def test_real_claude_overloaded(self):
        stderr = (
            "Error: Overloaded\n"
            "The API server is temporarily unavailable. "
            "Please try again later."
        )
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.RETRYABLE

    def test_real_claude_quota(self):
        stderr = (
            "You've run out of extra usage for Claude. "
            "Your quota resets 10am (Europe/Paris)."
        )
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.QUOTA

    def test_real_connection_reset_midstream(self):
        stderr = "Error: socket hang up\nconnection reset by peer"
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.RETRYABLE

    def test_real_invalid_api_key(self):
        stderr = "Error: Invalid API key provided. Check your ANTHROPIC_API_KEY."
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.TERMINAL

    # -- Auth errors (logged-out Claude) ----------------------------------------

    @pytest.mark.parametrize("stderr", [
        'Please run /login · API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"OAuth token has expired."}}',
        "OAuth token has expired. Please obtain a new token or refresh your existing token.",
        "Please run /login",
        "Error: not authenticated",
        "Please log in to continue",
        "please obtain a new token",
        "refresh your existing token",
    ])
    def test_auth_errors(self, stderr):
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.AUTH, f"Expected AUTH for: {stderr}"

    def test_auth_takes_priority_over_terminal(self):
        """Auth errors with 401/unauthorized text should be AUTH, not TERMINAL."""
        stderr = (
            'Please run /login · API Error: 401 '
            '{"type":"error","error":{"type":"authentication_error",'
            '"message":"OAuth token has expired."}}'
        )
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.AUTH

    def test_real_claude_logged_out(self):
        """Real-world logged-out error from the issue report."""
        stderr = (
            'Please run /login · API Error: 401 '
            '{"type":"error","error":{"type":"authentication_error",'
            '"message":"OAuth token has expired. Please obtain a new token '
            'or refresh your existing token."},'
            '"request_id":"req_011CZSUUxgv7cvbLAuhJY4ux"}'
        )
        result = classify_cli_error(1, stderr=stderr)
        assert result == ErrorCategory.AUTH
