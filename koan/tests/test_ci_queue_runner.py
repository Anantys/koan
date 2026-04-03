"""Tests for ci_queue_runner — CI queue drain, error handling, and fix pipeline."""

import json
from unittest.mock import MagicMock, patch

import pytest


PR_URL = "https://github.com/owner/repo/pull/42"
PROJECT_PATH = "/tmp/test-project"


@pytest.fixture
def _mock_pr_context():
    """Patch external dependencies so run_ci_check_and_fix can run without real git/GitHub."""
    fake_context = {"branch": "fix-branch", "base": "main", "url": PR_URL}
    with (
        patch("app.rebase_pr.fetch_pr_context", return_value=fake_context),
        patch("app.ci_queue_runner.check_ci_status", return_value=("failure", 123)),
        patch("app.claude_step._fetch_failed_logs", return_value="Error: test failed"),
        patch("app.rebase_pr._check_pr_state", return_value=("OPEN", "MERGEABLE")),
        patch("app.claude_step._get_current_branch", return_value="main"),
        patch("app.claude_step._run_git"),
        patch("app.claude_step._safe_checkout"),
    ):
        yield


class TestRunCiCheckAndFixErrorHandling:
    """Verify that exceptions in the fix pipeline are caught, not propagated."""

    @pytest.mark.usefixtures("_mock_pr_context")
    def test_exception_in_fix_returns_failure_tuple(self):
        """When _attempt_ci_fixes raises, run_ci_check_and_fix returns (False, summary)."""
        from app.ci_queue_runner import run_ci_check_and_fix

        with patch(
            "app.ci_queue_runner._attempt_ci_fixes",
            side_effect=RuntimeError("Claude crashed"),
        ):
            success, summary = run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        assert success is False
        assert "Claude crashed" in summary

    @pytest.mark.usefixtures("_mock_pr_context")
    def test_exception_in_fix_still_restores_branch(self):
        """After a crash, _safe_checkout is still called to restore the original branch."""
        from app.ci_queue_runner import run_ci_check_and_fix

        with (
            patch(
                "app.ci_queue_runner._attempt_ci_fixes",
                side_effect=RuntimeError("boom"),
            ),
            patch("app.claude_step._safe_checkout") as mock_checkout,
        ):
            run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        mock_checkout.assert_called_once_with("main", PROJECT_PATH)

    def test_ci_already_passing_returns_success(self):
        """If CI is already passing, return success without attempting fixes."""
        from app.ci_queue_runner import run_ci_check_and_fix

        fake_context = {"branch": "fix-branch", "base": "main"}
        with (
            patch("app.rebase_pr.fetch_pr_context", return_value=fake_context),
            patch("app.ci_queue_runner.check_ci_status", return_value=("success", 123)),
        ):
            success, summary = run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        assert success is True
        assert "already passing" in summary

    def test_ci_pending_returns_early(self):
        """If CI is still pending, return early without attempting fixes."""
        from app.ci_queue_runner import run_ci_check_and_fix

        fake_context = {"branch": "fix-branch", "base": "main"}
        with (
            patch("app.rebase_pr.fetch_pr_context", return_value=fake_context),
            patch("app.ci_queue_runner.check_ci_status", return_value=("pending", 123)),
        ):
            success, summary = run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        assert success is False
        assert "pending" in summary.lower()

    def test_pr_already_merged_returns_success(self):
        """If PR is already merged, skip CI fix."""
        from app.ci_queue_runner import run_ci_check_and_fix

        fake_context = {"branch": "fix-branch", "base": "main"}
        with (
            patch("app.rebase_pr.fetch_pr_context", return_value=fake_context),
            patch("app.ci_queue_runner.check_ci_status", return_value=("failure", 123)),
            patch("app.claude_step._fetch_failed_logs", return_value="Error: test failed"),
            patch("app.rebase_pr._check_pr_state", return_value=("MERGED", "UNKNOWN")),
        ):
            success, summary = run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        assert success is True
        assert "merged" in summary.lower()

    def test_pr_with_conflicts_returns_failure(self):
        """If PR has merge conflicts, skip CI fix."""
        from app.ci_queue_runner import run_ci_check_and_fix

        fake_context = {"branch": "fix-branch", "base": "main"}
        with (
            patch("app.rebase_pr.fetch_pr_context", return_value=fake_context),
            patch("app.ci_queue_runner.check_ci_status", return_value=("failure", 123)),
            patch("app.claude_step._fetch_failed_logs", return_value="Error: test failed"),
            patch("app.rebase_pr._check_pr_state", return_value=("OPEN", "CONFLICTING")),
        ):
            success, summary = run_ci_check_and_fix(PR_URL, PROJECT_PATH)

        assert success is False
        assert "conflicts" in summary.lower()


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

    def _missions_with_ci_entry(self, attempt=0, max_attempts=5):
        """Return missions.md content with one CI entry."""
        return (
            "# Missions\n\n## CI\n\n"
            f"- [project:proj] {PR_URL} branch:fix-branch repo:owner/repo"
            f" queued:2026-04-01T10:00 (attempt {attempt}/{max_attempts})\n\n"
            "## Pending\n\n## Done\n"
        )

    def test_drain_one_no_entries(self):
        """When ## CI section is empty, drain_one returns None."""
        from app.ci_queue_runner import drain_one

        empty_missions = "# Missions\n\n## CI\n\n## Pending\n\n## Done\n"
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=empty_missions),
            patch("app.ci_queue_runner._maybe_migrate_json_queue"),
        ):
            result = drain_one("/tmp/instance")

        assert result is None

    def test_drain_one_success_removes_entry(self):
        """On CI success, entry is removed from ## CI section."""
        from app.ci_queue_runner import drain_one

        missions_content = self._missions_with_ci_entry()
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=missions_content),
            patch("app.ci_queue_runner._maybe_migrate_json_queue"),
            patch("app.utils.modify_missions_file") as mock_modify,
            patch("app.ci_queue_runner.check_ci_status", return_value=("success", 123)),
            patch("app.ci_queue_runner._write_outbox"),
        ):
            result = drain_one("/tmp/instance")

        assert result is not None
        assert "passed" in result.lower()
        mock_modify.assert_called()

    def test_drain_one_failure_injects_mission(self):
        """On CI failure under max attempts, a /ci_check mission is injected."""
        from app.ci_queue_runner import drain_one

        missions_content = self._missions_with_ci_entry(attempt=0, max_attempts=5)
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=missions_content),
            patch("app.ci_queue_runner._maybe_migrate_json_queue"),
            patch("app.utils.modify_missions_file"),
            patch("app.ci_queue_runner.check_ci_status", return_value=("failure", 456)),
            patch("app.ci_queue_runner._inject_ci_fix_mission") as mock_inject,
        ):
            result = drain_one("/tmp/instance")

        assert result is not None
        assert "failed" in result.lower()
        mock_inject.assert_called_once()

    def test_drain_one_failure_at_max_gives_up(self):
        """On CI failure at max attempts, entry is removed and failure notified."""
        from app.ci_queue_runner import drain_one

        missions_content = self._missions_with_ci_entry(attempt=5, max_attempts=5)
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=missions_content),
            patch("app.ci_queue_runner._maybe_migrate_json_queue"),
            patch("app.utils.modify_missions_file") as mock_modify,
            patch("app.ci_queue_runner.check_ci_status", return_value=("failure", 456)),
            patch("app.ci_queue_runner._write_outbox") as mock_outbox,
        ):
            result = drain_one("/tmp/instance")

        assert result is not None
        assert "giving up" in result.lower()
        mock_modify.assert_called()
        mock_outbox.assert_called_once()
        # Failure notification should mention the PR URL
        assert PR_URL in mock_outbox.call_args[0][1]


class TestAttemptCiFixes:
    """Verify the fix pipeline attempts Claude-based fixes correctly."""

    def test_claude_produces_no_changes_gives_up(self):
        """If Claude produces no changes, the pipeline stops."""
        from app.ci_queue_runner import _attempt_ci_fixes

        with (
            patch("app.claude_step._run_git", return_value=""),
            patch("app.rebase_pr.truncate_text", side_effect=lambda t, n: t),
            patch("app.rebase_pr._build_ci_fix_prompt", return_value="fix this"),
            patch("app.claude_step.run_claude_step", return_value=False),
        ):
            actions_log = []
            result = _attempt_ci_fixes(
                branch="fix-branch",
                base="main",
                full_repo="owner/repo",
                pr_number="42",
                pr_url=PR_URL,
                project_path=PROJECT_PATH,
                context={"url": PR_URL},
                ci_logs="Error: test failed",
                actions_log=actions_log,
                max_attempts=2,
            )

        assert result is False
        assert any("no changes" in a.lower() for a in actions_log)

    def test_build_ci_fix_prompt_loads_without_error(self):
        """_build_ci_fix_prompt must load ci_fix.md without FileNotFoundError.

        Regression: ci_queue_runner called _build_ci_fix_prompt without a
        skill_dir, which fell back to system-prompts/ci_fix.md — but that
        file didn't exist, so every /ci_check mission crashed with
        FileNotFoundError and never attempted a fix.
        """
        from app.rebase_pr import _build_ci_fix_prompt

        context = {"title": "fix: test", "branch": "fix-branch", "base": "main"}
        prompt = _build_ci_fix_prompt(context, "Error: test failed", "diff content")

        assert "fix-branch" in prompt
        assert "Error: test failed" in prompt

    def test_successful_fix_and_push(self):
        """If Claude fixes and push succeeds, reports success when CI is pending."""
        from app.ci_queue_runner import _attempt_ci_fixes

        with (
            patch("app.claude_step._run_git", return_value=""),
            patch("app.rebase_pr.truncate_text", side_effect=lambda t, n: t),
            patch("app.rebase_pr._build_ci_fix_prompt", return_value="fix this"),
            patch("app.claude_step.run_claude_step", return_value=True),
            patch("app.rebase_pr._force_push"),
            patch("app.ci_queue_runner.check_ci_status", return_value=("pending", 789)),
            patch("app.ci_queue_runner._reenqueue_for_monitoring") as mock_reenqueue,
            patch("time.sleep"),
        ):
            actions_log = []
            result = _attempt_ci_fixes(
                branch="fix-branch",
                base="main",
                full_repo="owner/repo",
                pr_number="42",
                pr_url=PR_URL,
                project_path=PROJECT_PATH,
                context={"url": PR_URL},
                ci_logs="Error: test failed",
                actions_log=actions_log,
                max_attempts=2,
            )

        assert result is True
        assert any("pushed" in a.lower() for a in actions_log)
        # Verify re-enqueue was called so drain_one monitors the new CI run
        mock_reenqueue.assert_called_once_with(
            PR_URL, "fix-branch", "owner/repo", "42", PROJECT_PATH,
        )
        assert any("re-enqueued" in a.lower() for a in actions_log)

    def test_reenqueue_called_on_pending_ci(self):
        """After pushing a fix, if CI is pending, the PR is re-enqueued in ## CI section."""
        from app.ci_queue_runner import _reenqueue_for_monitoring

        with (
            patch.dict("os.environ", {"KOAN_ROOT": "/tmp/test-koan"}),
            patch("app.utils.modify_missions_file") as mock_modify,
            patch("app.utils.load_config", return_value={"ci_fix_max_attempts": 5}),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _reenqueue_for_monitoring(
                PR_URL, "fix-branch", "owner/repo", "42", PROJECT_PATH,
            )

        mock_modify.assert_called_once()
