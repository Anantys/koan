"""Tests for review skill handler — batch mode and single-PR dispatch."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from skills.core.review.handler import (
    handle,
    _parse_repo_url,
    _parse_limit,
    _list_open_prs,
    _handle_batch,
)
from app.skills import SkillContext


_HANDLER = "skills.core.review.handler"


# ---------------------------------------------------------------------------
# _parse_repo_url
# ---------------------------------------------------------------------------

class TestParseRepoUrl:
    def test_plain_repo_url(self):
        result = _parse_repo_url("https://github.com/owner/repo")
        assert result == ("https://github.com/owner/repo", "owner", "repo")

    def test_repo_url_with_dot_git(self):
        result = _parse_repo_url("https://github.com/owner/repo.git")
        assert result == ("https://github.com/owner/repo", "owner", "repo")

    def test_repo_url_with_limit(self):
        result = _parse_repo_url("https://github.com/owner/repo --limit=5")
        assert result is not None
        assert result[1] == "owner"
        assert result[2] == "repo"

    def test_issue_url_returns_none(self):
        result = _parse_repo_url("https://github.com/owner/repo/issues/42")
        assert result is None

    def test_pr_url_returns_none(self):
        result = _parse_repo_url("https://github.com/owner/repo/pull/10")
        assert result is None

    def test_no_url_returns_none(self):
        result = _parse_repo_url("just some text")
        assert result is None

    def test_rejects_sub_paths(self):
        result = _parse_repo_url("https://github.com/owner/issues")
        assert result is None

    def test_rejects_pulls_path(self):
        result = _parse_repo_url("https://github.com/owner/pull")
        assert result is None


# ---------------------------------------------------------------------------
# _parse_limit
# ---------------------------------------------------------------------------

class TestParseLimit:
    def test_limit_equals(self):
        assert _parse_limit("https://github.com/o/r --limit=5") == 5

    def test_limit_space(self):
        assert _parse_limit("https://github.com/o/r --limit 10") == 10

    def test_no_limit(self):
        assert _parse_limit("https://github.com/o/r") is None

    def test_case_insensitive(self):
        assert _parse_limit("--LIMIT=3") == 3


# ---------------------------------------------------------------------------
# _list_open_prs
# ---------------------------------------------------------------------------

class TestListOpenPrs:
    @patch("app.github.run_gh")
    def test_uses_pr_list_command(self, mock_gh):
        mock_gh.return_value = "[]"
        _list_open_prs("owner", "repo")

        args = mock_gh.call_args[0]
        assert args[0] == "pr"
        assert args[1] == "list"

    @patch("app.github.run_gh")
    def test_passes_limit(self, mock_gh):
        mock_gh.return_value = "[]"
        _list_open_prs("owner", "repo", limit=5)

        args = mock_gh.call_args[0]
        assert "--limit" in args
        limit_idx = args.index("--limit")
        assert args[limit_idx + 1] == "5"

    @patch("app.github.run_gh")
    def test_default_limit_100(self, mock_gh):
        mock_gh.return_value = "[]"
        _list_open_prs("owner", "repo")

        args = mock_gh.call_args[0]
        limit_idx = args.index("--limit")
        assert args[limit_idx + 1] == "100"

    @patch("app.github.run_gh")
    def test_empty_output_returns_empty_list(self, mock_gh):
        mock_gh.return_value = "  "
        result = _list_open_prs("owner", "repo")
        assert result == []


# ---------------------------------------------------------------------------
# _handle_batch
# ---------------------------------------------------------------------------

class TestHandleBatch:
    def _make_ctx(self, args=""):
        return SkillContext(
            koan_root=Path("/tmp/test"),
            instance_dir=Path("/tmp/test/instance"),
            command_name="review",
            args=args,
        )

    @patch(f"{_HANDLER}.queue_github_mission")
    @patch(f"{_HANDLER}._list_open_prs")
    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=("/path/to/repo", "myrepo"))
    def test_queues_all_prs(self, mock_resolve, mock_list, mock_queue):
        mock_list.return_value = [
            {"number": 1, "title": "PR one", "url": "https://github.com/o/r/pull/1"},
            {"number": 2, "title": "PR two", "url": "https://github.com/o/r/pull/2"},
            {"number": 3, "title": "PR three", "url": "https://github.com/o/r/pull/3"},
        ]
        ctx = self._make_ctx("https://github.com/o/r")
        result = _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        assert "3" in result
        assert "o/r" in result
        assert mock_queue.call_count == 3

    @patch(f"{_HANDLER}.queue_github_mission")
    @patch(f"{_HANDLER}._list_open_prs")
    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=("/path/to/repo", "myrepo"))
    def test_limit_passed_to_list(self, mock_resolve, mock_list, mock_queue):
        mock_list.return_value = [
            {"number": 1, "title": "PR one", "url": "https://github.com/o/r/pull/1"},
        ]
        ctx = self._make_ctx("https://github.com/o/r --limit=1")
        result = _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        mock_list.assert_called_once_with("o", "r", limit=1)
        assert "limited to 1" in result

    @patch(f"{_HANDLER}._list_open_prs")
    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=("/path/to/repo", "myrepo"))
    def test_no_prs_found(self, mock_resolve, mock_list):
        mock_list.return_value = []
        ctx = self._make_ctx("https://github.com/o/r")
        result = _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        assert "No open PRs" in result

    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=(None, None))
    def test_project_not_found(self, mock_resolve):
        ctx = self._make_ctx("https://github.com/o/r")
        result = _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        assert "Could not find" in result

    @patch(f"{_HANDLER}._list_open_prs", side_effect=RuntimeError("API error"))
    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=("/path", "repo"))
    def test_gh_error(self, mock_resolve, mock_list):
        ctx = self._make_ctx("https://github.com/o/r")
        result = _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        assert "Failed to list PRs" in result

    @patch(f"{_HANDLER}.queue_github_mission")
    @patch(f"{_HANDLER}._list_open_prs")
    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=("/path", "myrepo"))
    def test_pr_url_constructed_when_missing(self, mock_resolve, mock_list, mock_queue):
        """When PR dict has no 'url' key, construct it from owner/repo/number."""
        mock_list.return_value = [
            {"number": 42, "title": "Some PR"},
        ]
        ctx = self._make_ctx("https://github.com/o/r")
        _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        call_args = mock_queue.call_args
        assert "https://github.com/o/r/pull/42" in call_args[0]

    @patch(f"{_HANDLER}.queue_github_mission")
    @patch(f"{_HANDLER}._list_open_prs")
    @patch(f"{_HANDLER}.resolve_project_for_repo", return_value=("/path", "myrepo"))
    def test_queues_review_command(self, mock_resolve, mock_list, mock_queue):
        """Verify missions are queued as /review, not /fix."""
        mock_list.return_value = [
            {"number": 1, "title": "PR", "url": "https://github.com/o/r/pull/1"},
        ]
        ctx = self._make_ctx("https://github.com/o/r")
        _handle_batch(ctx, ctx.args, ("https://github.com/o/r", "o", "r"))

        call_args = mock_queue.call_args[0]
        assert call_args[1] == "review"  # command name


# ---------------------------------------------------------------------------
# handle (integration: routing)
# ---------------------------------------------------------------------------

class TestHandleRouting:
    def _make_ctx(self, args=""):
        return SkillContext(
            koan_root=Path("/tmp/test"),
            instance_dir=Path("/tmp/test/instance"),
            command_name="review",
            args=args,
        )

    @patch(f"{_HANDLER}._handle_batch")
    def test_repo_url_routes_to_batch(self, mock_batch):
        mock_batch.return_value = "Queued 5 /review missions"
        ctx = self._make_ctx("https://github.com/owner/repo")
        result = handle(ctx)

        mock_batch.assert_called_once()
        assert result == "Queued 5 /review missions"

    @patch(f"{_HANDLER}.handle_github_skill")
    def test_pr_url_routes_to_single(self, mock_single):
        mock_single.return_value = "Review queued"
        ctx = self._make_ctx("https://github.com/owner/repo/pull/42")
        result = handle(ctx)

        mock_single.assert_called_once()

    @patch(f"{_HANDLER}.handle_github_skill")
    def test_issue_url_routes_to_single(self, mock_single):
        mock_single.return_value = "Review queued"
        ctx = self._make_ctx("https://github.com/owner/repo/issues/42")
        result = handle(ctx)

        mock_single.assert_called_once()

    @patch(f"{_HANDLER}.handle_github_skill")
    def test_no_args_routes_to_single(self, mock_single):
        mock_single.return_value = "Usage: ..."
        ctx = self._make_ctx("")
        result = handle(ctx)

        mock_single.assert_called_once()

    @patch(f"{_HANDLER}._handle_batch")
    def test_repo_url_with_limit_routes_to_batch(self, mock_batch):
        mock_batch.return_value = "Queued 3 /review missions"
        ctx = self._make_ctx("https://github.com/owner/repo --limit=3")
        result = handle(ctx)

        mock_batch.assert_called_once()
