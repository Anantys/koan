"""Tests for rebase_pr.py — PR rebase pipeline, URL parsing, git operations."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.claude_step import _rebase_onto_target, _run_git
from app.utils import truncate_text
from app.github_url_parser import parse_pr_url
from app.rebase_pr import (
    fetch_pr_context,
    build_comment_summary,
    run_rebase,
    _apply_review_feedback,
    _build_rebase_comment,
    _build_rebase_prompt,
    _checkout_pr_branch,
    _get_current_branch,
    _is_conflict_failure,
    _push_with_fallback,
    _safe_checkout,
)
from app.claude_step import _is_permission_error


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
        mock_result = MagicMock(returncode=0, stdout="koan/my-feature\n")
        with patch("app.claude_step.subprocess.run", return_value=mock_result):
            assert _get_current_branch("/project") == "koan/my-feature"

    def test_fallback_on_error(self):
        with patch("app.claude_step.subprocess.run", side_effect=Exception("detached HEAD")):
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
            _checkout_pr_branch("koan/fix", "/project")

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
        """If origin fetch fails, tries upstream."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock(returncode=0, stdout="", stderr="")
            # origin fetch fails
            if cmd[:3] == ["git", "fetch", "origin"]:
                raise RuntimeError("remote ref not found")
            return result

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            _checkout_pr_branch("feat/upstream-only", "/project")

        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        assert ["git", "fetch", "origin", "feat/upstream-only"] in fetch_cmds
        assert ["git", "fetch", "upstream", "feat/upstream-only"] in fetch_cmds

        # Checkout should use upstream, not origin
        checkout_cmds = [c for c in calls if "checkout" in c]
        assert len(checkout_cmds) == 1
        assert "upstream/feat/upstream-only" in checkout_cmds[0]

    def test_raises_if_both_remotes_fail(self):
        """If both origin and upstream fail, raises RuntimeError."""
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                raise RuntimeError("remote ref not found")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            with pytest.raises(RuntimeError, match="not found on origin or upstream"):
                _checkout_pr_branch("nonexistent", "/project")


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

    @patch("app.github.subprocess.run")
    def test_handles_empty_responses(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({"title": "T", "headRefName": "br"})),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert context["branch"] == "br"
        assert context["diff"] == ""
        assert context["review_comments"] == ""

    @patch("app.github.subprocess.run")
    def test_handles_invalid_json(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="not json"),
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
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_successful_rebase(self, mock_ctx, mock_git, mock_gh, mock_safe):
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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_comment_failure_non_fatal(self, mock_ctx, mock_gh, mock_safe):
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
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_logs_comments_read(self, mock_ctx, mock_apply, mock_gh, mock_safe):
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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_restores_branch_after_success(self, mock_ctx, mock_gh, mock_safe):
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
    @patch("app.claude_step.run_claude_step", return_value=True)
    def test_invokes_claude_step(self, mock_step):
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
        mock_step.assert_called_once()
        call_kwargs = mock_step.call_args
        assert "rebase: apply review feedback on #42" in str(call_kwargs)

    @patch("app.claude_step.run_claude_step", return_value=True)
    def test_logs_success(self, mock_step):
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
        # run_claude_step handles logging, so just verify it was called
        assert mock_step.called


# ---------------------------------------------------------------------------
# run_rebase — Claude step integration
# ---------------------------------------------------------------------------

class TestRunRebaseClaude:
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_step_called_with_feedback(self, mock_ctx, mock_apply, mock_gh, mock_safe):
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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_step_skipped_without_feedback(self, mock_ctx, mock_apply, mock_gh, mock_safe):
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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_skill_dir_passed_to_apply(self, mock_ctx, mock_apply, mock_gh, mock_safe):
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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_branch_switch_restored_after_feedback(
        self, mock_ctx, mock_apply, mock_gh, mock_safe
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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_stays_on_branch_no_restore(
        self, mock_ctx, mock_apply, mock_gh, mock_safe
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
