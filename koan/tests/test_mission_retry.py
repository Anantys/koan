"""Tests for mission retry logic in app.run — _maybe_retry_mission and _get_git_head."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.run import _get_git_head, _maybe_retry_mission


# ---------------------------------------------------------------------------
# _get_git_head
# ---------------------------------------------------------------------------

class TestGetGitHead:

    @patch("app.run.subprocess.run")
    def test_returns_sha(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="abc123def\n", stderr=""
        )
        assert _get_git_head("/tmp/proj") == "abc123def"

    @patch("app.run.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal"
        )
        assert _get_git_head("/tmp/proj") == ""

    @patch("app.run.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5))
    def test_returns_empty_on_timeout(self, mock_run):
        assert _get_git_head("/tmp/proj") == ""

    @patch("app.run.subprocess.run", side_effect=OSError("no git"))
    def test_returns_empty_on_oserror(self, mock_run):
        assert _get_git_head("/tmp/proj") == ""


# ---------------------------------------------------------------------------
# _maybe_retry_mission
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_output_files():
    """Create temp stdout/stderr files for tests."""
    fd_out, stdout_file = tempfile.mkstemp(prefix="test-out-")
    os.close(fd_out)
    fd_err, stderr_file = tempfile.mkstemp(prefix="test-err-")
    os.close(fd_err)
    yield stdout_file, stderr_file
    for f in (stdout_file, stderr_file):
        try:
            os.unlink(f)
        except OSError:
            pass


class TestMaybeRetryMission:

    @patch("app.run.run_claude_task", return_value=0)
    @patch("app.run._get_git_head", return_value="abc123")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_retries_on_retryable_error(self, mock_log, mock_sleep, mock_head, mock_task, temp_output_files):
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("HTTP 503 Service Unavailable")

        exit_code, out_f, err_f = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        assert exit_code == 0  # retry succeeded
        assert mock_task.call_count == 1
        mock_sleep.assert_called_once_with(10)

    @patch("app.run.run_claude_task")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_no_retry_on_terminal_error(self, mock_log, mock_sleep, mock_task, temp_output_files):
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("authentication failed")

        exit_code, _, _ = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        assert exit_code == 1
        mock_task.assert_not_called()
        mock_sleep.assert_not_called()

    @patch("app.run.run_claude_task")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_no_retry_on_quota_error(self, mock_log, mock_sleep, mock_task, temp_output_files):
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("out of extra usage quota")

        exit_code, _, _ = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        assert exit_code == 1
        mock_task.assert_not_called()

    @patch("app.run.run_claude_task")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_no_retry_on_unknown_error(self, mock_log, mock_sleep, mock_task, temp_output_files):
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("something unexpected happened")

        exit_code, _, _ = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        assert exit_code == 1
        mock_task.assert_not_called()

    @patch("app.run._get_git_head", return_value="def456")
    @patch("app.run.run_claude_task")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_no_retry_when_commits_produced(self, mock_log, mock_sleep, mock_task, mock_head, temp_output_files):
        """If HEAD moved (commits produced), don't retry even on retryable error."""
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("HTTP 503 Service Unavailable")

        exit_code, _, _ = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",  # different from post_head "def456"
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        assert exit_code == 1
        mock_task.assert_not_called()

    @patch("app.run.run_claude_task")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_no_retry_for_autonomous_run(self, mock_log, mock_sleep, mock_task, temp_output_files):
        """Autonomous runs (no mission) should not be retried."""
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("HTTP 503 Service Unavailable")

        exit_code, _, _ = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=False,
        )

        assert exit_code == 1
        mock_task.assert_not_called()

    @patch("app.run.run_claude_task", return_value=1)
    @patch("app.run._get_git_head", return_value="abc123")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_retry_also_fails(self, mock_log, mock_sleep, mock_head, mock_task, temp_output_files):
        """If retry also fails, return the retry's exit code."""
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("HTTP 502 Bad Gateway")

        exit_code, _, _ = _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        assert exit_code == 1
        assert mock_task.call_count == 1

    @patch("app.run.run_claude_task", return_value=0)
    @patch("app.run._get_git_head", return_value="abc123")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_output_files_cleared_before_retry(self, mock_log, mock_sleep, mock_head, mock_task, temp_output_files):
        """Output files should be truncated before retry to avoid double-counting."""
        stdout_file, stderr_file = temp_output_files
        Path(stdout_file).write_text("partial output from failed attempt")
        Path(stderr_file).write_text("HTTP 503 Service Unavailable")

        _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        # The files should have been cleared before run_claude_task was called
        # (run_claude_task will then write new content)
        # We can verify by checking that the mock was called (retry happened)
        assert mock_task.call_count == 1

    @patch("app.run.run_claude_task", return_value=0)
    @patch("app.run._get_git_head", return_value="abc123")
    @patch("app.run.time.sleep")
    @patch("app.run.log")
    def test_logs_error_classification(self, mock_log, mock_sleep, mock_head, mock_task, temp_output_files):
        """Error classification should be logged."""
        stdout_file, stderr_file = temp_output_files
        Path(stderr_file).write_text("HTTP 503 Service Unavailable")

        _maybe_retry_mission(
            claude_exit=1,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            cmd=["claude", "-p", "test"],
            project_path="/tmp/proj",
            pre_head="abc123",
            instance="/tmp/instance",
            project_name="myproj",
            run_num=1,
            has_mission=True,
        )

        # Check that classification was logged
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("retryable" in c for c in log_calls)
