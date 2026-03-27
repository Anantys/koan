"""Tests for git_utils.py — centralized git command helpers."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from app.git_utils import (
    get_commit_subjects,
    get_current_branch,
    ordered_remotes,
    run_git,
    run_git_strict,
)


class TestRunGit:
    """Tests for run_git() — tuple-returning variant."""

    @patch("app.git_utils.subprocess.run")
    def test_returns_tuple(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  output  \n", stderr="  warn  \n"
        )
        rc, out, err = run_git("status")
        assert rc == 0
        assert out == "output"
        assert err == "warn"

    @patch("app.git_utils.subprocess.run")
    def test_prepends_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git("log", "--oneline", "-5")
        args = mock_run.call_args[0][0]
        assert args == ["git", "log", "--oneline", "-5"]

    @patch("app.git_utils.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git("status", cwd="/some/path")
        assert mock_run.call_args[1]["cwd"] == "/some/path"

    @patch("app.git_utils.subprocess.run")
    def test_default_timeout_30(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git("status")
        assert mock_run.call_args[1]["timeout"] == 30

    @patch("app.git_utils.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git("clone", "url", timeout=120)
        assert mock_run.call_args[1]["timeout"] == 120

    @patch("app.git_utils.subprocess.run")
    def test_env_merged(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git("push", env={"GH_TOKEN": "tok123"})
        env = mock_run.call_args[1]["env"]
        assert env["GH_TOKEN"] == "tok123"
        # Original env vars should also be present
        assert "PATH" in env

    @patch("app.git_utils.subprocess.run")
    def test_no_env_passes_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git("status")
        assert mock_run.call_args[1]["env"] is None

    @patch("app.git_utils.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128, stdout="", stderr="fatal: not a repo"
        )
        rc, out, err = run_git("status")
        assert rc == 128
        assert "not a repo" in err

    @patch("app.git_utils.subprocess.run")
    def test_timeout_returns_error_tuple(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
        rc, out, err = run_git("fetch")
        assert rc == 1
        assert out == ""
        assert "timed out" in err.lower()

    @patch("app.git_utils.subprocess.run")
    def test_file_not_found_returns_error_tuple(self, mock_run):
        mock_run.side_effect = FileNotFoundError("git not found")
        rc, out, err = run_git("status")
        assert rc == 1
        assert out == ""
        assert err != ""


class TestRunGitStrict:
    """Tests for run_git_strict() — raises on failure."""

    @patch("app.git_utils.subprocess.run")
    def test_returns_stdout_on_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  abc123  \n", stderr=""
        )
        result = run_git_strict("rev-parse", "HEAD")
        assert result == "abc123"

    @patch("app.git_utils.subprocess.run")
    def test_prepends_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git_strict("fetch", "origin")
        args = mock_run.call_args[0][0]
        assert args == ["git", "fetch", "origin"]

    @patch("app.git_utils.subprocess.run")
    def test_raises_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="fatal: bad ref"
        )
        with pytest.raises(RuntimeError, match="git failed"):
            run_git_strict("checkout", "nonexistent")

    @patch("app.git_utils.subprocess.run")
    def test_error_includes_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128, stdout="", stderr="fatal error"
        )
        with pytest.raises(RuntimeError, match="git checkout bad-branch"):
            run_git_strict("checkout", "bad-branch")

    @patch("app.git_utils.subprocess.run")
    def test_default_timeout_60(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git_strict("status")
        assert mock_run.call_args[1]["timeout"] == 60

    @patch("app.git_utils.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git_strict("clone", "url", timeout=120)
        assert mock_run.call_args[1]["timeout"] == 120

    @patch("app.git_utils.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_git_strict("status", cwd="/repo")
        assert mock_run.call_args[1]["cwd"] == "/repo"

    @patch("app.git_utils.subprocess.run")
    def test_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            run_git_strict("fetch")

    @patch("app.git_utils.subprocess.run")
    def test_error_truncates_stderr(self, mock_run):
        long_err = "x" * 500
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr=long_err
        )
        with pytest.raises(RuntimeError) as exc_info:
            run_git_strict("push")
        # Error message should truncate stderr to 200 chars
        assert len(str(exc_info.value)) < 300


class TestGetCurrentBranch:
    """Tests for get_current_branch() — branch name detection."""

    @patch("app.git_utils.run_git_strict", return_value="feature/xyz")
    def test_returns_branch(self, mock):
        assert get_current_branch(cwd="/repo") == "feature/xyz"
        mock.assert_called_once_with(
            "rev-parse", "--abbrev-ref", "HEAD", cwd="/repo",
        )

    @patch("app.git_utils.run_git_strict", return_value="main")
    def test_no_cwd(self, mock):
        assert get_current_branch() == "main"
        mock.assert_called_once_with(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=None,
        )

    @patch("app.git_utils.run_git_strict", side_effect=RuntimeError("not a repo"))
    def test_runtime_error_returns_default(self, mock):
        assert get_current_branch(cwd="/bad") == "main"

    @patch("app.git_utils.run_git_strict", side_effect=OSError("no git"))
    def test_os_error_returns_default(self, mock):
        assert get_current_branch(cwd="/bad") == "main"

    @patch("app.git_utils.run_git_strict", side_effect=subprocess.SubprocessError())
    def test_subprocess_error_returns_default(self, mock):
        assert get_current_branch(cwd="/bad") == "main"

    @patch("app.git_utils.run_git_strict", side_effect=RuntimeError("err"))
    def test_custom_default(self, mock):
        assert get_current_branch(default="develop") == "develop"


class TestGetCommitSubjects:
    """Tests for get_commit_subjects() — commit subject extraction."""

    @patch("app.git_utils.run_git", return_value=(0, "fix: A\nfeat: B", ""))
    def test_returns_subjects(self, mock):
        result = get_commit_subjects(cwd="/repo")
        assert result == ["fix: A", "feat: B"]
        mock.assert_called_once_with(
            "log", "main..HEAD", "--format=%s", cwd="/repo",
        )

    @patch("app.git_utils.run_git", return_value=(0, "fix: A\nfeat: B", ""))
    def test_custom_base_branch(self, mock):
        get_commit_subjects(cwd="/repo", base_branch="develop")
        mock.assert_called_once_with(
            "log", "develop..HEAD", "--format=%s", cwd="/repo",
        )

    @patch("app.git_utils.run_git", return_value=(0, "fix: A\nfeat: B", ""))
    def test_custom_branch(self, mock):
        get_commit_subjects(cwd="/repo", branch="koan/fix")
        mock.assert_called_once_with(
            "log", "main..koan/fix", "--format=%s", cwd="/repo",
        )

    @patch("app.git_utils.run_git", return_value=(0, "", ""))
    def test_empty_output(self, mock):
        assert get_commit_subjects(cwd="/repo") == []

    @patch("app.git_utils.run_git", return_value=(0, "\n \n\n", ""))
    def test_blank_lines_filtered(self, mock):
        assert get_commit_subjects(cwd="/repo") == []

    @patch("app.git_utils.run_git", return_value=(1, "", "fatal: not a repo"))
    def test_error_returns_empty(self, mock):
        assert get_commit_subjects(cwd="/repo") == []


class TestOrderedRemotes:
    """Tests for ordered_remotes() — remote priority ordering."""

    def test_no_preferred(self):
        assert ordered_remotes() == ["origin", "upstream"]

    def test_no_preferred_explicit_none(self):
        assert ordered_remotes(None) == ["origin", "upstream"]

    def test_preferred_origin(self):
        assert ordered_remotes("origin") == ["origin", "upstream"]

    def test_preferred_upstream(self):
        assert ordered_remotes("upstream") == ["upstream", "origin"]

    def test_preferred_custom(self):
        assert ordered_remotes("fork") == ["fork", "origin", "upstream"]

    def test_preferred_empty_string(self):
        assert ordered_remotes("") == ["origin", "upstream"]
