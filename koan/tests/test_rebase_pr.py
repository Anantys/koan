"""Tests for rebase_pr.py — PR rebase pipeline, URL parsing, git operations."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.claude_step import _rebase_onto_target, _run_git
from app.utils import truncate_text
from app.github_url_parser import parse_pr_url
from app.git_utils import ordered_remotes
from app.rebase_pr import (
    fetch_pr_context,
    build_comment_summary,
    run_rebase,
    _apply_review_feedback,
    _build_ci_fix_prompt,
    _build_rebase_comment,
    _build_rebase_prompt,
    _checkout_pr_branch,
    _find_remote_for_repo,
    _get_conflicted_files,
    _get_current_branch,
    _is_conflict_failure,
    _push_with_fallback,
    _rebase_with_conflict_resolution,
    _check_pr_state,
    _run_ci_check_and_fix,
    _safe_checkout,
    _UNMERGED_STATUSES,
    MAX_CI_FIX_ATTEMPTS,
)
from app.claude_step import _is_permission_error, wait_for_ci


# ---------------------------------------------------------------------------
# parse_pr_url (from pr_review)
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_standard_url(self):
        owner, repo, num = parse_pr_url("https://github.com/sukria/koan/pull/29")
        assert owner == "sukria"
        assert repo == "koan"
        assert num == "29"

    def test_url_with_fragment(self):
        owner, repo, num = parse_pr_url(
            "https://github.com/sukria/koan/pull/29#pullrequestreview-123"
        )
        assert num == "29"

    def test_url_with_trailing_whitespace(self):
        owner, repo, num = parse_pr_url("  https://github.com/foo/bar/pull/1  ")
        assert owner == "foo"
        assert repo == "bar"

    def test_http_url(self):
        owner, repo, num = parse_pr_url("http://github.com/a/b/pull/99")
        assert owner == "a"
        assert num == "99"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("https://github.com/sukria/koan/issues/29")

    def test_not_github_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("https://gitlab.com/sukria/koan/pull/29")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("")


# ---------------------------------------------------------------------------
# truncate_text (shared utility)
# ---------------------------------------------------------------------------

class TestTruncateText:
    def test_short_text_unchanged(self):
        assert truncate_text("hello", 100) == "hello"

    def test_exact_length_unchanged(self):
        assert truncate_text("12345", 5) == "12345"

    def test_long_text_truncated(self):
        result = truncate_text("a" * 20, 10)
        assert len(result) < 30
        assert "truncated" in result

    def test_empty_string(self):
        assert truncate_text("", 100) == ""


# ---------------------------------------------------------------------------
# _run_git (local helper)
# ---------------------------------------------------------------------------

class TestRunGit:
    def test_returns_stdout_stripped(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  main  "
        with patch("app.claude_step.subprocess.run", return_value=mock_result):
            assert _run_git(["git", "status"]) == "main"

    def test_raises_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        with patch("app.claude_step.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="git failed"):
                _run_git(["git", "checkout", "foo"])

    def test_passes_cwd(self):
        mock_result = MagicMock(returncode=0, stdout="ok")
        with patch("app.claude_step.subprocess.run", return_value=mock_result) as mock_run:
            _run_git(["git", "status"], cwd="/project")
            mock_run.assert_called_once()
            assert mock_run.call_args.kwargs.get("cwd") == "/project"


# ---------------------------------------------------------------------------
# _get_current_branch
# ---------------------------------------------------------------------------

class TestGetCurrentBranch:
    def test_returns_branch_name(self):
        with patch("app.claude_step._git_utils_get_current_branch", return_value="koan/my-feature"):
            assert _get_current_branch("/project") == "koan/my-feature"

    def test_fallback_on_error(self):
        with patch("app.claude_step._git_utils_get_current_branch", return_value="main"):
            assert _get_current_branch("/project") == "main"


# ---------------------------------------------------------------------------
# _checkout_pr_branch
# ---------------------------------------------------------------------------

class TestCheckoutPrBranch:
    def test_checkout_uses_dash_B_flag(self):
        """Should fetch and use -B to create/reset the local branch."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _checkout_pr_branch("koan/fix", "/project")

        assert result == "origin"
        cmds = [c[:3] for c in calls]
        assert ["git", "fetch", "origin"] in cmds
        # Must use -B, not -b or plain checkout
        checkout_cmds = [c for c in calls if "checkout" in c]
        assert len(checkout_cmds) == 1
        assert "-B" in checkout_cmds[0]
        assert "origin/koan/fix" in checkout_cmds[0]

    def test_resets_existing_local_branch(self):
        """A stale local branch with the same name must not block checkout."""
        # -B handles this — create or reset. Verify no "branch already exists" error.
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            _checkout_pr_branch("koan/fix", "/project")

        checkout_cmds = [c for c in calls if "checkout" in c]
        # Only ONE checkout call expected — -B handles both cases
        assert len(checkout_cmds) == 1
        assert "-B" in checkout_cmds[0]

    def test_falls_back_to_upstream(self):
        """If origin fetch fails, tries upstream and returns 'upstream'."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock(returncode=0, stdout="", stderr="")
            # origin fetch fails
            if cmd[:3] == ["git", "fetch", "origin"]:
                raise RuntimeError("remote ref not found")
            return result

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _checkout_pr_branch("feat/upstream-only", "/project")

        assert result == "upstream"
        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        assert ["git", "fetch", "origin", "feat/upstream-only"] in fetch_cmds
        assert ["git", "fetch", "upstream", "feat/upstream-only"] in fetch_cmds

        # Checkout should use upstream, not origin
        checkout_cmds = [c for c in calls if "checkout" in c]
        assert len(checkout_cmds) == 1
        assert "upstream/feat/upstream-only" in checkout_cmds[0]

    def test_raises_if_all_remotes_fail(self):
        """If all remotes fail and no fork info, raises RuntimeError."""
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                raise RuntimeError("remote ref not found")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            with pytest.raises(RuntimeError, match="not found on"):
                _checkout_pr_branch("nonexistent", "/project")

    def test_tries_head_remote_first(self):
        """When head_remote is given, it should be tried before origin."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _checkout_pr_branch(
                "feat/branch", "/project", head_remote="myfork",
            )

        assert result == "myfork"
        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        # head_remote should be tried first
        assert fetch_cmds[0] == ["git", "fetch", "myfork", "feat/branch"]

    def test_adds_fork_remote_when_no_match(self):
        """When branch not found on any known remote, adds fork remote."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            # All standard remotes fail for fetch
            if cmd[:2] == ["git", "fetch"] and cmd[2] in ("origin", "upstream"):
                raise RuntimeError("remote ref not found")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _checkout_pr_branch(
                "feat/fix", "/project",
                head_owner="someuser", repo="somerepo",
            )

        assert result == "fork-someuser"
        # Should have added the remote
        add_cmds = [c for c in calls if "remote" in c and "add" in c]
        assert len(add_cmds) == 1
        assert "fork-someuser" in add_cmds[0]
        assert "https://github.com/someuser/somerepo.git" in add_cmds[0]
        # Should have fetched from the fork remote
        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        fork_fetches = [c for c in fetch_cmds if c[2] == "fork-someuser"]
        assert len(fork_fetches) == 1


# ---------------------------------------------------------------------------
# _get_conflicted_files
# ---------------------------------------------------------------------------

class TestGetConflictedFiles:
    """Verify _get_conflicted_files uses git status --porcelain to detect unmerged entries."""

    def test_detects_uu_conflict(self):
        """UU (both modified) is the most common conflict type."""
        mock_result = MagicMock(
            stdout="UU file_a.txt\nM  file_b.txt\n",
            returncode=0,
        )
        with patch("app.rebase_pr.subprocess.run", return_value=mock_result) as mock_run:
            files = _get_conflicted_files("/project")
            assert files == ["file_a.txt"]
            # Verify stdin=subprocess.DEVNULL is passed
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs.get("stdin") == subprocess.DEVNULL

    def test_detects_multiple_conflict_types(self):
        """All unmerged status codes are detected (UU, AA, DU, UD, AU, UA, DD)."""
        mock_result = MagicMock(
            stdout=(
                "UU both_modified.py\n"
                "AA both_added.py\n"
                "DU deleted_by_us.py\n"
                "UD deleted_by_them.py\n"
                "AU added_by_us.py\n"
                "UA added_by_them.py\n"
                "DD both_deleted.py\n"
                "M  cleanly_staged.py\n"
                " M unstaged.py\n"
            ),
            returncode=0,
        )
        with patch("app.rebase_pr.subprocess.run", return_value=mock_result):
            files = _get_conflicted_files("/project")
            assert files == [
                "both_modified.py",
                "both_added.py",
                "deleted_by_us.py",
                "deleted_by_them.py",
                "added_by_us.py",
                "added_by_them.py",
                "both_deleted.py",
            ]

    def test_no_conflicts_returns_empty(self):
        """When no unmerged entries exist, returns empty list."""
        mock_result = MagicMock(
            stdout="M  staged.py\n M unstaged.py\n?? untracked.py\n",
            returncode=0,
        )
        with patch("app.rebase_pr.subprocess.run", return_value=mock_result):
            assert _get_conflicted_files("/project") == []

    def test_empty_output_returns_empty(self):
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("app.rebase_pr.subprocess.run", return_value=mock_result):
            assert _get_conflicted_files("/project") == []

    def test_exception_returns_empty(self):
        with patch("app.rebase_pr.subprocess.run", side_effect=OSError("fail")):
            assert _get_conflicted_files("/project") == []

    def test_paths_with_spaces(self):
        mock_result = MagicMock(
            stdout="UU path with spaces/file.txt\n",
            returncode=0,
        )
        with patch("app.rebase_pr.subprocess.run", return_value=mock_result):
            files = _get_conflicted_files("/project")
            assert files == ["path with spaces/file.txt"]

    def test_unmerged_statuses_constant_covers_all_types(self):
        """The frozen set covers all git unmerged status codes."""
        assert _UNMERGED_STATUSES == {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


# ---------------------------------------------------------------------------
# _rebase_onto_target (local helper)
# ---------------------------------------------------------------------------

class TestRebaseOntoTarget:
    def test_successful_rebase_on_origin(self):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step.subprocess.run", return_value=mock_result):
            result = _rebase_onto_target("main", "/project")
            assert result == "origin"

    def test_falls_back_to_upstream(self):
        def mock_run(cmd, **kwargs):
            result = MagicMock(returncode=0, stdout="", stderr="")
            if "origin" in cmd and "fetch" in cmd:
                raise RuntimeError("fetch failed")
            return result

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target("main", "/project")
            assert result == "upstream"

    def test_returns_none_on_conflict(self):
        def mock_run(cmd, **kwargs):
            if "rebase" in cmd and "--abort" not in cmd:
                raise RuntimeError("conflict")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target("main", "/project")
            assert result is None


# ---------------------------------------------------------------------------
# _is_permission_error
# ---------------------------------------------------------------------------

class TestIsPermissionError:
    def test_permission_denied(self):
        assert _is_permission_error("permission denied") is True

    def test_forbidden_403(self):
        assert _is_permission_error("HTTP 403: Forbidden") is True

    def test_protected_branch(self):
        assert _is_permission_error("protected branch") is True

    def test_auth_failed(self):
        assert _is_permission_error("authentication failed for url") is True

    def test_normal_error_not_permission(self):
        assert _is_permission_error("fatal: remote ref does not exist") is False

    def test_empty_string(self):
        assert _is_permission_error("") is False


# ---------------------------------------------------------------------------
# _safe_checkout
# ---------------------------------------------------------------------------

class TestSafeCheckout:
    def test_succeeds_silently(self):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step.subprocess.run", return_value=mock_result):
            _safe_checkout("main", "/project")

    def test_fails_silently(self):
        with patch("app.claude_step.subprocess.run", side_effect=RuntimeError("oops")):
            _safe_checkout("main", "/project")  # Should not raise


# ---------------------------------------------------------------------------
# _find_remote_for_repo / ordered_remotes
# ---------------------------------------------------------------------------

class TestFindRemoteForRepo:
    """Test matching a GitHub owner/repo to a local git remote."""

    @patch("app.rebase_pr.subprocess.run")
    def test_finds_origin_https(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "origin\thttps://github.com/atoomic/Crypt-OpenSSL-RSA.git (fetch)\n"
                "origin\thttps://github.com/atoomic/Crypt-OpenSSL-RSA.git (push)\n"
                "upstream\thttps://github.com/cpan-authors/Crypt-OpenSSL-RSA.git (fetch)\n"
                "upstream\thttps://github.com/cpan-authors/Crypt-OpenSSL-RSA.git (push)\n"
            ),
        )
        assert _find_remote_for_repo(
            "cpan-authors", "Crypt-OpenSSL-RSA", "/tmp/project"
        ) == "upstream"

    @patch("app.rebase_pr.subprocess.run")
    def test_finds_origin_ssh(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "origin\tgit@github.com:owner/repo.git (fetch)\n"
                "origin\tgit@github.com:owner/repo.git (push)\n"
            ),
        )
        assert _find_remote_for_repo("owner", "repo", "/tmp/p") == "origin"

    @patch("app.rebase_pr.subprocess.run")
    def test_case_insensitive(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="upstream\thttps://github.com/OWNER/REPO.git (fetch)\n",
        )
        assert _find_remote_for_repo("owner", "repo", "/tmp/p") == "upstream"

    @patch("app.rebase_pr.subprocess.run")
    def test_no_match_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="origin\thttps://github.com/other/repo.git (fetch)\n",
        )
        assert _find_remote_for_repo("owner", "repo", "/tmp/p") is None

    @patch("app.rebase_pr.subprocess.run")
    def test_subprocess_failure_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _find_remote_for_repo("o", "r", "/tmp/p") is None


class TestOrderedRemotes:
    """Test remote ordering with preferred remote."""

    def test_no_preferred(self):
        assert ordered_remotes(None) == ["origin", "upstream"]

    def test_preferred_origin(self):
        # origin already in default list — should be first, no duplicate
        assert ordered_remotes("origin") == ["origin", "upstream"]

    def test_preferred_upstream(self):
        assert ordered_remotes("upstream") == ["upstream", "origin"]

    def test_preferred_custom(self):
        assert ordered_remotes("fork") == ["fork", "origin", "upstream"]


# ---------------------------------------------------------------------------
# build_comment_summary
# ---------------------------------------------------------------------------

class TestBuildCommentSummary:
    def test_with_reviews_and_comments(self):
        context = {
            "reviews": "@alice (APPROVED): LGTM",
            "review_comments": "[file.py:10] @bob: Fix this",
            "issue_comments": "@carol: Can we also handle edge case?",
        }
        result = build_comment_summary(context)
        assert "Reviews" in result
        assert "alice" in result
        assert "Inline Comments" in result
        assert "bob" in result
        assert "Discussion" in result
        assert "carol" in result

    def test_no_comments(self):
        context = {"reviews": "", "review_comments": "", "issue_comments": ""}
        result = build_comment_summary(context)
        assert "No comments" in result

    def test_partial_comments(self):
        context = {"reviews": "some review", "review_comments": "", "issue_comments": ""}
        result = build_comment_summary(context)
        assert "Reviews" in result
        assert "Inline" not in result


# ---------------------------------------------------------------------------
# _build_rebase_comment
# ---------------------------------------------------------------------------

class TestBuildRebaseComment:
    def test_basic_comment(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Force-pushed"],
            {"title": "Fix bug"},
        )
        assert "## Rebase: Fix bug" in result
        assert "`koan/fix`" in result
        assert "`main`" in result
        assert "Rebased" in result
        assert "Kōan" in result

    def test_empty_actions(self):
        result = _build_rebase_comment(
            "1", "br", "main", [],
            {"title": "PR"},
        )
        assert "no additional changes needed" in result

    def test_diffstat_included(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            diffstat="3 files changed, 15 insertions(+), 5 deletions(-)",
        )
        assert "3 files changed" in result
        assert "**Diff**" in result

    def test_no_diffstat_when_empty(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            diffstat="",
        )
        assert "**Diff**" not in result

    def test_review_feedback_noted(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Applied review feedback"],
            {"title": "Fix bug", "review_comments": "please fix the typo"},
        )
        assert "Review feedback was analyzed and applied" in result

    def test_mechanical_actions_filtered(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Read PR comments and review feedback", "Rebased", "Commented on PR"],
            {"title": "Fix bug"},
        )
        assert "Read PR comments" not in result
        assert "Commented on PR" not in result
        assert "Rebased" in result


# ---------------------------------------------------------------------------
# fetch_pr_context
# ---------------------------------------------------------------------------

class TestFetchPrContext:
    @patch("app.github.subprocess.run")
    def test_parses_pr_metadata(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "Fix auth",
                "body": "Fixes #42",
                "headRefName": "koan/fix-auth",
                "baseRefName": "main",
                "state": "OPEN",
                "author": {"login": "sukria"},
                "url": "https://github.com/sukria/koan/pull/42",
            })),
            MagicMock(returncode=0, stdout="1"),  # review_comments count
            MagicMock(returncode=0, stdout="+added line"),
            MagicMock(returncode=0, stdout="[auth.py:10] @reviewer: Fix this"),
            MagicMock(returncode=0, stdout="@reviewer (CHANGES_REQUESTED): Please fix"),
            MagicMock(returncode=0, stdout="@sukria: Will do"),
        ]

        context = fetch_pr_context("sukria", "koan", "42")
        assert context["title"] == "Fix auth"
        assert context["branch"] == "koan/fix-auth"
        assert context["base"] == "main"
        assert context["state"] == "OPEN"
        assert context["author"] == "sukria"
        assert context["diff"] == "+added line"
        assert "Fix this" in context["review_comments"]
        assert "Please fix" in context["reviews"]
        assert "Will do" in context["issue_comments"]
        assert context["has_pending_reviews"] is False  # comments fetched OK

    @patch("app.github.subprocess.run")
    def test_handles_empty_responses(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({"title": "T", "headRefName": "br"})),
            MagicMock(returncode=0, stdout="0"),  # review_comments count
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["branch"] == "br"
        assert context["diff"] == ""
        assert context["review_comments"] == ""
        assert context["has_pending_reviews"] is False

    @patch("app.github.subprocess.run")
    def test_handles_invalid_json(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="not json"),
            MagicMock(returncode=0, stdout="0"),  # review_comments count
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["title"] == ""
        assert context["base"] == "main"

    @patch("app.github.subprocess.run")
    def test_diff_fetch_failure_graceful(self, mock_run):
        """Large PR diffs (HTTP 406) should not crash the entire fetch."""
        mock_run.side_effect = [
            # Metadata succeeds
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "Big PR",
                "headRefName": "feat/big",
                "baseRefName": "main",
                "state": "OPEN",
                "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),  # review_comments count
            # Diff fails (HTTP 406 — too large)
            MagicMock(returncode=1, stderr="HTTP 406: diff exceeded maximum"),
            # Comments succeed
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["title"] == "Big PR"
        assert context["branch"] == "feat/big"
        assert context["diff"] == ""  # Graceful fallback

    @patch("app.github.subprocess.run")
    def test_comments_fetch_failure_graceful(self, mock_run):
        """API failures on comments should not crash the fetch."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),  # review_comments count
            MagicMock(returncode=0, stdout="+diff"),
            # All comment APIs fail
            MagicMock(returncode=1, stderr="rate limited"),
            MagicMock(returncode=1, stderr="rate limited"),
            MagicMock(returncode=1, stderr="rate limited"),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["branch"] == "br"
        assert context["diff"] == "+diff"
        assert context["review_comments"] == ""
        assert context["reviews"] == ""
        assert context["issue_comments"] == ""

    @patch("app.github.subprocess.run")
    def test_detects_pending_reviews(self, mock_run):
        """Detect when GitHub reports review comments but API returns empty."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="2"),  # API says 2 review comments
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(returncode=0, stdout=""),    # but comments endpoint returns empty
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["has_pending_reviews"] is True

    @patch("app.github.subprocess.run")
    def test_no_pending_reviews_when_comments_fetched(self, mock_run):
        """No pending flag when review comments are successfully fetched."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="1"),  # API says 1 review comment
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(returncode=0, stdout="[file.py:10] @reviewer: Fix this"),  # fetched OK
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["has_pending_reviews"] is False

    @patch("app.rebase_pr.time.sleep")
    @patch("app.github.subprocess.run")
    def test_pending_review_count_fetch_failure_graceful(self, mock_run, mock_sleep):
        """If the review_comments count fetch fails twice, assume no pending reviews."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=1, stderr="rate limited"),  # count fetch fails (attempt 1)
            MagicMock(returncode=1, stderr="rate limited"),  # count fetch fails (retry)
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["has_pending_reviews"] is False
        mock_sleep.assert_called_once_with(2)

    @patch("app.rebase_pr.time.sleep")
    @patch("app.github.subprocess.run")
    def test_pending_review_count_retry_succeeds(self, mock_run, mock_sleep):
        """If count fetch fails once but retry succeeds, use the retried value."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=1, stderr="transient error"),  # count fetch fails
            MagicMock(returncode=0, stdout="2"),                # retry succeeds
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(returncode=0, stdout=""),  # comments endpoint returns empty
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["has_pending_reviews"] is True
        mock_sleep.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# _push_with_fallback
# ---------------------------------------------------------------------------

class TestPushWithFallback:
    def test_successful_force_with_lease(self):
        """Happy path: force-with-lease on origin succeeds."""
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step.subprocess.run", return_value=mock_result):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project"
            )
            assert result["success"] is True
            assert any("Force-pushed" in a for a in result["actions"])
            assert any("origin" in a for a in result["actions"])

    def test_falls_back_to_plain_force_on_origin(self):
        """If force-with-lease fails on origin, tries plain --force on origin."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if "--force-with-lease" in cmd:
                raise RuntimeError("stale tracking ref")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project"
            )
            assert result["success"] is True
            push_cmds = [c for c in calls if c[:2] == ["git", "push"]]
            assert any("--force" in c and "--force-with-lease" not in c for c in push_cmds)

    def test_falls_back_to_upstream(self):
        """If both origin push strategies fail, tries upstream."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["git", "push"] and "origin" in cmd:
                raise RuntimeError("permission denied")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project"
            )
            assert result["success"] is True
            assert any("upstream" in a for a in result["actions"])

    def test_never_creates_new_pr(self):
        """When all pushes fail, should fail — NOT create a new branch/PR."""
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"]:
                raise RuntimeError("permission denied on all remotes")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project"
            )
            assert result["success"] is False
            assert "all remotes rejected" in result["error"]
            # Must NOT contain any "new branch" or "draft PR" actions
            assert not any("new branch" in a.lower() for a in result["actions"])
            assert not any("draft PR" in a for a in result["actions"])

    def test_all_remotes_fail_returns_error(self):
        """Comprehensive failure: all 4 push attempts (2 remotes x 2 strategies) fail."""
        push_count = [0]
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"]:
                push_count[0] += 1
                raise RuntimeError("rejected")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project"
            )
            assert result["success"] is False
            assert push_count[0] == 4  # 2 remotes x 2 strategies


# ---------------------------------------------------------------------------
# run_rebase — integration tests
# ---------------------------------------------------------------------------

class TestRunRebase:
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_successful_rebase(self, mock_ctx, mock_git, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "Fix auth",
            "body": "Fix",
            "branch": "koan/fix-auth",
            "base": "main",
            "state": "OPEN",
            "author": "sukria",
            "url": "https://...",
            "diff": "",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
        }
        mock_git.return_value = "ok"
        notify = MagicMock()

        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed `koan/fix-auth`"], "error": ""
             }):
            success, summary = run_rebase(
                "sukria", "koan", "42", "/project", notify_fn=notify
            )
            assert success is True
            assert "Rebased" in summary

    @patch("app.rebase_pr.fetch_pr_context")
    def test_fetch_failure(self, mock_ctx):
        mock_ctx.side_effect = RuntimeError("network error")
        notify = MagicMock()
        success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
        assert success is False
        assert "Failed to fetch" in summary

    @patch("app.rebase_pr.fetch_pr_context")
    def test_skip_merged_pr(self, mock_ctx):
        """Rebase should skip and succeed when the PR is already merged."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "MERGED", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
        assert success is True
        assert "merged" in summary.lower()

    @patch("app.rebase_pr.fetch_pr_context")
    def test_skip_closed_pr(self, mock_ctx):
        """Rebase should skip and succeed when the PR is closed."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "CLOSED", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
        assert success is True
        assert "closed" in summary.lower()

    @patch("app.rebase_pr.fetch_pr_context")
    def test_missing_branch(self, mock_ctx):
        mock_ctx.return_value = {"branch": "", "base": "main", "title": "T",
                                  "body": "", "state": "", "author": "", "url": "",
                                  "diff": "", "review_comments": "", "reviews": "", "issue_comments": ""}
        notify = MagicMock()
        success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
        assert success is False
        assert "branch name" in summary.lower()

    @patch("app.rebase_pr.fetch_pr_context")
    def test_checkout_failure(self, mock_ctx):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch", side_effect=RuntimeError("no such branch")):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
            assert success is False
            assert "checkout" in summary.lower()

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_rebase_conflict_restores_branch(self, mock_ctx, mock_safe):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="original"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value=None):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
            assert success is False
            assert "conflict" in summary.lower()
            mock_safe.assert_called_with("original", "/p")

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_comment_failure_non_fatal(self, mock_ctx, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        mock_gh.side_effect = RuntimeError("no perms to comment")
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
            assert success is True
            assert "Comment failed" in summary

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr._checkout_pr_branch")
    @patch("app.rebase_pr._rebase_with_conflict_resolution")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_warns_on_pending_reviews(self, mock_ctx, mock_rebase, mock_checkout, mock_safe):
        """Rebase should warn but proceed when pending reviews are detected."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
            "has_pending_reviews": True,
        }
        mock_checkout.return_value = "origin"
        mock_rebase.return_value = None  # rebase fails (not the point of this test)
        notify = MagicMock()
        success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
        # Should have warned via notify_fn about pending reviews
        pending_calls = [c for c in notify.call_args_list if "pending" in str(c).lower()]
        assert len(pending_calls) >= 1
        # Should NOT have aborted — it proceeded to the rebase step
        mock_checkout.assert_called_once()

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_logs_comments_read(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "@reviewer (CHANGES_REQUESTED): please fix",
            "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)
            assert success is True
            assert "comments" in summary.lower()
            # Claude step should be called when feedback exists
            mock_apply.assert_called_once()

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_restores_branch_after_success(self, mock_ctx, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="original"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            run_rebase("o", "r", "1", "/p", notify_fn=notify)
            mock_safe.assert_called_with("original", "/p")

    @patch("app.rebase_pr.fetch_pr_context")
    def test_default_notify_fn(self, mock_ctx):
        """When no notify_fn provided, defaults to send_telegram."""
        mock_ctx.return_value = {"branch": "", "base": "main", "title": "",
                                  "body": "", "state": "", "author": "", "url": "",
                                  "diff": "", "review_comments": "", "reviews": "", "issue_comments": ""}
        with patch("app.notify.send_telegram") as mock_tg:
            success, _ = run_rebase("o", "r", "1", "/p")
            mock_tg.assert_called()

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_passes_preferred_remote_to_rebase(self, mock_ctx, mock_gh, mock_safe, mock_ci_check):
        """run_rebase must determine the correct base remote and pass it through."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "koan/fix",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._find_remote_for_repo", return_value="upstream") as mock_find, \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="upstream") as mock_rebase, \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            run_rebase("cpan-authors", "Crypt-OpenSSL-RSA", "87", "/p", notify_fn=notify)
            mock_find.assert_called_once_with("cpan-authors", "Crypt-OpenSSL-RSA", "/p")
            mock_rebase.assert_called_once()
            # Verify preferred_remote kwarg was passed
            _, kwargs = mock_rebase.call_args
            assert kwargs.get("preferred_remote") == "upstream"


# ---------------------------------------------------------------------------
# _push_with_fallback — cross-linking
# ---------------------------------------------------------------------------

class TestPushBranchRecycling:
    def test_reuses_same_branch_name(self):
        """Push must always target the original branch name, never create a new one."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project"
            )
            assert result["success"] is True
            push_cmds = [c for c in calls if c[:2] == ["git", "push"]]
            # All push commands must target the original branch name
            for push_cmd in push_cmds:
                assert "koan/fix" in push_cmd
                # Must NOT create a new branch with a different name
                assert "-b" not in push_cmd
                assert "-u" not in push_cmd


# ---------------------------------------------------------------------------
# _build_rebase_prompt
# ---------------------------------------------------------------------------

REBASE_SKILL_DIR = Path(__file__).parent.parent / "skills" / "core" / "rebase"


class TestBuildRebasePrompt:
    def test_builds_prompt_with_skill_dir(self):
        context = {
            "title": "Fix auth",
            "body": "Fixes a bug",
            "branch": "koan/fix-auth",
            "base": "main",
            "diff": "+some code",
            "review_comments": "@reviewer: fix this",
            "reviews": "@reviewer (CHANGES_REQUESTED): Please fix",
            "issue_comments": "@author: will do",
        }
        prompt = _build_rebase_prompt(context, skill_dir=REBASE_SKILL_DIR)
        assert "Fix auth" in prompt
        assert "koan/fix-auth" in prompt
        assert "+some code" in prompt
        assert "fix this" in prompt
        assert "Please fix" in prompt

    def test_prompt_without_skill_dir_falls_back(self):
        """Without skill_dir, falls back to system-prompts directory."""
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        # This will raise FileNotFoundError since rebase.md doesn't exist
        # in system-prompts/, which is expected — the skill_dir path is
        # the intended usage
        with pytest.raises(FileNotFoundError):
            _build_rebase_prompt(context, skill_dir=None)


# ---------------------------------------------------------------------------
# _apply_review_feedback
# ---------------------------------------------------------------------------

class TestApplyReviewFeedback:
    @patch("app.rebase_pr.commit_if_changes", return_value=True)
    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_invokes_claude_and_commits(self, _mc, _cmd, mock_claude, mock_commit):
        mock_claude.return_value = {
            "success": True, "output": "Changed things.", "error": "",
        }
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
        )
        mock_claude.assert_called_once()
        mock_commit.assert_called_once()
        commit_msg = mock_commit.call_args[0][1]
        assert "rebase: apply review feedback on #42" in commit_msg

    @patch("app.rebase_pr.commit_if_changes", return_value=True)
    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_logs_success(self, _mc, _cmd, mock_claude, mock_commit):
        mock_claude.return_value = {
            "success": True, "output": "Applied changes.", "error": "",
        }
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
        )
        assert "Applied review feedback" in actions


# ---------------------------------------------------------------------------
# run_rebase — Claude step integration
# ---------------------------------------------------------------------------

class TestRunRebaseClaude:
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_step_called_with_feedback(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, _ = run_rebase("o", "r", "1", "/p", notify_fn=notify,
                                     skill_dir=REBASE_SKILL_DIR)
            assert success is True
            mock_apply.assert_called_once()

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_step_skipped_without_feedback(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "",
            "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, _ = run_rebase("o", "r", "1", "/p", notify_fn=notify)
            assert success is True
            mock_apply.assert_not_called()

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_skill_dir_passed_to_apply(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "feedback",
            "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            run_rebase("o", "r", "1", "/p", notify_fn=notify,
                       skill_dir=REBASE_SKILL_DIR)
            call_kwargs = mock_apply.call_args
            assert call_kwargs[1].get("skill_dir") == REBASE_SKILL_DIR

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_branch_switch_restored_after_feedback(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check
    ):
        """If Claude switches branches during feedback, we restore the PR branch."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()

        # _get_current_branch returns different values:
        # 1st call: "main" (original branch before checkout)
        # 2nd call: "koan/some-branch" (Claude switched during feedback)
        branch_calls = iter(["main", "koan/some-branch"])
        with patch("app.rebase_pr._get_current_branch", side_effect=branch_calls), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify,
                                          skill_dir=REBASE_SKILL_DIR)
            assert success is True
            # _safe_checkout should be called to restore the PR branch
            # (once for restoration + once at end for original branch)
            checkout_calls = [c[0][0] for c in mock_safe.call_args_list]
            assert "feat" in checkout_calls  # restored to PR branch

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_stays_on_branch_no_restore(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check
    ):
        """If Claude stays on the correct branch, no extra checkout happens."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()

        # _get_current_branch returns "feat" after feedback (stayed on branch)
        branch_calls = iter(["main", "feat"])
        with patch("app.rebase_pr._get_current_branch", side_effect=branch_calls), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify,
                                          skill_dir=REBASE_SKILL_DIR)
            assert success is True
            # _safe_checkout should only be called at the end (original branch)
            # NOT for branch restoration since Claude stayed on correct branch
            restore_calls = [
                c for c in mock_safe.call_args_list
                if c[0][0] == "feat"
            ]
            assert len(restore_calls) == 0  # no restoration needed


# ---------------------------------------------------------------------------
# main() CLI entry point
# ---------------------------------------------------------------------------

from app.rebase_pr import main as rebase_main


class TestMain:
    def test_main_success(self):
        with patch("app.rebase_pr.run_rebase", return_value=(True, "Rebased OK")):
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 0

    def test_main_failure(self):
        with patch("app.rebase_pr.run_rebase", return_value=(False, "Conflict")):
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 1

    def test_main_invalid_url(self):
        code = rebase_main(["not-a-url", "--project-path", "/p"])
        assert code == 1

    def test_main_skill_dir_resolved(self):
        """Verify skill_dir is correctly computed relative to rebase_pr.py."""
        with patch("app.rebase_pr.run_rebase", return_value=(True, "OK")) as mock_rebase:
            rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            call_kwargs = mock_rebase.call_args
            skill_dir = call_kwargs[1].get("skill_dir")
            assert skill_dir is not None
            assert str(skill_dir).endswith("skills/core/rebase")

    def test_main_conflict_falls_back_to_recreate(self):
        """On rebase conflict, main() should fall back to /recreate."""
        conflict_msg = "Rebase conflict on `main` (tried origin and upstream). Manual resolution required."
        open_ctx = {"state": "OPEN", "branch": "feat", "base": "main"}
        with patch("app.rebase_pr.run_rebase", return_value=(False, conflict_msg)), \
             patch("app.rebase_pr.fetch_pr_context", return_value=open_ctx), \
             patch("app.recreate_pr.run_recreate", return_value=(True, "PR #42 recreated.")) as mock_recreate:
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 0
            mock_recreate.assert_called_once()
            call_args = mock_recreate.call_args
            assert call_args[0][:3] == ("sukria", "koan", "42")
            assert call_args[0][3] == "/project"
            assert str(call_args[1]["skill_dir"]).endswith("skills/core/recreate")

    def test_main_non_conflict_failure_no_fallback(self):
        """Non-conflict failures should NOT trigger recreate fallback."""
        with patch("app.rebase_pr.run_rebase", return_value=(False, "Push failed: auth error")), \
             patch("app.recreate_pr.run_recreate") as mock_recreate:
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 1
            mock_recreate.assert_not_called()

    def test_main_conflict_recreate_also_fails(self):
        """If recreate also fails after conflict, exit code should be 1."""
        conflict_msg = "Rebase conflict on `main` (tried origin and upstream). Manual resolution required."
        open_ctx = {"state": "OPEN", "branch": "feat", "base": "main"}
        with patch("app.rebase_pr.run_rebase", return_value=(False, conflict_msg)), \
             patch("app.rebase_pr.fetch_pr_context", return_value=open_ctx), \
             patch("app.recreate_pr.run_recreate", return_value=(False, "Recreation failed.")):
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 1

    def test_main_conflict_merged_pr_no_fallback(self):
        """On conflict with a merged PR, should NOT fall back to recreate."""
        conflict_msg = "Rebase conflict on `main` (tried origin and upstream). Manual resolution required."
        merged_ctx = {"state": "MERGED", "branch": "feat", "base": "main"}
        with patch("app.rebase_pr.run_rebase", return_value=(False, conflict_msg)), \
             patch("app.rebase_pr.fetch_pr_context", return_value=merged_ctx), \
             patch("app.recreate_pr.run_recreate") as mock_recreate:
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 1
            mock_recreate.assert_not_called()

    def test_main_conflict_closed_pr_no_fallback(self):
        """On conflict with a closed PR, should NOT fall back to recreate."""
        conflict_msg = "Rebase conflict on `main` (tried origin and upstream). Manual resolution required."
        closed_ctx = {"state": "CLOSED", "branch": "feat", "base": "main"}
        with patch("app.rebase_pr.run_rebase", return_value=(False, conflict_msg)), \
             patch("app.rebase_pr.fetch_pr_context", return_value=closed_ctx), \
             patch("app.recreate_pr.run_recreate") as mock_recreate:
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 1
            mock_recreate.assert_not_called()

    def test_main_conflict_fetch_failure_still_falls_back(self):
        """If fetch_pr_context fails in fallback, proceed with recreate anyway."""
        conflict_msg = "Rebase conflict on `main` (tried origin and upstream). Manual resolution required."
        with patch("app.rebase_pr.run_rebase", return_value=(False, conflict_msg)), \
             patch("app.rebase_pr.fetch_pr_context", side_effect=RuntimeError("API error")), \
             patch("app.recreate_pr.run_recreate", return_value=(True, "PR #42 recreated.")) as mock_recreate:
            code = rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert code == 0
            mock_recreate.assert_called_once()
    def test_detects_conflict_message(self):
        msg = "Rebase conflict on `main` (tried origin and upstream). Manual resolution required."
        assert _is_conflict_failure(msg) is True

    def test_rejects_non_conflict(self):
        assert _is_conflict_failure("Push failed: auth error") is False

    def test_rejects_empty(self):
        assert _is_conflict_failure("") is False


# ---------------------------------------------------------------------------
# --onto rebase (cross-fork PR support)
# ---------------------------------------------------------------------------

class TestRebaseOntoTarget_OntoMode:
    """Tests for --onto rebase when head_remote differs from target remote."""

    def test_uses_onto_when_head_remote_differs(self):
        """--onto should be used when head_remote != target remote."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result == "upstream"
        # Should have fetched both remotes' base branches
        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        assert ["git", "fetch", "upstream", "main"] in fetch_cmds
        assert ["git", "fetch", "origin", "main"] in fetch_cmds
        # Should use --onto
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "--onto" in rebase_cmds[0]
        assert "upstream/main" in rebase_cmds[0]
        assert "origin/main" in rebase_cmds[0]

    def test_plain_rebase_when_head_remote_same_as_target(self):
        """When head_remote == target remote, use plain rebase (same-repo PR)."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="origin",
                head_remote="origin",
            )

        assert result == "origin"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "--onto" not in rebase_cmds[0]

    def test_plain_rebase_when_head_remote_is_none(self):
        """When head_remote is None, use plain rebase."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target("main", "/project", head_remote=None)

        assert result == "origin"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "--onto" not in rebase_cmds[0]

    def test_onto_failure_falls_back_to_plain_rebase(self):
        """If --onto rebase fails, fall back to plain rebase."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if "rebase" in cmd and "--onto" in cmd:
                raise RuntimeError("onto rebase conflict")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result == "upstream"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        # Should have tried --onto first, then plain rebase
        assert len(rebase_cmds) == 2
        assert "--onto" in rebase_cmds[0]
        assert "--onto" not in rebase_cmds[1]

    def test_onto_head_remote_fetch_failure_falls_back(self):
        """If fetching head_remote/base fails, fall back to plain rebase."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            # head_remote fetch fails
            if cmd[:3] == ["git", "fetch", "origin"] and "main" in cmd:
                raise RuntimeError("fetch failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result == "upstream"
        # Should have fallen back to plain rebase
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "--onto" not in rebase_cmds[0]


class TestRebaseWithConflictResolution_OntoMode:
    """Tests for --onto rebase in _rebase_with_conflict_resolution."""

    def _base_context(self):
        return {
            "title": "Fix", "body": "", "branch": "feat",
            "base": "main", "diff": "", "review_comments": "",
            "reviews": "", "issue_comments": "",
        }

    def test_uses_onto_when_head_remote_differs(self):
        """--onto should be used when head_remote != preferred_remote."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_with_conflict_resolution(
                "main", "/project", self._base_context(), [],
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result == "upstream"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert any("--onto" in c for c in rebase_cmds)

    def test_plain_rebase_when_same_remote(self):
        """Same-repo PR: head_remote == target, no --onto."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_with_conflict_resolution(
                "main", "/project", self._base_context(), [],
                preferred_remote="origin",
                head_remote="origin",
            )

        assert result == "origin"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert all("--onto" not in c for c in rebase_cmds)

    def test_onto_failure_falls_back_to_plain_rebase(self):
        """If --onto fails (non-conflict), should fall back to plain rebase."""
        calls = []
        rebase_dir = MagicMock()
        rebase_dir.exists.return_value = False

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if "rebase" in cmd and "--onto" in cmd:
                raise RuntimeError("onto failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            result = _rebase_with_conflict_resolution(
                "main", "/project", self._base_context(), [],
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result == "upstream"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        plain_rebases = [c for c in rebase_cmds if "--onto" not in c]
        assert len(plain_rebases) >= 1


class TestFetchPrContextHeadOwner:
    """Tests that fetch_pr_context extracts head_owner."""

    @patch("app.github.subprocess.run")
    def test_extracts_head_owner(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "Fix",
                "headRefName": "feat",
                "baseRefName": "main",
                "state": "OPEN",
                "author": {"login": "contributor"},
                "headRepositoryOwner": {"login": "contributor"},
                "url": "https://github.com/upstream/repo/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),  # review comment count
            MagicMock(returncode=0, stdout=""),  # diff
            MagicMock(returncode=0, stdout=""),  # review comments
            MagicMock(returncode=0, stdout=""),  # reviews
            MagicMock(returncode=0, stdout=""),  # issue comments
        ]

        context = fetch_pr_context("upstream", "repo", "1")
        assert context["head_owner"] == "contributor"

    @patch("app.github.subprocess.run")
    def test_head_owner_missing_defaults_empty(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "Fix",
                "headRefName": "feat",
                "baseRefName": "main",
            })),
            MagicMock(returncode=0, stdout="0"),  # review comment count
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]

        context = fetch_pr_context("o", "r", "1")
        assert context["head_owner"] == ""


class TestPushWithFallbackHeadRemote:
    """Tests that _push_with_fallback tries head_remote first."""

    def test_tries_head_remote_first(self):
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "feat", "main", "upstream/repo", "1",
                {"title": "Fix"}, "/project",
                head_remote="myfork",
            )

        assert result["success"] is True
        # First push attempt should be to head_remote
        push_cmds = [c for c in calls if "push" in c]
        assert push_cmds[0][2] == "myfork"

    def test_falls_back_to_origin_when_head_remote_fails(self):
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if "push" in cmd and cmd[2] == "myfork":
                raise RuntimeError("push rejected")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "feat", "main", "upstream/repo", "1",
                {"title": "Fix"}, "/project",
                head_remote="myfork",
            )

        assert result["success"] is True
        push_cmds = [c for c in calls if "push" in c]
        # Should have tried myfork first (both lease and force), then origin
        assert any(c[2] == "origin" for c in push_cmds)


# ---------------------------------------------------------------------------
# CI checking and fixing
# ---------------------------------------------------------------------------

class TestWaitForCi:
    """Tests for wait_for_ci() in claude_step."""

    @patch("app.claude_step.time.sleep")
    @patch("app.claude_step.run_gh")
    def test_ci_passes(self, mock_gh, mock_sleep):
        mock_gh.return_value = json.dumps([{
            "databaseId": 123,
            "status": "completed",
            "conclusion": "success",
        }])
        status, run_id, logs = wait_for_ci("koan/fix", "owner/repo", timeout=60)
        assert status == "success"
        assert run_id == 123
        assert logs == ""

    @patch("app.claude_step.time.sleep")
    @patch("app.claude_step.run_gh")
    def test_no_ci_runs(self, mock_gh, mock_sleep):
        mock_gh.return_value = "[]"
        status, run_id, logs = wait_for_ci("koan/fix", "owner/repo", timeout=60)
        assert status == "none"
        assert run_id is None

    @patch("app.claude_step.time.sleep")
    @patch("app.claude_step._fetch_failed_logs", return_value="error in test_foo")
    @patch("app.claude_step.run_gh")
    def test_ci_fails(self, mock_gh, mock_fetch_logs, mock_sleep):
        mock_gh.return_value = json.dumps([{
            "databaseId": 456,
            "status": "completed",
            "conclusion": "failure",
        }])
        status, run_id, logs = wait_for_ci("koan/fix", "owner/repo", timeout=60)
        assert status == "failure"
        assert run_id == 456
        assert "error in test_foo" in logs

    @patch("app.claude_step.time.time")
    @patch("app.claude_step.time.sleep")
    @patch("app.claude_step.run_gh")
    def test_ci_timeout(self, mock_gh, mock_sleep, mock_time):
        # Simulate time progression past deadline
        mock_time.side_effect = [0, 100, 200, 700]  # deadline=600, exceeds on 3rd check
        mock_gh.return_value = json.dumps([{
            "databaseId": 789,
            "status": "in_progress",
            "conclusion": "",
        }])
        status, run_id, logs = wait_for_ci("koan/fix", "owner/repo", timeout=600)
        assert status == "timeout"


class TestRunCiCheckAndFix:
    """Tests for _run_ci_check_and_fix() in rebase_pr."""

    def _make_context(self):
        return {
            "title": "Fix bug",
            "branch": "koan/fix",
            "base": "main",
            "body": "",
            "diff": "",
        }

    @patch("app.rebase_pr.wait_for_ci", return_value=("success", 100, ""))
    def test_ci_passes_no_fix_needed(self, mock_wait):
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "CI passed" in result
        assert "CI passed" in actions

    @patch("app.rebase_pr.wait_for_ci", return_value=("none", None, ""))
    def test_no_ci_runs(self, mock_wait):
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert result == ""
        assert "No CI runs found" in actions

    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.run_claude_step", return_value=True)
    @patch("app.rebase_pr.load_prompt_or_skill", return_value="fix prompt")
    @patch("app.rebase_pr.wait_for_ci")
    def test_ci_fails_then_fixed(self, mock_wait, mock_prompt, mock_claude, mock_git):
        mock_wait.side_effect = [
            ("failure", 456, "test_foo FAILED"),  # initial failure
            ("success", 457, ""),                  # passes after fix
        ]
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "fixed on attempt 1" in result
        mock_claude.assert_called_once()

    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.run_claude_step", return_value=True)
    @patch("app.rebase_pr.load_prompt_or_skill", return_value="fix prompt")
    @patch("app.rebase_pr.wait_for_ci")
    def test_ci_fails_exhausts_retries(self, mock_wait, mock_prompt, mock_claude, mock_git):
        mock_wait.return_value = ("failure", 456, "persistent error")
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert f"after {MAX_CI_FIX_ATTEMPTS} fix attempts" in result
        assert mock_claude.call_count == MAX_CI_FIX_ATTEMPTS

    @patch("app.rebase_pr.run_claude_step", return_value=False)
    @patch("app.rebase_pr.load_prompt_or_skill", return_value="fix prompt")
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_ci_fails_claude_no_changes(self, mock_wait, mock_prompt, mock_claude):
        """When Claude can't produce a fix, stop retrying."""
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        # Should stop after first failed attempt since Claude produced no changes
        mock_claude.assert_called_once()


class TestCiCheckAndFixPrLink:
    """Tests that _run_ci_check_and_fix includes the PR link in notifications."""

    def _make_context(self):
        return {
            "title": "Fix bug",
            "branch": "koan/fix",
            "base": "main",
            "body": "",
            "diff": "",
            "url": "https://github.com/owner/repo/pull/42",
        }

    @patch("app.rebase_pr.wait_for_ci", return_value=("success", 100, ""))
    def test_initial_check_includes_pr_link(self, mock_wait):
        messages = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), [], lambda m: messages.append(m),
        )
        assert any("owner/repo/pull/42" in m for m in messages)

    @patch("app.rebase_pr._check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.run_claude_step", return_value=True)
    @patch("app.rebase_pr.load_prompt_or_skill", return_value="fix prompt")
    @patch("app.rebase_pr.wait_for_ci")
    def test_fix_attempt_includes_pr_link(self, mock_wait, mock_prompt, mock_claude, mock_git, mock_state):
        mock_wait.side_effect = [
            ("failure", 456, "test FAILED"),
            ("success", 457, ""),
        ]
        messages = []
        _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), [], lambda m: messages.append(m),
        )
        fix_msgs = [m for m in messages if "Fix attempt" in m]
        assert len(fix_msgs) > 0
        assert all("owner/repo/pull/42" in m for m in fix_msgs)


class TestCiCheckAndFixAbortOnMerged:
    """Tests that _run_ci_check_and_fix aborts if the PR has been merged."""

    def _make_context(self):
        return {
            "title": "Fix bug",
            "branch": "koan/fix",
            "base": "main",
            "body": "",
            "diff": "",
            "url": "https://github.com/owner/repo/pull/42",
        }

    @patch("app.rebase_pr._check_pr_state", return_value=("MERGED", "UNKNOWN"))
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_aborts_when_pr_merged(self, mock_wait, mock_state):
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "merged" in result.lower()
        assert any("merged" in a.lower() for a in actions)

    @patch("app.rebase_pr._check_pr_state", return_value=("OPEN", "CONFLICTING"))
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_aborts_when_pr_has_conflicts(self, mock_wait, mock_state):
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "conflict" in result.lower()
        assert any("conflict" in a.lower() for a in actions)

    @patch("app.rebase_pr._check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.run_claude_step", return_value=True)
    @patch("app.rebase_pr.load_prompt_or_skill", return_value="fix prompt")
    @patch("app.rebase_pr.wait_for_ci")
    def test_proceeds_when_pr_open_and_mergeable(self, mock_wait, mock_prompt, mock_claude, mock_git, mock_state):
        mock_wait.side_effect = [
            ("failure", 456, "test FAILED"),
            ("success", 457, ""),
        ]
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "fixed on attempt 1" in result
        mock_claude.assert_called_once()


class TestCheckPrState:
    """Tests for _check_pr_state() helper."""

    @patch("app.rebase_pr.run_gh", return_value='{"state":"MERGED","mergeable":"UNKNOWN"}')
    def test_returns_merged(self, mock_gh):
        from app.rebase_pr import _check_pr_state
        state, mergeable = _check_pr_state("42", "owner/repo")
        assert state == "MERGED"
        assert mergeable == "UNKNOWN"

    @patch("app.rebase_pr.run_gh", return_value='{"state":"OPEN","mergeable":"CONFLICTING"}')
    def test_returns_conflicting(self, mock_gh):
        from app.rebase_pr import _check_pr_state
        state, mergeable = _check_pr_state("42", "owner/repo")
        assert state == "OPEN"
        assert mergeable == "CONFLICTING"

    @patch("app.rebase_pr.run_gh", side_effect=RuntimeError("API error"))
    def test_returns_unknown_on_error(self, mock_gh):
        from app.rebase_pr import _check_pr_state
        state, mergeable = _check_pr_state("42", "owner/repo")
        assert state == "UNKNOWN"
        assert mergeable == "UNKNOWN"


class TestBuildRebaseCommentWithCi:
    """Tests for CI section in _build_rebase_comment."""

    def test_ci_section_included(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            ci_section="CI passed.",
        )
        assert "### CI" in result
        assert "CI passed." in result

    def test_no_ci_section_when_empty(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            ci_section="",
        )
        assert "### CI" not in result


# ---------------------------------------------------------------------------
# Descriptive commit messages for review feedback (issue #964)
# ---------------------------------------------------------------------------

class TestApplyReviewFeedbackDescriptiveCommit:
    """_apply_review_feedback should return a change summary and use it in
    the commit message so that rebase commits explain what changed."""

    @patch("app.rebase_pr.commit_if_changes", return_value=True)
    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_returns_change_summary(self, _mc, _cmd, mock_claude, mock_commit):
        """When Claude produces changes, _apply_review_feedback returns the summary."""
        mock_claude.return_value = {
            "success": True,
            "output": "Refactored auth to use JWT tokens and updated tests.",
            "error": "",
        }
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
        )
        assert summary is not None
        assert len(summary) > 0

    @patch("app.rebase_pr.commit_if_changes", return_value=False)
    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_returns_empty_when_no_changes(self, _mc, _cmd, mock_claude, mock_commit):
        """When Claude produces no changes, returns empty string."""
        mock_claude.return_value = {
            "success": True, "output": "No changes needed.", "error": "",
        }
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "looks good",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
        )
        assert summary == ""

    @patch("app.rebase_pr.commit_if_changes", return_value=True)
    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_commit_message_includes_summary(self, _mc, _cmd, mock_claude, mock_commit):
        """The commit message should include Claude's change summary as a body."""
        mock_claude.return_value = {
            "success": True,
            "output": "Updated error handling in auth middleware.",
            "error": "",
        }
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix error handling",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
        )
        # Verify commit message has both subject and body
        commit_msg = mock_commit.call_args[0][1]
        assert "rebase: apply review feedback on #42" in commit_msg
        assert "Updated error handling" in commit_msg


class TestBuildRebaseCommentChangeSummary:
    """_build_rebase_comment should include a change summary section
    when review feedback was applied (issue #964)."""

    def test_change_summary_included(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Applied review feedback"],
            {"title": "Fix bug", "review_comments": "fix the typo"},
            change_summary="Fixed typo in error message and updated tests.",
        )
        assert "### Changes" in result
        assert "Fixed typo in error message" in result

    def test_no_change_summary_when_empty(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            change_summary="",
        )
        assert "### Changes" not in result

    def test_no_change_summary_when_none(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
        )
        assert "### Changes" not in result


class TestRunRebasePassesChangeSummary:
    """run_rebase should pass the change summary from _apply_review_feedback
    through to _build_rebase_comment."""

    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._build_rebase_comment")
    @patch("app.rebase_pr._apply_review_feedback", return_value="Fixed the auth bug.")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_summary_forwarded_to_comment(
        self, mock_ctx, mock_apply, mock_comment, mock_gh, mock_safe, mock_ci_check,
    ):
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }
        mock_comment.return_value = "comment body"
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._get_diffstat", return_value=""), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            run_rebase("o", "r", "1", "/p", notify_fn=notify,
                       skill_dir=REBASE_SKILL_DIR)
            # Verify _build_rebase_comment was called with change_summary
            call_kwargs = mock_comment.call_args
            assert call_kwargs[1].get("change_summary") == "Fixed the auth bug."
