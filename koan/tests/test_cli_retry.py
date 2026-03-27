"""Tests for app.cli_exec.run_cli_with_retry — CLI retry with error classification."""

import subprocess
from unittest.mock import patch, call

import pytest

from app.cli_exec import run_cli_with_retry, CLI_RETRY_BACKOFF, CLI_RETRY_MAX_ATTEMPTS


def _make_result(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestRunCliWithRetry:
    """Core run_cli_with_retry() behaviour."""

    @patch("app.cli_exec.run_cli")
    def test_success_first_try_no_retry(self, mock_run):
        mock_run.return_value = _make_result(0, stdout="ok")
        result = run_cli_with_retry(["claude", "-p", "test"])
        assert result.returncode == 0
        assert mock_run.call_count == 1

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_retries_on_retryable_then_succeeds(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            _make_result(1, stderr="HTTP 503 Service Unavailable"),
            _make_result(1, stderr="connection reset by peer"),
            _make_result(0, stdout="recovered"),
        ]
        result = run_cli_with_retry(["claude", "-p", "test"])
        assert result.returncode == 0
        assert result.stdout == "recovered"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_uses_correct_backoff_delays(self, mock_run, mock_sleep):
        mock_run.return_value = _make_result(1, stderr="HTTP 502 Bad Gateway")
        result = run_cli_with_retry(["claude", "-p", "test"], max_attempts=3)
        assert result.returncode == 1
        assert mock_sleep.call_args_list == [call(2), call(5)]

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_custom_backoff(self, mock_run, mock_sleep):
        mock_run.return_value = _make_result(1, stderr="timeout")
        result = run_cli_with_retry(
            ["claude", "-p", "test"],
            max_attempts=2,
            backoff=(10, 20),
        )
        assert mock_sleep.call_args_list == [call(10)]

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_no_retry_on_terminal_error(self, mock_run, mock_sleep):
        mock_run.return_value = _make_result(1, stderr="authentication failed")
        result = run_cli_with_retry(["claude", "-p", "test"])
        assert result.returncode == 1
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_no_retry_on_quota_error(self, mock_run, mock_sleep):
        mock_run.return_value = _make_result(1, stderr="out of extra usage quota")
        result = run_cli_with_retry(["claude", "-p", "test"])
        assert result.returncode == 1
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_no_retry_on_unknown_error(self, mock_run, mock_sleep):
        mock_run.return_value = _make_result(1, stderr="something unexpected")
        result = run_cli_with_retry(["claude", "-p", "test"])
        assert result.returncode == 1
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_exhausts_all_attempts_on_persistent_retryable(self, mock_run, mock_sleep):
        mock_run.return_value = _make_result(1, stderr="HTTP 503 Service Unavailable")
        result = run_cli_with_retry(["claude", "-p", "test"], max_attempts=3)
        assert result.returncode == 1
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("app.cli_exec.run_cli")
    def test_single_attempt_no_sleep(self, mock_run):
        mock_run.return_value = _make_result(1, stderr="HTTP 502 Bad Gateway")
        result = run_cli_with_retry(["claude", "-p", "test"], max_attempts=1)
        assert result.returncode == 1
        assert mock_run.call_count == 1

    @patch("app.cli_exec.run_cli")
    def test_passes_kwargs_through(self, mock_run):
        mock_run.return_value = _make_result(0, stdout="ok")
        run_cli_with_retry(
            ["claude", "-p", "test"],
            timeout=42,
            cwd="/tmp",
        )
        _, kwargs = mock_run.call_args
        assert kwargs["timeout"] == 42
        assert kwargs["cwd"] == "/tmp"

    @patch("app.cli_exec.run_cli")
    def test_defaults_capture_output_and_text(self, mock_run):
        mock_run.return_value = _make_result(0, stdout="ok")
        run_cli_with_retry(["claude", "-p", "test"])
        _, kwargs = mock_run.call_args
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

    @patch("app.cli_exec.run_cli")
    def test_caller_can_override_capture_output(self, mock_run):
        mock_run.return_value = _make_result(0, stdout="ok")
        run_cli_with_retry(["claude", "-p", "test"], capture_output=False)
        _, kwargs = mock_run.call_args
        assert kwargs["capture_output"] is False

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_returns_last_result_on_exhaustion(self, mock_run, mock_sleep):
        """When retries exhaust, the last CompletedProcess is returned."""
        results = [
            _make_result(1, stderr="HTTP 503 attempt 1"),
            _make_result(1, stderr="HTTP 503 attempt 2"),
            _make_result(1, stderr="HTTP 503 attempt 3"),
        ]
        mock_run.side_effect = results
        result = run_cli_with_retry(["claude", "-p", "test"], max_attempts=3)
        assert result.stderr == "HTTP 503 attempt 3"

    @patch("app.cli_exec.time.sleep")
    @patch("app.cli_exec.run_cli")
    def test_exit_code_zero_not_retried_even_with_stderr(self, mock_run, mock_sleep):
        """Don't retry if exit code is 0, even if stderr has warning text."""
        mock_run.return_value = _make_result(0, stderr="warning: timeout approaching")
        result = run_cli_with_retry(["claude", "-p", "test"])
        assert result.returncode == 0
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()
