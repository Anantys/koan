"""Tests for the /add_project core skill — clone a repo and register it."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Import handler
# ---------------------------------------------------------------------------

HANDLER_PATH = (
    Path(__file__).parent.parent / "skills" / "core" / "add_project" / "handler.py"
)


def _load_handler():
    spec = importlib.util.spec_from_file_location("add_project_handler", str(HANDLER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def ctx(tmp_path):
    """Create a SkillContext with a workspace-ready koan_root."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="add_project",
        args="",
        send_message=MagicMock(),
    )


# ===========================================================================
# _normalize_github_url
# ===========================================================================


class TestNormalizeGithubUrl:
    def test_https_url(self, handler):
        assert handler._normalize_github_url("https://github.com/owner/repo") == (
            "https://github.com/owner/repo"
        )

    def test_https_url_with_git_suffix(self, handler):
        assert handler._normalize_github_url("https://github.com/owner/repo.git") == (
            "https://github.com/owner/repo"
        )

    def test_https_url_trailing_slash(self, handler):
        assert handler._normalize_github_url("https://github.com/owner/repo/") == (
            "https://github.com/owner/repo"
        )

    def test_ssh_url(self, handler):
        assert handler._normalize_github_url("git@github.com:owner/repo.git") == (
            "https://github.com/owner/repo"
        )

    def test_ssh_url_no_git_suffix(self, handler):
        assert handler._normalize_github_url("git@github.com:owner/repo") == (
            "https://github.com/owner/repo"
        )

    def test_short_form(self, handler):
        assert handler._normalize_github_url("owner/repo") == (
            "https://github.com/owner/repo"
        )

    def test_unrecognizable_returns_none(self, handler):
        assert handler._normalize_github_url("not-a-url") is None

    def test_empty_string_returns_none(self, handler):
        assert handler._normalize_github_url("") is None

    def test_http_url(self, handler):
        assert handler._normalize_github_url("http://github.com/owner/repo") == (
            "https://github.com/owner/repo"
        )

    def test_owner_repo_with_dots(self, handler):
        assert handler._normalize_github_url("my.org/my.repo") == (
            "https://github.com/my.org/my.repo"
        )

    def test_owner_repo_with_hyphens(self, handler):
        assert handler._normalize_github_url("my-org/my-repo") == (
            "https://github.com/my-org/my-repo"
        )


# ===========================================================================
# _extract_owner_repo
# ===========================================================================


class TestExtractOwnerRepo:
    def test_valid_url(self, handler):
        assert handler._extract_owner_repo("https://github.com/sukria/koan") == (
            "sukria",
            "koan",
        )

    def test_url_with_git_suffix(self, handler):
        assert handler._extract_owner_repo("https://github.com/sukria/koan.git") == (
            "sukria",
            "koan",
        )

    def test_invalid_url(self, handler):
        assert handler._extract_owner_repo("not-a-url") == (None, None)


# ===========================================================================
# _parse_args
# ===========================================================================


class TestParseArgs:
    def test_url_only(self, handler):
        url, name = handler._parse_args("https://github.com/owner/repo")
        assert url == "https://github.com/owner/repo"
        assert name is None

    def test_url_with_name(self, handler):
        url, name = handler._parse_args("https://github.com/owner/repo myname")
        assert url == "https://github.com/owner/repo"
        assert name == "myname"

    def test_short_form(self, handler):
        url, name = handler._parse_args("owner/repo")
        assert url == "https://github.com/owner/repo"
        assert name is None

    def test_short_form_with_name(self, handler):
        url, name = handler._parse_args("owner/repo custom")
        assert url == "https://github.com/owner/repo"
        assert name == "custom"

    def test_ssh_url(self, handler):
        url, name = handler._parse_args("git@github.com:owner/repo.git")
        assert url == "https://github.com/owner/repo"
        assert name is None

    def test_invalid_url(self, handler):
        url, name = handler._parse_args("garbage")
        assert url is None
        assert name is None


# ===========================================================================
# _check_push_access
# ===========================================================================


class TestCheckPushAccess:
    @patch("app.github.run_gh", return_value="ADMIN")
    def test_admin_has_push(self, mock_gh, handler):
        assert handler._check_push_access("owner", "repo") is True

    @patch("app.github.run_gh", return_value="WRITE")
    def test_write_has_push(self, mock_gh, handler):
        assert handler._check_push_access("owner", "repo") is True

    @patch("app.github.run_gh", return_value="MAINTAIN")
    def test_maintain_has_push(self, mock_gh, handler):
        assert handler._check_push_access("owner", "repo") is True

    @patch("app.github.run_gh", return_value="READ")
    def test_read_no_push(self, mock_gh, handler):
        assert handler._check_push_access("owner", "repo") is False

    @patch("app.github.run_gh", return_value="")
    def test_empty_no_push(self, mock_gh, handler):
        assert handler._check_push_access("owner", "repo") is False

    @patch("app.github.run_gh", return_value="  write  \n")
    def test_whitespace_stripped(self, mock_gh, handler):
        assert handler._check_push_access("owner", "repo") is True


# ===========================================================================
# _get_gh_username
# ===========================================================================


class TestGetGhUsername:
    @patch("app.github.run_gh", return_value="koan-bot")
    def test_success(self, mock_gh, handler):
        assert handler._get_gh_username() == "koan-bot"

    @patch("app.github.run_gh", side_effect=RuntimeError("auth error"))
    def test_failure_returns_none(self, mock_gh, handler):
        assert handler._get_gh_username() is None


# ===========================================================================
# _create_fork_and_configure
# ===========================================================================


class TestCreateForkAndConfigure:
    @patch("app.github.run_gh")
    def test_success(self, mock_gh, handler, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        mock_gh.side_effect = [
            "",          # fork creation
            "koan-bot",  # get username
        ]

        with patch.object(handler, "run_git_strict") as mock_git:
            result = handler._create_fork_and_configure("owner", "repo", str(project_dir))

        assert result == "koan-bot/repo"
        assert mock_git.call_count == 2  # rename + add

    @patch("app.github.run_gh")
    def test_fork_already_exists_continues(self, mock_gh, handler, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        mock_gh.side_effect = [
            RuntimeError("fork already exists"),  # fork creation
            "koan-bot",                            # get username
        ]

        with patch.object(handler, "run_git_strict"):
            result = handler._create_fork_and_configure("owner", "repo", str(project_dir))

        assert result == "koan-bot/repo"

    @patch("app.github.run_gh")
    def test_fork_real_error_raises(self, mock_gh, handler, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        mock_gh.side_effect = RuntimeError("permission denied")

        with patch.object(handler, "run_git_strict"):
            with pytest.raises(RuntimeError, match="permission denied"):
                handler._create_fork_and_configure("owner", "repo", str(project_dir))

    @patch("app.github.run_gh")
    def test_no_username_raises(self, mock_gh, handler, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        mock_gh.side_effect = [
            "",    # fork creation
            None,  # get username fails (patched _get_gh_username returns None)
        ]

        with patch.object(handler, "_get_gh_username", return_value=None):
            with patch.object(handler, "run_git_strict"):
                with pytest.raises(RuntimeError, match="Cannot determine"):
                    handler._create_fork_and_configure("owner", "repo", str(project_dir))


# ===========================================================================
# handle() — main entry point
# ===========================================================================


class TestHandle:
    def test_no_args_returns_usage(self, handler, ctx):
        result = handler.handle(ctx)
        assert "Usage:" in result
        assert "/add_project" in result

    def test_invalid_url_returns_error(self, handler, ctx):
        ctx.args = "not-a-url"
        result = handler.handle(ctx)
        assert "Could not parse" in result

    def test_invalid_owner_repo_returns_error(self, handler, ctx):
        # Craft a URL that normalizes but doesn't extract owner/repo
        with patch.object(handler, "_normalize_github_url", return_value="bad://url"):
            ctx.args = "bad://url"
            result = handler.handle(ctx)
        assert "Could not extract" in result

    def test_invalid_project_name(self, handler, ctx):
        ctx.args = "owner/repo ../escape"
        result = handler.handle(ctx)
        assert "Invalid project name" in result

    def test_project_name_starting_with_dot(self, handler, ctx):
        ctx.args = "owner/repo .hidden"
        result = handler.handle(ctx)
        assert "Invalid project name" in result

    def test_project_already_exists(self, handler, ctx):
        workspace = ctx.koan_root / "workspace"
        workspace.mkdir()
        (workspace / "repo").mkdir()
        ctx.args = "owner/repo"

        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True):
            result = handler.handle(ctx)

        assert "already exists" in result

    def test_clone_failure(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_check_push_access", return_value=False), \
             patch.object(handler, "_git_clone", side_effect=RuntimeError("timeout")):
            result = handler.handle(ctx)
        assert "Clone failed" in result
        assert "timeout" in result

    def test_success_with_push_access(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        assert "Project 'repo' added" in result
        assert "owner/repo" in result
        # No fork info
        assert "Fork:" not in result

    def test_success_with_custom_name(self, handler, ctx):
        ctx.args = "owner/repo myproject"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        assert "Project 'myproject' added" in result

    def test_success_with_fork(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=False), \
             patch.object(handler, "_create_fork_and_configure", return_value="koan-bot/repo"), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        assert "Project 'repo' added" in result
        assert "Fork:" in result
        assert "koan-bot/repo" in result

    def test_fork_failure_still_adds_project(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=False), \
             patch.object(handler, "_create_fork_and_configure", side_effect=RuntimeError("fork failed")), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        # Project should still be added
        assert "Project 'repo' added" in result
        # Fork message sent via send_message
        ctx.send_message.assert_any_call("Fork creation failed: fork failed")

    def test_push_access_check_exception_treated_as_no_push(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", side_effect=Exception("network")), \
             patch.object(handler, "_create_fork_and_configure", return_value="koan-bot/repo"), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        # Should proceed as no-push → fork
        assert "Fork:" in result

    def test_sends_progress_message_push_access(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            handler.handle(ctx)

        ctx.send_message.assert_any_call(
            "Push access to owner/repo confirmed. "
            "Cloning directly (no fork needed)..."
        )

    def test_sends_progress_message_no_push_access(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=False), \
             patch.object(handler, "_create_fork_and_configure", return_value="bot/repo"), \
             patch("app.projects_merged.refresh_projects"):
            handler.handle(ctx)

        ctx.send_message.assert_any_call(
            "No push access to owner/repo. "
            "Will clone and create a personal fork..."
        )

    def test_creates_workspace_dir_if_missing(self, handler, ctx):
        ctx.args = "owner/repo"
        workspace = ctx.koan_root / "workspace"
        assert not workspace.exists()

        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            handler.handle(ctx)

        assert workspace.exists()

    def test_refresh_projects_failure_is_silent(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects", side_effect=Exception("fail")):
            result = handler.handle(ctx)

        # Should succeed despite refresh failure
        assert "Project 'repo' added" in result

    def test_ssh_url_input(self, handler, ctx):
        ctx.args = "git@github.com:sukria/koan.git"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        assert "Project 'koan' added" in result
        assert "sukria/koan" in result

    def test_default_project_name_from_repo(self, handler, ctx):
        ctx.args = "org/my-cool-project"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        assert "Project 'my-cool-project' added" in result

    def test_push_access_shows_direct_push_remotes(self, handler, ctx):
        ctx.args = "owner/repo"
        with patch.object(handler, "_git_clone"), \
             patch.object(handler, "_check_push_access", return_value=True), \
             patch("app.projects_merged.refresh_projects"):
            result = handler.handle(ctx)

        assert "origin=upstream (direct push)" in result

    def test_clone_failure_with_push_access(self, handler, ctx):
        """Clone failure is reported even when push access was confirmed."""
        ctx.args = "owner/repo"
        with patch.object(handler, "_check_push_access", return_value=True), \
             patch.object(handler, "_git_clone", side_effect=RuntimeError("timeout")):
            result = handler.handle(ctx)
        assert "Clone failed" in result
        assert "timeout" in result


# ===========================================================================
# _check_push_access_safe
# ===========================================================================


class TestCheckPushAccessSafe:
    @patch("app.github.run_gh", return_value="WRITE")
    def test_success(self, mock_gh, handler):
        assert handler._check_push_access_safe("owner", "repo") is True

    @patch("app.github.run_gh", return_value="READ")
    def test_no_access(self, mock_gh, handler):
        assert handler._check_push_access_safe("owner", "repo") is False

    @patch("app.github.run_gh", side_effect=RuntimeError("network"))
    def test_retries_on_failure(self, mock_gh, handler):
        result = handler._check_push_access_safe("owner", "repo")
        assert result is False
        # Called twice (initial + retry)
        assert mock_gh.call_count == 2

    @patch("app.github.run_gh")
    def test_succeeds_on_retry(self, mock_gh, handler):
        mock_gh.side_effect = [
            RuntimeError("timeout"),  # first attempt fails
            "ADMIN",                  # retry succeeds
        ]
        assert handler._check_push_access_safe("owner", "repo") is True


# ===========================================================================
# _git_clone
# ===========================================================================


class TestGitClone:
    def test_calls_run_git_strict(self, handler):
        with patch.object(handler, "run_git_strict") as mock_git:
            handler._git_clone("https://github.com/owner/repo.git", "/tmp/target")

        mock_git.assert_called_once_with(
            "clone", "https://github.com/owner/repo.git", "/tmp/target", timeout=120
        )

    def test_propagates_error(self, handler):
        with patch.object(handler, "run_git_strict", side_effect=RuntimeError("failed")):
            with pytest.raises(RuntimeError, match="failed"):
                handler._git_clone("url", "/tmp/t")
