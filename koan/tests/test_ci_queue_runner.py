"""Tests for ci_queue_runner — CI queue drain, notification, and error handling."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


PR_URL = "https://github.com/owner/repo/pull/42"
PROJECT_PATH = "/tmp/test-project"


@pytest.fixture
def _mock_pr_context():
    """Patch external dependencies so run_ci_check_and_fix can run without real git/GitHub."""
    fake_context = {"branch": "fix-branch", "base": "main"}
    with (
        patch("app.rebase_pr.fetch_pr_context", return_value=fake_context),
        patch("app.claude_step._get_current_branch", return_value="main"),
        patch("app.claude_step._run_git"),
        patch("app.claude_step._safe_checkout"),
    ):
        yield


class TestRunCiCheckAndFixErrorHandling:
    """Verify that exceptions from _run_ci_check_and_fix are caught, not propagated.

    Before the fix, run_ci_check_and_fix() used try/finally (no except) around
    _run_ci_check_and_fix(). Any exception from CI polling, git ops, or Claude
    CLI would propagate up, crash the subprocess, and produce no JSON output —
    causing the mission runner to see exit code 1 with no parseable result.
    """

    @pytest.mark.usefixtures("_mock_pr_context")
    def test_exception_in_ci_fix_returns_failure_tuple(self):
        """When _run_ci_check_and_fix raises, run_ci_check_and_fix returns (False, summary)."""
        from app.ci_queue_runner import run_ci_check_and_fix

        with patch(
            "app.rebase_pr._run_ci_check_and_fix",
            side_effect=RuntimeError("gh run list failed"),
        ):
            success, summary = run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        assert success is False
        assert "gh run list failed" in summary

    @pytest.mark.usefixtures("_mock_pr_context")
    def test_exception_in_ci_fix_still_restores_branch(self):
        """After a crash, _safe_checkout is still called to restore the original branch."""
        from app.ci_queue_runner import run_ci_check_and_fix

        with (
            patch(
                "app.rebase_pr._run_ci_check_and_fix",
                side_effect=RuntimeError("boom"),
            ),
            patch("app.claude_step._safe_checkout") as mock_checkout,
        ):
            run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        mock_checkout.assert_called_once_with("main", PROJECT_PATH)


class TestMainErrorHandling:
    """Verify that main() always produces JSON on stdout, even when run_ci_check_and_fix crashes."""

    def test_main_outputs_json_on_crash(self, capsys):
        """When run_ci_check_and_fix raises, main() still prints JSON to stdout."""
        from app.ci_queue_runner import main

        with patch(
            "app.ci_queue_runner.run_ci_check_and_fix",
            side_effect=RuntimeError("unexpected failure"),
        ):
            exit_code = main([PR_URL, "--project-path", PROJECT_PATH])

        assert exit_code == 1
        stdout = capsys.readouterr().out
        result = json.loads(stdout)
        assert result["success"] is False
        assert "unexpected failure" in result["summary"]

    def test_main_outputs_json_on_success(self, capsys):
        """Normal success path still produces JSON."""
        from app.ci_queue_runner import main

        with patch(
            "app.ci_queue_runner.run_ci_check_and_fix",
            return_value=(True, "CI passed"),
        ):
            exit_code = main([PR_URL, "--project-path", PROJECT_PATH])

        assert exit_code == 0
        stdout = capsys.readouterr().out
        result = json.loads(stdout)
        assert result["success"] is True


class TestDrainOneErrorHandling:
    """Verify drain_one handles CI status results correctly."""

    def test_drain_one_no_entries(self):
        """When queue is empty, drain_one returns None."""
        from app.ci_queue_runner import drain_one

        with patch("app.ci_queue.peek", return_value=None):
            result = drain_one("/tmp/instance")

        assert result is None

    def test_drain_one_success_removes_entry(self):
        """On CI success, entry is removed from queue."""
        from app.ci_queue_runner import drain_one

        entry = {
            "pr_url": PR_URL,
            "branch": "fix-branch",
            "full_repo": "owner/repo",
            "pr_number": 42,
        }
        with (
            patch("app.ci_queue.peek", return_value=entry),
            patch("app.ci_queue.remove") as mock_remove,
            patch(
                "app.ci_queue_runner.check_ci_status",
                return_value=("success", 123),
            ),
        ):
            result = drain_one("/tmp/instance")

        assert "passed" in result.lower()
        mock_remove.assert_called_once_with("/tmp/instance", PR_URL)

    def test_drain_one_failure_sends_notification(self):
        """On CI failure, a notification is sent via outbox (not a /ci_check mission)."""
        from app.ci_queue_runner import drain_one

        entry = {
            "pr_url": PR_URL,
            "branch": "fix-branch",
            "full_repo": "owner/repo",
            "pr_number": 42,
            "project_path": "/tmp/project",
        }
        with (
            patch("app.ci_queue.peek", return_value=entry),
            patch("app.ci_queue.remove"),
            patch(
                "app.ci_queue_runner.check_ci_status",
                return_value=("failure", 456),
            ),
            patch(
                "app.ci_queue_runner._notify_ci_failure",
            ) as mock_notify,
        ):
            result = drain_one("/tmp/instance")

        assert "failed" in result.lower()
        assert "notification" in result.lower()
        mock_notify.assert_called_once_with("/tmp/instance", PR_URL, entry)


class TestNotifyCiFailure:
    """Verify _notify_ci_failure writes to outbox instead of missions."""

    def test_writes_outbox_with_pr_info(self, tmp_path):
        """Notification includes PR number, URL, and rebase suggestion."""
        from app.ci_queue_runner import _notify_ci_failure

        instance_dir = str(tmp_path)
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")

        entry = {
            "pr_url": PR_URL,
            "pr_number": 42,
            "project_path": "/tmp/my-project",
        }

        _notify_ci_failure(instance_dir, PR_URL, entry)

        content = outbox.read_text()
        assert "CI failed" in content
        assert "PR #42" in content
        assert PR_URL in content
        assert "/rebase" in content

    def test_includes_project_name(self, tmp_path):
        """Notification includes project name derived from path."""
        from app.ci_queue_runner import _notify_ci_failure

        instance_dir = str(tmp_path)
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")

        entry = {
            "pr_url": PR_URL,
            "pr_number": 42,
            "project_path": "/home/koan/workspace/YAML-Syck",
        }

        _notify_ci_failure(instance_dir, PR_URL, entry)

        content = outbox.read_text()
        assert "YAML-Syck" in content

    def test_no_crash_when_outbox_missing(self, tmp_path):
        """If outbox doesn't exist yet, the notification creates it."""
        from app.ci_queue_runner import _notify_ci_failure

        instance_dir = str(tmp_path)
        entry = {"pr_url": PR_URL, "pr_number": 42, "project_path": ""}

        # Should not raise even without existing outbox
        _notify_ci_failure(instance_dir, PR_URL, entry)
