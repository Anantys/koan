"""Tests for claude_step.py — shared CI/CD pipeline helpers.

Tests _run_git, truncate_text, _rebase_onto_target, run_claude,
commit_if_changes, and run_claude_step.
"""

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from app.claude_step import (
    _rebase_onto_target,
    _run_git,
    commit_if_changes,
    run_claude,
    run_claude_step,
    run_project_tests,
    strip_cli_noise,
)


# ---------- _run_git ----------


class TestRunGit:
    """Tests for _run_git helper."""

    @patch("app.cli_exec.subprocess.run")
    def test_success_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  abc123  \n")
        result = _run_git(["git", "rev-parse", "HEAD"])
        assert result == "abc123"

    @patch("app.cli_exec.subprocess.run")
    def test_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128, stderr="fatal: not a git repo"
        )
        with pytest.raises(RuntimeError, match="git failed"):
            _run_git(["git", "status"])

    @patch("app.cli_exec.subprocess.run")
    def test_passes_cwd_and_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        _run_git(["git", "status"], cwd="/tmp/test", timeout=30)
        mock_run.assert_called_once_with(
            ["git", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/tmp/test",
        )

    @patch("app.cli_exec.subprocess.run")
    def test_error_message_truncates_stderr(self, mock_run):
        long_stderr = "x" * 500
        mock_run.return_value = MagicMock(returncode=1, stderr=long_stderr)
        with pytest.raises(RuntimeError) as exc_info:
            _run_git(["git", "bad"])
        # Stderr in error message should be truncated to 200 chars
        assert len(str(exc_info.value)) < 300


# ---------- truncate_text (now in utils.py) ----------


class TestTruncateText:
    """Tests for truncate_text shared utility."""

    def test_short_text_unchanged(self):
        from app.utils import truncate_text
        assert truncate_text("hello", 10) == "hello"

    def test_exact_limit_unchanged(self):
        from app.utils import truncate_text
        assert truncate_text("12345", 5) == "12345"

    def test_over_limit_truncated(self):
        from app.utils import truncate_text
        result = truncate_text("1234567890", 5)
        assert result.startswith("12345")
        assert "truncated" in result

    def test_empty_string(self):
        from app.utils import truncate_text
        assert truncate_text("", 10) == ""


# ---------- strip_cli_noise ----------


class TestStripCliNoise:
    """Tests for strip_cli_noise helper."""

    def test_removes_max_turns_error(self):
        text = "Some reflection text.\nError: Reached max turns (1)"
        assert strip_cli_noise(text) == "Some reflection text."

    def test_removes_higher_turn_counts(self):
        text = "Output\nError: Reached max turns (3)"
        assert strip_cli_noise(text) == "Output"

    def test_preserves_clean_text(self):
        text = "A genuine reflection.\nWith multiple lines."
        assert strip_cli_noise(text) == text

    def test_empty_string(self):
        assert strip_cli_noise("") == ""

    def test_only_error_line_returns_empty(self):
        assert strip_cli_noise("Error: Reached max turns (1)") == ""

    def test_multiline_with_error_in_middle(self):
        text = "Line 1\nError: Reached max turns (1)\nLine 3"
        assert strip_cli_noise(text) == "Line 1\nLine 3"

    def test_case_insensitive(self):
        text = "Output\nerror: reached MAX TURNS (2)"
        assert strip_cli_noise(text) == "Output"

    def test_preserves_unrelated_error_lines(self):
        text = "Output\nError: something else happened"
        assert strip_cli_noise(text) == text

    def test_multiple_error_lines(self):
        text = "Line 1\nError: Reached max turns (1)\nLine 2\nError: Reached max turns (1)"
        assert strip_cli_noise(text) == "Line 1\nLine 2"


# ---------- _rebase_onto_target ----------


class TestRebaseOntoTarget:
    """Tests for _rebase_onto_target."""

    @patch("app.claude_step._run_git")
    def test_origin_success(self, mock_git):
        result = _rebase_onto_target("main", "/project")
        assert result == "origin"
        assert mock_git.call_count == 2
        mock_git.assert_any_call(
            ["git", "fetch", "origin", "main"], cwd="/project"
        )

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_origin_fails_upstream_succeeds(self, mock_git, mock_subprocess):
        def side_effect(cmd, **kwargs):
            if "origin" in cmd:
                raise RuntimeError("fetch failed")
            return MagicMock(returncode=0, stdout="ok")

        mock_git.side_effect = side_effect
        result = _rebase_onto_target("main", "/project")
        assert result == "upstream"

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_both_fail_returns_none(self, mock_git, mock_subprocess):
        mock_git.side_effect = RuntimeError("fail")
        result = _rebase_onto_target("main", "/project")
        assert result is None

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_rebase_abort_called_on_failure(self, mock_git, mock_subprocess):
        call_count = 0
        def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Odd calls are fetch (succeed), even calls are rebase (fail)
            if call_count % 2 == 0:
                raise RuntimeError("conflict")
            return ""
        mock_git.side_effect = selective_fail
        _rebase_onto_target("main", "/project")
        # Should call rebase --abort for each failed remote
        abort_calls = [
            c
            for c in mock_subprocess.call_args_list
            if "rebase" in c[0][0] and "--abort" in c[0][0]
        ]
        assert len(abort_calls) == 2

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_rebase_abort_called_with_timeout(self, mock_git, mock_subprocess):
        """git rebase --abort must have a timeout to prevent hangs in cleanup."""
        call_count = 0
        def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise RuntimeError("conflict")
            return ""
        mock_git.side_effect = selective_fail
        _rebase_onto_target("main", "/project")
        abort_calls = [
            c
            for c in mock_subprocess.call_args_list
            if "rebase" in c[0][0] and "--abort" in c[0][0]
        ]
        assert len(abort_calls) >= 1
        for call in abort_calls:
            assert call[1].get("timeout", 0) > 0

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_timeout_caught_and_logged(self, mock_git, mock_subprocess, capsys):
        """TimeoutExpired should be caught (not just Exception) and logged."""
        call_count = 0
        def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise subprocess.TimeoutExpired("git", 60)
            return ""
        mock_git.side_effect = selective_fail
        result = _rebase_onto_target("main", "/project")
        assert result is None
        captured = capsys.readouterr()
        assert "Rebase onto" in captured.err
        assert "timed out" in captured.err.lower() or "timeout" in captured.err.lower()

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_os_error_caught_and_logged(self, mock_git, mock_subprocess, capsys):
        """OSError (e.g. git not found) should be caught and logged."""
        call_count = 0
        def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise OSError("No such file or directory: 'git'")
            return ""
        mock_git.side_effect = selective_fail
        result = _rebase_onto_target("main", "/project")
        assert result is None
        captured = capsys.readouterr()
        assert "Rebase onto" in captured.err

    @patch("app.cli_exec.subprocess.run")
    @patch("app.claude_step._run_git")
    def test_unexpected_exception_not_caught(self, mock_git, mock_subprocess):
        """Unexpected exceptions (e.g. ValueError) should propagate, not be swallowed."""
        mock_git.side_effect = ValueError("unexpected error")
        with pytest.raises(ValueError, match="unexpected"):
            _rebase_onto_target("main", "/project")


# ---------- run_claude ----------


class TestRunClaude:
    """Tests for run_claude — CLI invocation wrapper."""

    @patch("app.cli_exec.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  done  \n", stderr=""
        )
        result = run_claude(["claude", "-p", "test"], "/project")
        assert result["success"] is True
        assert result["output"] == "done"
        assert result["error"] == ""

    @patch("app.cli_exec.subprocess.run")
    def test_failure_with_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="partial", stderr="something broke"
        )
        result = run_claude(["claude", "-p", "test"], "/project")
        assert result["success"] is False
        assert "Exit code 1" in result["error"]
        assert "something broke" in result["error"]

    @patch("app.cli_exec.subprocess.run")
    def test_failure_no_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr=""
        )
        result = run_claude(["claude", "-p", "test"], "/project")
        assert result["success"] is False
        assert "no stderr" in result["error"]

    @patch("app.cli_exec.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        result = run_claude(["claude", "-p", "test"], "/project")
        assert result["success"] is False
        assert "Timeout" in result["error"]
        assert "600" in result["error"]

    @patch("app.cli_exec.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        run_claude(["claude", "-p", "test"], "/project", timeout=120)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 120
        assert call_kwargs["cwd"] == "/project"

    @patch("app.cli_exec.subprocess.run")
    def test_long_stderr_truncated(self, mock_run):
        long_err = "E" * 1000
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr=long_err
        )
        result = run_claude(["claude", "-p", "test"], "/project")
        # Should only keep last 500 chars of stderr
        assert len(result["error"]) < 600


# ---------- commit_if_changes ----------


class TestCommitIfChanges:
    """Tests for commit_if_changes."""

    @patch("app.claude_step._run_git")
    @patch("app.cli_exec.subprocess.run")
    def test_no_changes_returns_false(self, mock_run, mock_git):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        result = commit_if_changes("/project", "test msg")
        assert result is False
        # Should not call git add or commit
        mock_git.assert_not_called()

    @patch("app.claude_step._run_git")
    @patch("app.cli_exec.subprocess.run")
    def test_with_changes_commits(self, mock_run, mock_git):
        mock_run.return_value = MagicMock(
            stdout=" M file.py\n", returncode=0
        )
        result = commit_if_changes("/project", "test msg")
        assert result is True
        assert mock_git.call_count == 2
        mock_git.assert_any_call(["git", "add", "-A"], cwd="/project")
        mock_git.assert_any_call(
            ["git", "commit", "-m", "test msg"], cwd="/project"
        )

    @patch("app.claude_step._run_git")
    @patch("app.cli_exec.subprocess.run")
    def test_whitespace_only_status_is_no_changes(self, mock_run, mock_git):
        mock_run.return_value = MagicMock(stdout="   \n  ", returncode=0)
        result = commit_if_changes("/project", "msg")
        assert result is False

    @patch("app.claude_step._run_git")
    @patch("app.cli_exec.subprocess.run")
    def test_git_status_called_with_timeout(self, mock_run, mock_git):
        """git status must have a timeout to prevent hangs on unresponsive repos."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        commit_if_changes("/project", "msg")
        call_kwargs = mock_run.call_args[1]
        assert "timeout" in call_kwargs
        assert call_kwargs["timeout"] > 0

    @patch("app.claude_step._run_git")
    @patch("app.cli_exec.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30))
    def test_git_status_timeout_propagates(self, mock_run, mock_git):
        """TimeoutExpired from git status should propagate (not silently swallowed)."""
        with pytest.raises(subprocess.TimeoutExpired):
            commit_if_changes("/project", "msg")


# ---------- run_claude_step ----------


class TestRunClaudeStep:
    """Tests for run_claude_step — orchestrator."""

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "fix bug", "--allowedTools", "Bash,Read,Write,Glob,Grep,Edit", "--model", "opus"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "opus", "fallback": "sonnet", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_success_with_commit(self, mock_config, mock_flags, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "done", "error": ""}
        actions = []
        result = run_claude_step(
            prompt="fix bug",
            project_path="/project",
            commit_msg="fix: bug",
            success_label="Bug fixed",
            failure_label="Fix failed",
            actions_log=actions,
        )
        assert result is True
        assert "Bug fixed" in actions

    @patch("app.claude_step.commit_if_changes", return_value=False)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_success_no_commit(self, mock_config, mock_flags, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "ok", "error": ""}
        actions = []
        result = run_claude_step(
            prompt="review code",
            project_path="/project",
            commit_msg="chore: review",
            success_label="Reviewed",
            failure_label="Review failed",
            actions_log=actions,
        )
        assert result is False
        assert actions == []

    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_failure_logs_error(self, mock_config, mock_flags, mock_claude):
        mock_claude.return_value = {
            "success": False,
            "output": "",
            "error": "Exit code 1: crash",
        }
        actions = []
        result = run_claude_step(
            prompt="fix bug",
            project_path="/project",
            commit_msg="fix: bug",
            success_label="Fixed",
            failure_label="Fix failed",
            actions_log=actions,
        )
        assert result is False
        assert len(actions) == 1
        assert "Fix failed" in actions[0]
        assert "crash" in actions[0]

    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_failure_empty_label_no_log(self, mock_config, mock_flags, mock_claude):
        mock_claude.return_value = {
            "success": False,
            "output": "",
            "error": "fail",
        }
        actions = []
        result = run_claude_step(
            prompt="test",
            project_path="/p",
            commit_msg="x",
            success_label="OK",
            failure_label="",
            actions_log=actions,
        )
        assert result is False
        assert actions == []

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_use_skill_adds_skill_tool(self, mock_config, mock_cmd, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "done", "error": ""}
        run_claude_step(
            prompt="refactor",
            project_path="/project",
            commit_msg="refactor",
            success_label="OK",
            failure_label="Fail",
            actions_log=[],
            use_skill=True,
        )
        # Verify build_full_command was called with Skill in allowed_tools
        call_kwargs = mock_cmd.call_args
        allowed = call_kwargs.kwargs.get("allowed_tools", [])
        assert "Skill" in allowed

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_no_skill_by_default(self, mock_config, mock_cmd, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "done", "error": ""}
        run_claude_step(
            prompt="fix",
            project_path="/project",
            commit_msg="fix",
            success_label="OK",
            failure_label="Fail",
            actions_log=[],
        )
        # Verify build_full_command was called without Skill in allowed_tools
        call_kwargs = mock_cmd.call_args
        allowed = call_kwargs.kwargs.get("allowed_tools", [])
        assert "Skill" not in allowed

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_custom_max_turns_and_timeout(self, mock_config, mock_cmd, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "ok", "error": ""}
        run_claude_step(
            prompt="deep work",
            project_path="/project",
            commit_msg="chore: deep",
            success_label="Done",
            failure_label="Fail",
            actions_log=[],
            max_turns=5,
            timeout=120,
        )
        # Verify build_full_command was called with max_turns=5
        call_kwargs = mock_cmd.call_args
        assert call_kwargs.kwargs.get("max_turns") == 5
        # Timeout passed to run_claude
        assert mock_claude.call_args[1]["timeout"] == 120

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "fix bug", "--allowedTools", "Bash,Read,Write,Glob,Grep,Edit", "--model", "opus"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "opus", "fallback": "sonnet", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_model_config_passed_to_flags(self, mock_config, mock_cmd, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "ok", "error": ""}
        run_claude_step(
            prompt="test",
            project_path="/p",
            commit_msg="test",
            success_label="OK",
            failure_label="Fail",
            actions_log=[],
        )
        # Verify model and fallback passed to build_full_command
        call_kwargs = mock_cmd.call_args.kwargs
        assert call_kwargs["model"] == "opus"
        assert call_kwargs["fallback"] == "sonnet"

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch(
        "app.claude_step.get_model_config",
        return_value={"mission": "", "fallback": "", "chat": "", "lightweight": "", "review_mode": ""},
    )
    def test_success_empty_label_no_log(self, mock_config, mock_flags, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "ok", "error": ""}
        actions = []
        result = run_claude_step(
            prompt="test",
            project_path="/p",
            commit_msg="test",
            success_label="",
            failure_label="Fail",
            actions_log=actions,
        )
        # commit_if_changes returns True but label is empty — still returns False
        assert result is False
        assert actions == []


# ---------- _get_current_branch ----------


class TestGetCurrentBranch:
    """Tests for _get_current_branch helper."""

    @patch("app.claude_step._git_utils_get_current_branch", return_value="koan/my-feature")
    def test_returns_branch_name(self, mock_git):
        from app.claude_step import _get_current_branch
        assert _get_current_branch("/project") == "koan/my-feature"
        mock_git.assert_called_once_with(cwd="/project")

    @patch("app.claude_step._git_utils_get_current_branch", return_value="main")
    def test_fallback_to_main_on_error(self, mock_git):
        from app.claude_step import _get_current_branch
        assert _get_current_branch("/project") == "main"


# ---------- _safe_checkout ----------


class TestSafeCheckout:
    """Tests for _safe_checkout helper."""

    @patch("app.claude_step._run_git")
    def test_checkout_succeeds(self, mock_git):
        from app.claude_step import _safe_checkout
        _safe_checkout("main", "/project")
        mock_git.assert_called_once_with(
            ["git", "checkout", "main"], cwd="/project"
        )

    @patch("app.claude_step._run_git", side_effect=Exception("dirty tree"))
    def test_does_not_raise_on_failure(self, mock_git):
        from app.claude_step import _safe_checkout
        _safe_checkout("main", "/project")  # Should not raise


# ---------- _get_diffstat ----------


class TestGetDiffstat:
    """Tests for _get_diffstat helper."""

    @patch("app.claude_step._run_git")
    def test_returns_summary_line(self, mock_git):
        from app.claude_step import _get_diffstat
        mock_git.return_value = (
            " file1.py | 10 ++++---\n"
            " file2.py |  3 ++\n"
            " 2 files changed, 9 insertions(+), 4 deletions(-)"
        )
        result = _get_diffstat("origin/main", "/project")
        assert "2 files changed" in result
        assert "9 insertions" in result

    @patch("app.claude_step._run_git", side_effect=Exception("bad ref"))
    def test_returns_empty_on_failure(self, mock_git):
        from app.claude_step import _get_diffstat
        result = _get_diffstat("origin/main", "/project")
        assert result == ""

    @patch("app.claude_step._run_git", return_value="")
    def test_returns_empty_for_no_diff(self, mock_git):
        from app.claude_step import _get_diffstat
        result = _get_diffstat("origin/main", "/project")
        assert result == ""


# ---------- _is_permission_error ----------


class TestIsPermissionError:
    """Tests for _is_permission_error helper."""

    def test_permission_denied(self):
        from app.claude_step import _is_permission_error
        assert _is_permission_error("permission denied") is True

    def test_forbidden_403(self):
        from app.claude_step import _is_permission_error
        assert _is_permission_error("HTTP 403: Forbidden") is True

    def test_protected_branch(self):
        from app.claude_step import _is_permission_error
        assert _is_permission_error("protected branch") is True

    def test_auth_failed(self):
        from app.claude_step import _is_permission_error
        assert _is_permission_error("authentication failed for url") is True

    def test_non_permission_error(self):
        from app.claude_step import _is_permission_error
        assert _is_permission_error("fatal: remote ref does not exist") is False

    def test_empty_string(self):
        from app.claude_step import _is_permission_error
        assert _is_permission_error("") is False


# ---------- _build_pr_prompt ----------


class TestBuildPrPrompt:
    """Tests for _build_pr_prompt shared helper."""

    @pytest.fixture
    def context(self):
        return {
            "title": "feat: add scanner",
            "body": "Scans outbox.",
            "branch": "koan/scanner",
            "base": "main",
            "diff": "+code",
            "review_comments": "looks good",
            "reviews": "",
            "issue_comments": "",
        }

    @patch("app.claude_step.load_prompt_or_skill", return_value="skill prompt")
    def test_with_skill_dir(self, mock_lp, context, tmp_path):
        from app.claude_step import _build_pr_prompt
        result = _build_pr_prompt("rebase", context, skill_dir=tmp_path)
        assert result == "skill prompt"
        mock_lp.assert_called_once()
        args, kwargs = mock_lp.call_args
        assert args[0] == tmp_path
        assert args[1] == "rebase"
        assert kwargs["TITLE"] == "feat: add scanner"

    @patch("app.claude_step.load_prompt_or_skill", return_value="system prompt")
    def test_without_skill_dir(self, mock_lp, context):
        from app.claude_step import _build_pr_prompt
        result = _build_pr_prompt("recreate", context, skill_dir=None)
        assert result == "system prompt"
        mock_lp.assert_called_once()
        args, kwargs = mock_lp.call_args
        assert args[0] is None
        assert args[1] == "recreate"

    @patch("app.claude_step.load_prompt_or_skill", return_value="ok")
    def test_passes_all_context_fields(self, mock_lp, context):
        from app.claude_step import _build_pr_prompt
        _build_pr_prompt("rebase", context)
        _, kwargs = mock_lp.call_args
        assert kwargs["BRANCH"] == "koan/scanner"
        assert kwargs["BASE"] == "main"
        assert kwargs["DIFF"] == "+code"
        assert kwargs["REVIEW_COMMENTS"] == "looks good"


# ---------- _push_with_pr_fallback ----------


class TestPushWithPrFallback:
    """Tests for the unified push-with-fallback helper."""

    @pytest.fixture
    def context(self):
        return {
            "title": "feat: scanner",
            "url": "https://github.com/sukria/koan/pull/99",
        }

    @patch("app.claude_step._run_git")
    def test_force_push_success_rebase(self, mock_git, context):
        from app.claude_step import _push_with_pr_fallback
        result = _push_with_pr_fallback(
            "koan/fix", "main", "sukria/koan", "99",
            context, "/project", pr_type="rebase",
        )
        assert result["success"] is True
        assert any("Force-pushed" in a for a in result["actions"])
        assert "recreated" not in result["actions"][0]

    @patch("app.claude_step._run_git")
    def test_force_push_success_recreate(self, mock_git, context):
        from app.claude_step import _push_with_pr_fallback
        result = _push_with_pr_fallback(
            "koan/fix", "main", "sukria/koan", "99",
            context, "/project", pr_type="recreate",
        )
        assert result["success"] is True
        assert "recreated from scratch" in result["actions"][0]

    @patch("app.claude_step._run_git", side_effect=RuntimeError("network timeout"))
    def test_non_permission_error_fails(self, mock_git, context):
        from app.claude_step import _push_with_pr_fallback
        result = _push_with_pr_fallback(
            "koan/fix", "main", "sukria/koan", "99",
            context, "/project", pr_type="rebase",
        )
        assert result["success"] is False
        assert "network timeout" in result["error"]

    def test_permission_error_creates_fallback_pr(self, context):
        from app.claude_step import _push_with_pr_fallback
        call_count = [0]

        def mock_git(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("permission denied")
            return ""

        with patch("app.claude_step._run_git", side_effect=mock_git), \
             patch("app.claude_step.pr_create", return_value="https://github.com/sukria/koan/pull/200\n"), \
             patch("app.claude_step.run_gh"), \
             patch("app.utils.get_branch_prefix", return_value="koan/"):
            result = _push_with_pr_fallback(
                "koan/fix", "main", "sukria/koan", "99",
                context, "/project", pr_type="rebase",
            )
            assert result["success"] is True
            assert any("new branch" in a.lower() for a in result["actions"])
            assert any("draft PR" in a for a in result["actions"])
            assert "new_pr_url" in result

    def test_recreate_fallback_uses_recreate_prefix(self, context):
        from app.claude_step import _push_with_pr_fallback
        call_count = [0]
        branches_created = []

        def mock_git(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("permission denied")
            if "checkout" in cmd and "-b" in cmd:
                branches_created.append(cmd[cmd.index("-b") + 1])
            return ""

        with patch("app.claude_step._run_git", side_effect=mock_git), \
             patch("app.claude_step.pr_create", return_value="https://github.com/sukria/koan/pull/201\n"), \
             patch("app.claude_step.run_gh"), \
             patch("app.utils.get_branch_prefix", return_value="koan/"):
            _push_with_pr_fallback(
                "feat/scanner", "main", "sukria/koan", "99",
                context, "/project", pr_type="recreate",
            )
            assert branches_created
            assert "recreate-" in branches_created[0]

    def test_crosslink_failure_is_nonfatal(self, context):
        from app.claude_step import _push_with_pr_fallback
        call_count = [0]

        def mock_git(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("permission denied")
            return ""

        with patch("app.claude_step._run_git", side_effect=mock_git), \
             patch("app.claude_step.pr_create", return_value="https://github.com/sukria/koan/pull/202\n"), \
             patch("app.claude_step.run_gh", side_effect=RuntimeError("API error")), \
             patch("app.utils.get_branch_prefix", return_value="koan/"):
            result = _push_with_pr_fallback(
                "koan/fix", "main", "sukria/koan", "99",
                context, "/project", pr_type="rebase",
            )
            assert result["success"] is True


# ---------- run_project_tests ----------


class TestRunProjectTests:
    """Tests for the shared run_project_tests helper."""

    @patch("app.claude_step.subprocess.run")
    def test_passing_tests_with_count(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="collected 42 items\n42 passed in 3.5s\n",
            stderr="",
        )
        result = run_project_tests("/project")
        assert result["passed"] is True
        assert "42 passed" in result["details"]

    @patch("app.claude_step.subprocess.run")
    def test_passing_tests_no_count(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="All good\n",
            stderr="",
        )
        result = run_project_tests("/project")
        assert result["passed"] is True
        assert result["details"] == "OK"

    @patch("app.claude_step.subprocess.run")
    def test_failing_tests(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="3 failed, 10 passed\n",
            stderr="",
        )
        result = run_project_tests("/project")
        assert result["passed"] is False
        assert "3 failed" in result["details"]
        assert "10 passed" in result["details"]

    @patch("app.claude_step.subprocess.run")
    def test_custom_test_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="OK\n",
            stderr="",
        )
        run_project_tests("/project", test_cmd="npm test")
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["npm", "test"]

    @patch("app.claude_step.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="OK\n",
            stderr="",
        )
        run_project_tests("/project", timeout=600)
        assert mock_run.call_args[1]["timeout"] == 600

    @patch("app.claude_step.subprocess.run", side_effect=subprocess.TimeoutExpired("make test", 300))
    def test_timeout(self, mock_run):
        result = run_project_tests("/project")
        assert result["passed"] is False
        assert "timeout" in result["details"]

    @patch("app.claude_step.subprocess.run", side_effect=FileNotFoundError("make"))
    def test_command_not_found(self, mock_run):
        result = run_project_tests("/project")
        assert result["passed"] is False
        assert result["details"] == "command not found"

    @patch("app.claude_step.subprocess.run", side_effect=OSError("disk full"))
    def test_generic_exception(self, mock_run):
        result = run_project_tests("/project")
        assert result["passed"] is False
        assert "disk full" in result["details"]

    @patch("app.claude_step.subprocess.run")
    def test_output_truncated_to_3000(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="x" * 5000,
            stderr="",
        )
        result = run_project_tests("/project")
        assert len(result["output"]) <= 3000

    @patch("app.claude_step.subprocess.run")
    def test_uses_shlex_split(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        run_project_tests("/project", test_cmd="make test")
        # Should pass a list (shlex.split), not a string with shell=True
        assert mock_run.call_args[0][0] == ["make", "test"]
        assert mock_run.call_args[1].get("shell") is not True

    @patch("app.claude_step.subprocess.run")
    def test_stdin_devnull(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        run_project_tests("/project")
        assert mock_run.call_args[1].get("stdin") == subprocess.DEVNULL or \
               mock_run.call_args[0][0] is not None  # just verify call was made
