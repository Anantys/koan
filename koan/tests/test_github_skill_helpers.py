"""Tests for github_skill_helpers.py — shared helpers for GitHub-related skills."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.github_skill_helpers import (
    extract_github_url,
    resolve_project_for_repo,
    queue_github_mission,
    format_project_not_found_error,
    format_success_message,
    handle_github_skill,
    _format_usage_message,
    _format_no_url_error,
)
from app.skills import SkillContext


# ---------------------------------------------------------------------------
# extract_github_url
# ---------------------------------------------------------------------------

class TestExtractGithubUrl:
    """Tests for extract_github_url()."""

    def test_pr_url_only(self):
        result = extract_github_url("https://github.com/sukria/koan/pull/42")
        assert result == ("https://github.com/sukria/koan/pull/42", None)

    def test_issue_url_only(self):
        result = extract_github_url("https://github.com/sukria/koan/issues/243")
        assert result == ("https://github.com/sukria/koan/issues/243", None)

    def test_pr_url_with_context(self):
        result = extract_github_url(
            "https://github.com/sukria/koan/pull/42 phase 1 only"
        )
        assert result == ("https://github.com/sukria/koan/pull/42", "phase 1 only")

    def test_issue_url_with_context(self):
        result = extract_github_url(
            "https://github.com/sukria/koan/issues/10 focus on auth"
        )
        assert result == ("https://github.com/sukria/koan/issues/10", "focus on auth")

    def test_url_with_fragment_stripped(self):
        # Fragment chars after the URL are captured as context by regex
        result = extract_github_url(
            "https://github.com/o/r/pull/1#issuecomment-999"
        )
        url, context = result
        assert url == "https://github.com/o/r/pull/1"
        # Fragment text becomes context since regex stops at \d+
        assert context == "#issuecomment-999"

    def test_url_with_fragment_and_context(self):
        result = extract_github_url(
            "https://github.com/o/r/issues/5#discussion extra context"
        )
        url, context = result
        assert url == "https://github.com/o/r/issues/5"
        assert "#discussion extra context" == context

    def test_no_url_returns_none(self):
        assert extract_github_url("just some text") is None

    def test_empty_string_returns_none(self):
        assert extract_github_url("") is None

    def test_url_type_pr_rejects_issue(self):
        result = extract_github_url(
            "https://github.com/o/r/issues/42", url_type="pr"
        )
        assert result is None

    def test_url_type_issue_rejects_pr(self):
        result = extract_github_url(
            "https://github.com/o/r/pull/42", url_type="issue"
        )
        assert result is None

    def test_url_type_pr_accepts_pr(self):
        result = extract_github_url(
            "https://github.com/o/r/pull/42", url_type="pr"
        )
        assert result == ("https://github.com/o/r/pull/42", None)

    def test_url_type_issue_accepts_issue(self):
        result = extract_github_url(
            "https://github.com/o/r/issues/42", url_type="issue"
        )
        assert result == ("https://github.com/o/r/issues/42", None)

    def test_url_type_pr_or_issue_accepts_both(self):
        pr = extract_github_url("https://github.com/o/r/pull/1")
        issue = extract_github_url("https://github.com/o/r/issues/1")
        assert pr is not None
        assert issue is not None

    def test_url_embedded_in_text(self):
        result = extract_github_url(
            "Please review https://github.com/o/r/pull/99 when you can"
        )
        assert result == ("https://github.com/o/r/pull/99", "when you can")

    def test_http_url(self):
        result = extract_github_url("http://github.com/o/r/pull/1")
        assert result == ("http://github.com/o/r/pull/1", None)

    def test_gitlab_mr_url(self):
        result = extract_github_url(
            "https://gitlab.com/org/sub/repo/-/merge_requests/21"
        )
        assert result == ("https://gitlab.com/org/sub/repo/-/merge_requests/21", None)

    def test_codeberg_pr_url(self):
        result = extract_github_url(
            "https://codeberg.org/acme/repo/pulls/5"
        )
        assert result == ("https://codeberg.org/acme/repo/pulls/5", None)

    def test_gitlab_issue_url(self):
        result = extract_github_url(
            "https://gitlab.com/org/sub/repo/-/issues/88"
        )
        assert result == ("https://gitlab.com/org/sub/repo/-/issues/88", None)


# ---------------------------------------------------------------------------
# resolve_project_for_repo
# ---------------------------------------------------------------------------

class TestResolveProjectForRepo:
    """Tests for resolve_project_for_repo()."""

    @patch("app.utils.resolve_project_path", return_value="/path/to/myrepo")
    @patch("app.utils.project_name_for_path", return_value="myrepo")
    def test_found(self, mock_name, mock_path):
        path, name = resolve_project_for_repo("myrepo")
        assert path == "/path/to/myrepo"
        assert name == "myrepo"

    @patch("app.utils.resolve_project_path", return_value="/p/koan")
    @patch("app.utils.project_name_for_path", return_value="koan")
    def test_with_owner(self, mock_name, mock_path):
        path, name = resolve_project_for_repo("koan", owner="sukria")
        mock_path.assert_called_once_with("koan", owner="sukria")

    @patch("app.utils.resolve_project_path", return_value=None)
    def test_not_found(self, mock_path):
        path, name = resolve_project_for_repo("unknown")
        assert path is None
        assert name is None


# ---------------------------------------------------------------------------
# queue_github_mission
# ---------------------------------------------------------------------------

class TestQueueGithubMission:
    """Tests for queue_github_mission()."""

    def _make_ctx(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance,
            command_name="review",
            args="",
        )

    @patch("app.utils.insert_pending_mission")
    def test_basic_mission(self, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path)
        queue_github_mission(
            ctx, "review", "https://github.com/o/r/pull/1", "myproject"
        )
        mock_insert.assert_called_once()
        args = mock_insert.call_args
        assert args[0][0] == ctx.instance_dir / "missions.md"
        entry = args[0][1]
        assert "[project:myproject]" in entry
        assert "/review https://github.com/o/r/pull/1" in entry

    @patch("app.utils.insert_pending_mission")
    def test_with_context(self, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path)
        queue_github_mission(
            ctx, "implement", "https://github.com/o/r/issues/5",
            "koan", context="phase 1"
        )
        entry = mock_insert.call_args[0][1]
        assert "/implement https://github.com/o/r/issues/5 phase 1" in entry
        assert "[project:koan]" in entry

    @patch("app.utils.insert_pending_mission")
    def test_no_context(self, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path)
        queue_github_mission(
            ctx, "rebase", "https://github.com/o/r/pull/10", "proj"
        )
        entry = mock_insert.call_args[0][1]
        assert entry.endswith("/rebase https://github.com/o/r/pull/10")


# ---------------------------------------------------------------------------
# format_project_not_found_error
# ---------------------------------------------------------------------------

class TestFormatProjectNotFoundError:
    """Tests for format_project_not_found_error()."""

    @patch("app.utils.get_known_projects", return_value=[("koan", "/p/k"), ("web", "/p/w")])
    def test_with_known_projects(self, mock_projects):
        msg = format_project_not_found_error("unknown-repo")
        assert "unknown-repo" in msg
        assert "koan, web" in msg
        assert "❌" in msg

    @patch("app.utils.get_known_projects", return_value=[])
    def test_no_known_projects(self, mock_projects):
        msg = format_project_not_found_error("something")
        assert "none" in msg

    @patch("app.utils.get_known_projects", return_value=[("koan", "/p/k")])
    def test_with_owner_shows_full_slug(self, mock_projects):
        msg = format_project_not_found_error("koan", owner="sukria")
        assert "sukria/koan" in msg
        assert "❌" in msg

    @patch("app.utils.get_known_projects", return_value=[("web", "/p/w")])
    def test_suggests_add_project(self, mock_projects):
        msg = format_project_not_found_error("repo", owner="org")
        assert "/add_project" in msg
        assert "org/repo" in msg

    @patch("app.utils.get_known_projects", return_value=[])
    def test_without_owner_uses_repo_only(self, mock_projects):
        msg = format_project_not_found_error("myrepo")
        assert "myrepo" in msg
        assert "/" not in msg.split("\n")[0]  # No slash in target when no owner


# ---------------------------------------------------------------------------
# format_success_message
# ---------------------------------------------------------------------------

class TestFormatSuccessMessage:
    """Tests for format_success_message()."""

    def test_pr_message(self):
        msg = format_success_message("PR", "42", "sukria", "koan")
        assert msg == "PR #42 (sukria/koan)"

    def test_issue_message(self):
        msg = format_success_message("issue", "100", "org", "repo")
        assert msg == "issue #100 (org/repo)"

    def test_with_context(self):
        msg = format_success_message("PR", "5", "o", "r", context="phase 1")
        assert msg == "PR #5 (o/r) — phase 1"

    def test_no_context(self):
        msg = format_success_message("PR", "1", "a", "b")
        assert "—" not in msg


# ---------------------------------------------------------------------------
# _format_usage_message
# ---------------------------------------------------------------------------

class TestFormatUsageMessage:
    """Tests for _format_usage_message()."""

    def test_issue_type(self):
        msg = _format_usage_message("implement", "issue")
        assert "Usage: /implement <issue>" in msg
        assert "issues/42" in msg
        assert "Queues a implement mission" in msg

    def test_pr_type(self):
        msg = _format_usage_message("rebase", "pr")
        assert "Usage: /rebase <PR>" in msg
        assert "pull/42" in msg

    def test_pr_or_issue_type(self):
        msg = _format_usage_message("review", "pr-or-issue")
        assert "Usage: /review <github-url>" in msg
        assert "pull/42" in msg
        assert "issues/42" in msg


# ---------------------------------------------------------------------------
# _format_no_url_error
# ---------------------------------------------------------------------------

class TestFormatNoUrlError:
    """Tests for _format_no_url_error()."""

    def test_issue_type(self):
        msg = _format_no_url_error("issue")
        assert "❌" in msg
        assert "issues/123" in msg

    def test_pr_type(self):
        msg = _format_no_url_error("pr")
        assert "pull/123" in msg

    def test_default_type(self):
        msg = _format_no_url_error("pr-or-issue")
        assert "pull/123" in msg


# ---------------------------------------------------------------------------
# handle_github_skill (integration-style)
# ---------------------------------------------------------------------------

class TestHandleGithubSkill:
    """Tests for handle_github_skill() — the unified skill handler."""

    def _make_ctx(self, tmp_path, args=""):
        instance = tmp_path / "instance"
        instance.mkdir(exist_ok=True)
        (instance / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance,
            command_name="review",
            args=args,
        )

    def _parse_3tuple(self, url):
        """Parse returning 3-tuple (owner, repo, number)."""
        return "sukria", "koan", "42"

    def _parse_4tuple(self, url):
        """Parse returning 4-tuple (owner, repo, type, number)."""
        return "sukria", "koan", "pull", "42"

    def test_empty_args_returns_usage(self, tmp_path):
        ctx = self._make_ctx(tmp_path, args="")
        result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_3tuple, "Review queued")
        assert "Usage:" in result

    def test_whitespace_only_args_returns_usage(self, tmp_path):
        ctx = self._make_ctx(tmp_path, args="   ")
        result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_3tuple, "Review queued")
        assert "Usage:" in result

    def test_no_url_in_args(self, tmp_path):
        ctx = self._make_ctx(tmp_path, args="just some text without url")
        result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_3tuple, "Review queued")
        assert "❌" in result
        assert "No valid GitHub URL" in result

    def test_parse_error_returns_error(self, tmp_path):
        def bad_parse(url):
            raise ValueError("Invalid URL format")

        ctx = self._make_ctx(tmp_path, args="https://github.com/o/r/pull/1")
        result = handle_github_skill(ctx, "review", "pr-or-issue", bad_parse, "Review queued")
        assert "❌" in result
        assert "Invalid URL format" in result

    @patch("app.utils.resolve_project_path", return_value=None)
    def test_project_not_found(self, mock_path, tmp_path):
        ctx = self._make_ctx(tmp_path, args="https://github.com/o/r/pull/1")
        with patch("app.utils.get_known_projects", return_value=[]):
            result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_3tuple, "Review queued")
        assert "❌" in result
        assert "Could not find local project" in result
        assert "sukria/koan" in result  # owner/repo from parse function
        assert "/add_project" in result  # actionable suggestion

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="koan")
    @patch("app.utils.resolve_project_path", return_value="/path/to/koan")
    def test_success_with_3tuple_pr(self, mock_path, mock_name, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path, args="https://github.com/sukria/koan/pull/42")
        result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_3tuple, "Review queued")
        assert "Review queued" in result
        assert "PR #42" in result
        assert "sukria/koan" in result
        mock_insert.assert_called_once()

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="koan")
    @patch("app.utils.resolve_project_path", return_value="/path/to/koan")
    def test_success_with_3tuple_issue(self, mock_path, mock_name, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path, args="https://github.com/sukria/koan/issues/10")
        result = handle_github_skill(
            ctx, "implement", "pr-or-issue", self._parse_3tuple, "Implement queued"
        )
        assert "Implement queued" in result
        assert "issue #42" in result  # type inferred from url_type in 3-tuple path

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="koan")
    @patch("app.utils.resolve_project_path", return_value="/path/to/koan")
    def test_success_with_4tuple(self, mock_path, mock_name, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path, args="https://github.com/sukria/koan/pull/42")
        result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_4tuple, "Review queued")
        assert "Review queued" in result
        assert "PR #42" in result

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="koan")
    @patch("app.utils.resolve_project_path", return_value="/path/to/koan")
    def test_success_with_context(self, mock_path, mock_name, mock_insert, tmp_path):
        ctx = self._make_ctx(
            tmp_path, args="https://github.com/sukria/koan/pull/42 phase 1 only"
        )
        result = handle_github_skill(ctx, "review", "pr-or-issue", self._parse_3tuple, "Review queued")
        assert "phase 1 only" in result
        entry = mock_insert.call_args[0][1]
        assert "phase 1 only" in entry

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="myproject")
    @patch("app.utils.resolve_project_path", return_value="/path/to/myproject")
    def test_mission_entry_format(self, mock_path, mock_name, mock_insert, tmp_path):
        ctx = self._make_ctx(tmp_path, args="https://github.com/o/r/pull/1")
        handle_github_skill(ctx, "rebase", "pr", self._parse_3tuple, "Rebase queued")
        entry = mock_insert.call_args[0][1]
        assert entry.startswith("- [project:myproject]")
        assert "/rebase https://github.com/o/r/pull/1" in entry

    def test_url_type_filtering_rejects_wrong_type(self, tmp_path):
        """Issue URL rejected when url_type is 'pr'."""
        ctx = self._make_ctx(tmp_path, args="https://github.com/o/r/issues/1")
        result = handle_github_skill(ctx, "rebase", "pr", self._parse_3tuple, "Queued")
        assert "❌" in result
        assert "No valid GitHub URL" in result

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="koan")
    @patch("app.utils.resolve_project_path", return_value="/path/to/koan")
    def test_implement_accepts_pr_url(self, mock_path, mock_name, mock_insert, tmp_path):
        """PR URLs are valid for implement — GitHub issues API works for PRs."""
        ctx = self._make_ctx(tmp_path, args="https://github.com/sukria/koan/pull/61")
        result = handle_github_skill(ctx, "implement", "pr-or-issue", self._parse_3tuple, "Implementation queued")
        assert "Implementation queued" in result
        mock_insert.assert_called_once()

    @patch("app.utils.insert_pending_mission")
    @patch("app.utils.project_name_for_path", return_value="koan")
    @patch("app.utils.resolve_project_path", return_value="/p/koan")
    def test_4tuple_issue_type(self, mock_path, mock_name, mock_insert, tmp_path):
        """4-tuple with type 'issues' labels as 'issue' not 'PR'."""
        def parse_issue_4(url):
            return "sukria", "koan", "issues", "99"

        ctx = self._make_ctx(tmp_path, args="https://github.com/sukria/koan/issues/99")
        result = handle_github_skill(ctx, "implement", "pr-or-issue", parse_issue_4, "Implement queued")
        assert "issue #99" in result
