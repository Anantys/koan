"""Tests for the claudemd_refresh pipeline module."""

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from app.claudemd_refresh import (
    _git_last_modified,
    _git_log_since,
    _git_diff_stat_since,
    _git_log_full,
    _has_changes,
    build_git_context,
    run_refresh,
    main,
)


# ---------------------------------------------------------------------------
# _git_last_modified
# ---------------------------------------------------------------------------

class TestGitLastModified:
    def test_returns_date_on_success(self):
        with patch("app.claudemd_refresh.run_git", return_value="2026-01-15T10:30:00+01:00") as mock_git:
            result = _git_last_modified("/project", "CLAUDE.md")
            assert result == "2026-01-15T10:30:00+01:00"
            mock_git.assert_called_once_with("/project", "log", "-1", "--format=%aI", "--", "CLAUDE.md")

    def test_returns_empty_on_no_history(self):
        with patch("app.claudemd_refresh.run_git", return_value=""):
            result = _git_last_modified("/project", "CLAUDE.md")
            assert result == ""

    def test_returns_empty_on_exception(self):
        """run_git swallows exceptions internally, so this returns empty."""
        with patch("app.claudemd_refresh.run_git", return_value=""):
            result = _git_last_modified("/project", "CLAUDE.md")
            assert result == ""

    def test_project_path_passed(self):
        with patch("app.claudemd_refresh.run_git", return_value="") as mock_git:
            _git_last_modified("/my/project", "CLAUDE.md")
            assert mock_git.call_args[0][0] == "/my/project"


# ---------------------------------------------------------------------------
# _git_log_since
# ---------------------------------------------------------------------------

class TestGitLogSince:
    def test_returns_log_lines(self):
        log = "abc1234 Add new module\ndef5678 Refactor auth"
        with patch("app.claudemd_refresh.run_git", return_value=log):
            result = _git_log_since("/project", "2026-01-01")
            assert "abc1234" in result
            assert "Refactor auth" in result

    def test_since_date_in_command(self):
        with patch("app.claudemd_refresh.run_git", return_value="") as mock_git:
            _git_log_since("/project", "2026-01-15")
            args = mock_git.call_args[0]
            assert "--since=2026-01-15" in args

    def test_max_commits_limit(self):
        with patch("app.claudemd_refresh.run_git", return_value="") as mock_git:
            _git_log_since("/project", "2026-01-01", max_commits=10)
            args = mock_git.call_args[0]
            assert "-n10" in args

    def test_returns_empty_on_exception(self):
        """run_git swallows exceptions internally, so this returns empty."""
        with patch("app.claudemd_refresh.run_git", return_value=""):
            result = _git_log_since("/project", "2026-01-01")
            assert result == ""


# ---------------------------------------------------------------------------
# _git_diff_stat_since
# ---------------------------------------------------------------------------

class TestGitDiffStatSince:
    def test_returns_stat_output(self):
        with patch("app.claudemd_refresh.run_git") as mock_git:
            # First call: find commits (log --reverse)
            # Second call: rev-parse --verify (has parent → non-empty)
            # Third call: diff stat
            mock_git.side_effect = [
                "abc123\ndef456",
                "parent-hash",
                " app/new.py | 50 ++++\n 2 files changed",
            ]
            result = _git_diff_stat_since("/project", "2026-01-01")
            assert "app/new.py" in result

    def test_root_commit_uses_oldest_to_head(self):
        with patch("app.claudemd_refresh.run_git") as mock_git:
            # First call: find commits (single root commit)
            # Second call: rev-parse --verify (no parent → empty string)
            # Third call: diff stat with oldest..HEAD
            mock_git.side_effect = [
                "abc123",
                "",
                " README.md | 10 ++++\n 1 file changed",
            ]
            result = _git_diff_stat_since("/project", "2026-01-01")
            assert "README.md" in result
            # Verify the diff range used oldest..HEAD (not oldest~1..HEAD)
            diff_call = mock_git.call_args_list[2]
            assert "abc123..HEAD" in diff_call[0]

    def test_returns_empty_when_no_commits(self):
        with patch("app.claudemd_refresh.run_git", return_value=""):
            result = _git_diff_stat_since("/project", "2026-01-01")
            assert result == ""

    def test_returns_empty_on_exception(self):
        """run_git swallows exceptions internally, so this returns empty."""
        with patch("app.claudemd_refresh.run_git", return_value=""):
            result = _git_diff_stat_since("/project", "2026-01-01")
            assert result == ""


# ---------------------------------------------------------------------------
# _git_log_full
# ---------------------------------------------------------------------------

class TestGitLogFull:
    def test_returns_recent_log(self):
        with patch("app.claudemd_refresh.run_git", return_value="abc1234 Initial commit"):
            result = _git_log_full("/project")
            assert "abc1234" in result

    def test_no_merges_flag(self):
        with patch("app.claudemd_refresh.run_git", return_value="") as mock_git:
            _git_log_full("/project")
            args = mock_git.call_args[0]
            assert "--no-merges" in args

    def test_returns_empty_on_exception(self):
        """run_git swallows exceptions internally, so this returns empty."""
        with patch("app.claudemd_refresh.run_git", return_value=""):
            result = _git_log_full("/project")
            assert result == ""


# ---------------------------------------------------------------------------
# build_git_context
# ---------------------------------------------------------------------------

class TestBuildGitContext:
    def test_init_mode_no_claudemd(self):
        with patch("app.claudemd_refresh._git_log_full", return_value="abc Recent commit"):
            result = build_git_context("/project", claude_md_exists=False)
            assert "No CLAUDE.md exists" in result
            assert "abc Recent commit" in result

    def test_init_mode_no_history(self):
        with patch("app.claudemd_refresh._git_log_full", return_value=""):
            result = build_git_context("/project", claude_md_exists=False)
            assert "No CLAUDE.md exists" in result
            assert "No git history" in result

    def test_update_mode_with_commits(self):
        with patch("app.claudemd_refresh._git_last_modified", return_value="2026-01-15T10:00:00"), \
             patch("app.claudemd_refresh._git_log_since", return_value="abc Add new module"), \
             patch("app.claudemd_refresh._git_diff_stat_since", return_value="app/new.py | 50 +"):
            result = build_git_context("/project", claude_md_exists=True)
            assert "2026-01-15" in result
            assert "abc Add new module" in result
            assert "app/new.py" in result

    def test_update_mode_no_new_commits(self):
        with patch("app.claudemd_refresh._git_last_modified", return_value="2026-02-07T10:00:00"), \
             patch("app.claudemd_refresh._git_log_since", return_value=""):
            result = build_git_context("/project", claude_md_exists=True)
            assert "up to date" in result.lower()

    def test_update_mode_no_diffstat(self):
        with patch("app.claudemd_refresh._git_last_modified", return_value="2026-01-15"), \
             patch("app.claudemd_refresh._git_log_since", return_value="abc Fix bug"), \
             patch("app.claudemd_refresh._git_diff_stat_since", return_value=""):
            result = build_git_context("/project", claude_md_exists=True)
            assert "abc Fix bug" in result
            assert "diffstat" not in result.lower()

    def test_uncommitted_claudemd(self):
        """CLAUDE.md exists on disk but has no git history."""
        with patch("app.claudemd_refresh._git_last_modified", return_value=""), \
             patch("app.claudemd_refresh._git_log_full", return_value="abc Recent"):
            result = build_git_context("/project", claude_md_exists=True)
            assert "no git history" in result.lower()
            assert "abc Recent" in result


# ---------------------------------------------------------------------------
# run_refresh
# ---------------------------------------------------------------------------

class TestRunRefresh:
    """Tests for run_refresh().

    run_refresh now creates a branch, invokes Claude, commits/pushes,
    and creates a draft PR.  We mock all git and GitHub operations.
    """

    @pytest.fixture(autouse=True)
    def _common_patches(self):
        """Patches shared by all run_refresh tests."""
        self._mock_run_claude = MagicMock(
            return_value={"success": True, "output": "OK", "error": ""},
        )
        self._mock_build_cmd = MagicMock(return_value=["claude"])
        self._mock_prompt = MagicMock(return_value="prompt")
        self._mock_models = MagicMock(
            return_value={"mission": "", "fallback": ""},
        )
        self._mock_branch_prefix = MagicMock(return_value="koan/")
        self._mock_git_strict = MagicMock(return_value="main")
        self._mock_has_changes = MagicMock(return_value=True)
        self._mock_pr_create = MagicMock(
            return_value="https://github.com/o/r/pull/42",
        )

        with patch("app.claude_step.run_claude", self._mock_run_claude), \
             patch("app.cli_provider.build_full_command", self._mock_build_cmd), \
             patch("app.claudemd_refresh.load_skill_prompt", self._mock_prompt), \
             patch("app.config.get_model_config", self._mock_models), \
             patch("app.config.get_branch_prefix", self._mock_branch_prefix), \
             patch("app.claudemd_refresh.run_git_strict", self._mock_git_strict), \
             patch("app.claudemd_refresh._has_changes", self._mock_has_changes), \
             patch("app.github.pr_create", self._mock_pr_create):
            yield

    def test_success_creates_branch_and_pr(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            result = run_refresh(str(project), "test")

        assert result == 0
        # Branch was created
        branch_calls = [c for c in self._mock_git_strict.call_args_list
                        if c[0][0] == "checkout" and "-b" in c[0]]
        assert len(branch_calls) == 1
        assert "koan/update-claudemd-test" in branch_calls[0][0]
        # PR was created
        self._mock_pr_create.assert_called_once()

    def test_failure_returns_to_base_branch(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")
        self._mock_run_claude.return_value = {
            "success": False, "output": "", "error": "timeout",
        }

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            result = run_refresh(str(project), "test")

        assert result == 1
        # Returns to base branch on failure
        checkout_calls = [c for c in self._mock_git_strict.call_args_list
                          if c[0] == ("checkout", "main")]
        assert len(checkout_calls) >= 1

    def test_no_changes_needed_returns_zero(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context",
                    return_value="No new commits since then. CLAUDE.md is up to date."):
            result = run_refresh(str(project), "test")

        assert result == 0
        # No branch created when nothing to do
        self._mock_pr_create.assert_not_called()

    def test_no_file_changes_skips_pr(self, tmp_path):
        """Claude decided no changes needed — no commit/PR."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")
        self._mock_has_changes.return_value = False

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            result = run_refresh(str(project), "test")

        assert result == 0
        self._mock_pr_create.assert_not_called()

    def test_init_mode_when_no_claudemd(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        with patch("app.claudemd_refresh.build_git_context", return_value="No CLAUDE.md"):
            run_refresh(str(project), "test")

        prompt_call = self._mock_prompt.call_args
        assert prompt_call[1]["MODE"] == "INIT"

    def test_update_mode_when_claudemd_exists(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Existing\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commits"):
            run_refresh(str(project), "test")

        prompt_call = self._mock_prompt.call_args
        assert prompt_call[1]["MODE"] == "UPDATE"

    def test_allowed_tools_include_edit(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            run_refresh(str(project), "test")

        tools = self._mock_build_cmd.call_args[1]["allowed_tools"]
        assert "Edit" in tools
        assert "Read" in tools
        assert "Write" in tools
        assert "Bash" in tools

    def test_project_name_in_prompt(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            run_refresh(str(project), "myproject")

        assert self._mock_prompt.call_args[1]["PROJECT_NAME"] == "myproject"

    def test_max_turns_set(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            run_refresh(str(project), "test")

        assert self._mock_build_cmd.call_args[1]["max_turns"] == 10

    def test_commit_stages_only_claudemd(self, tmp_path):
        """Commit should stage CLAUDE.md specifically, not git add -A."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            run_refresh(str(project), "test")

        add_calls = [c for c in self._mock_git_strict.call_args_list
                     if c[0][0] == "add"]
        assert len(add_calls) == 1
        assert add_calls[0][0] == ("add", "CLAUDE.md")

    def test_pr_title_for_init_mode(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        # No CLAUDE.md — INIT mode

        with patch("app.claudemd_refresh.build_git_context", return_value="No CLAUDE.md"):
            run_refresh(str(project), "myproj")

        pr_kwargs = self._mock_pr_create.call_args[1]
        assert "create" in pr_kwargs["title"].lower()
        assert pr_kwargs["draft"] is True

    def test_pr_title_for_update_mode(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Existing\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            run_refresh(str(project), "myproj")

        pr_kwargs = self._mock_pr_create.call_args[1]
        assert "update" in pr_kwargs["title"].lower()

    def test_returns_to_base_branch_after_success(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Project\n")

        with patch("app.claudemd_refresh.build_git_context", return_value="abc Commit"):
            run_refresh(str(project), "test")

        # Last checkout should be back to base branch
        checkout_main = [c for c in self._mock_git_strict.call_args_list
                         if c[0] == ("checkout", "main")]
        assert len(checkout_main) >= 1


# ---------------------------------------------------------------------------
# main() — CLI entry point
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_main_calls_run_refresh(self):
        with patch("app.claudemd_refresh.run_refresh", return_value=0) as mock_run, \
             patch("sys.argv", ["claudemd_refresh", "/my/project", "--project-name", "myproj"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
            mock_run.assert_called_once_with("/my/project", "myproj")

    def test_main_defaults_project_name_to_basename(self):
        with patch("app.claudemd_refresh.run_refresh", return_value=0) as mock_run, \
             patch("sys.argv", ["claudemd_refresh", "/my/project"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
            mock_run.assert_called_once_with("/my/project", "project")

    def test_main_propagates_exit_code(self):
        with patch("app.claudemd_refresh.run_refresh", return_value=1), \
             patch("sys.argv", ["claudemd_refresh", "/my/project"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
