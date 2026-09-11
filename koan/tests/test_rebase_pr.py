"""Tests for rebase_pr.py — PR rebase pipeline, URL parsing, git operations."""

import json
import subprocess
from types import SimpleNamespace
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
    parse_severity,
    run_rebase,
    severity_at_or_above,
    SEVERITY_LEVELS,
    _apply_review_feedback,
    _build_ci_fix_prompt,
    _build_rebase_comment,
    _split_change_summary,
    _build_rebase_prompt,
    _build_rebase_recovery_guidance,
    _checkout_pr_branch,
    _check_if_already_solved,
    _cli_label,
    _enqueue_ci_check,
    _close_pr_as_duplicate,
    _filter_bot_issue_comments,
    _find_remote_for_repo,
    _fix_existing_ci_failures,
    _get_conflicted_files,
    _resolve_rebase_conflicts,
    _truncate_recent,
    _get_current_branch,
    _is_conflict_failure,
    _push_with_fallback,
    _rebase_with_conflict_resolution,
    _run_force_push_guard,
    check_pr_state,
    _run_ci_check_and_fix,
    _run_ci_fix_step_with_timeout_retry,
    _safe_checkout,
    _UNMERGED_STATUSES,
    MAX_CI_FIX_ATTEMPTS,
)
from app.claude_step import _is_permission_error, check_existing_ci, wait_for_ci


class TestCliLabel:
    """`_cli_label` names the real CLI provider + model, not a hardcoded 'Claude'."""

    @patch("app.provider.get_provider_display", return_value="codex")
    @patch("app.config.get_model_config",
           return_value={"mission": "gpt-5-codex", "fallback": "f", "review": "r"})
    def test_mission_role_names_provider_and_model(self, _mc, _disp):
        assert _cli_label() == "codex (gpt-5-codex)"

    @patch("app.provider.get_provider_display", return_value="claude")
    @patch("app.config.get_model_config",
           return_value={"mission": "opus", "fallback": "f", "custom": "sonnet"})
    def test_present_role_key_uses_that_model(self, _mc, _disp):
        # A role key that exists in the config selects that model. (In real
        # config `review` is never emitted, so it falls back — see below.)
        assert _cli_label("custom") == "claude (sonnet)"

    @patch("app.provider.get_provider_display", return_value="claude")
    @patch("app.config.get_model_config", return_value={"mission": "opus", "fallback": "f"})
    def test_missing_role_falls_back_to_mission_model(self, _mc, _disp):
        # Real config emits no `review` key, so review→mission fallback applies.
        assert _cli_label("review") == "claude (opus)"

    @patch("app.provider.get_provider_display", return_value="claude")
    @patch("app.config.get_model_config", return_value={"mission": "", "fallback": "f"})
    def test_empty_model_omits_parenthetical(self, _mc, _disp):
        # Default config ships an empty mission model — no dangling "claude ()".
        assert _cli_label() == "claude"


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
        assert ["git", "fetch", "origin", "+refs/heads/feat/upstream-only:refs/remotes/origin/feat/upstream-only"] in fetch_cmds
        assert ["git", "fetch", "upstream", "+refs/heads/feat/upstream-only:refs/remotes/upstream/feat/upstream-only"] in fetch_cmds

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
        assert fetch_cmds[0] == ["git", "fetch", "myfork", "+refs/heads/feat/branch:refs/remotes/myfork/feat/branch"]

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

    def test_origin_only_repo_does_not_try_upstream(self):
        """When only origin is configured, checkout must not probe upstream."""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                raise RuntimeError("remote ref not found")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.rebase_pr._ordered_remotes", return_value=["origin"]), \
             patch("app.claude_step.subprocess.run", side_effect=mock_run):
            with pytest.raises(RuntimeError, match="not found on"):
                _checkout_pr_branch("feat/missing", "/project")

        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        assert len(fetch_cmds) == 1
        assert fetch_cmds[0][2] == "origin"


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

    def test_exception_returns_none_not_empty(self):
        """"Could not tell" must stay distinguishable from "nothing conflicted"."""
        with patch("app.rebase_pr.subprocess.run", side_effect=OSError("fail")):
            assert _get_conflicted_files("/project") is None

    def test_non_zero_status_returns_none(self):
        mock_result = MagicMock(
            stdout="", stderr="fatal: index.lock exists", returncode=128)
        with patch("app.rebase_pr.subprocess.run", return_value=mock_result):
            assert _get_conflicted_files("/project") is None

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
    def test_successful_rebase_on_base_remote(self):
        def mock_git(cmd, **kwargs):
            if "rev-parse" in cmd or "merge-base" in cmd:
                return "tip"
            if "rev-list" in cmd:
                return "2"
            return ""

        with patch("app.claude_step._run_git", side_effect=mock_git):
            result = _rebase_onto_target(
                "main", "/project", preferred_remote="origin",
            )
            assert result == "origin"

    def test_no_base_remote_refuses_to_guess(self):
        """Without a remote matching the PR's base repo, the rebase fails
        instead of falling back to some other remote (PR #2309 regression)."""
        meta = {}
        with patch("app.claude_step._run_git") as mock_git:
            result = _rebase_onto_target("main", "/project", result_meta=meta)
        assert result is None
        assert meta["error"] == "no_base_remote"
        mock_git.assert_not_called()

    def test_returns_none_on_conflict(self):
        def mock_git(cmd, **kwargs):
            if "rebase" in cmd:
                raise RuntimeError("conflict")
            if "rev-parse" in cmd or "merge-base" in cmd:
                return "tip"
            if "rev-list" in cmd:
                return "2"
            return ""

        mock_abort = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step._run_git", side_effect=mock_git), \
             patch("app.claude_step.subprocess.run", return_value=mock_abort):
            result = _rebase_onto_target(
                "main", "/project", preferred_remote="origin",
            )
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
        assert "## Simple rebase" in result
        assert "`koan/fix`" in result
        assert "`main`" in result
        assert "no additional changes" in result
        assert "Kōan" in result

    def test_empty_actions(self):
        result = _build_rebase_comment(
            "1", "br", "main", [],
            {"title": "PR"},
        )
        assert "no additional changes" in result

    def test_diffstat_included(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            diffstat="3 files changed, 15 insertions(+), 5 deletions(-)",
        )
        assert "3 files changed" in result
        assert "### Stats" in result

    def test_no_diffstat_when_empty(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            diffstat="",
        )
        assert "### Stats" not in result

    def test_transition_notice_included_when_flagged(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            show_transition_notice=True,
        )
        assert "> [!NOTE]" in result
        assert "--fix" in result

    def test_no_transition_notice_by_default(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
        )
        assert "> [!NOTE]" not in result

    def test_review_feedback_noted(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Applied review feedback"],
            {"title": "Fix bug", "review_comments": "please fix the typo"},
        )
        assert "## Rebase with requested adjustments" in result
        assert "review feedback was applied" in result

    def test_feedback_failed_flag_reports_not_applied(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            [
                "Rebased onto origin/main",
                "Review feedback timed out; restored clean rebased state and continuing with rebase-only push",
            ],
            {"title": "Fix bug", "review_comments": "please fix the typo"},
            feedback_failed=True,
        )
        assert "## Rebase completed; review feedback not applied" in result
        assert "review feedback could not be applied automatically" in result
        assert "## Rebase with requested adjustments" not in result

    def test_feedback_failed_renders_prominent_warning_callout(self):
        # The reason must be surfaced in a non-collapsed warning callout (not
        # only buried inside the <details> Actions block), and appear before it.
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            [
                "Rebased onto origin/main",
                "Review feedback timed out; restored clean rebased state and continuing with rebase-only push",
            ],
            {"title": "Fix bug", "review_comments": "please fix the typo"},
            feedback_failed=True,
            feedback_reason="the feedback step timed out",
        )
        assert "[!WARNING]" in result
        assert "Review feedback was NOT applied" in result
        assert "the feedback step timed out" in result
        # Callout comes before the collapsed Actions section.
        assert result.index("[!WARNING]") < result.index("<details>")

    def test_feedback_failed_warning_has_default_reason(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            feedback_failed=True,
        )
        assert "[!WARNING]" in result
        assert "Review feedback was NOT applied" in result

    def test_feedback_failed_recovery_cta_points_at_fix(self):
        """The feedback-failure recovery CTA must tell the human to re-run
        `/rebase --fix`, not a bare `/rebase`. After the /rebase split a bare
        rebase only rebases onto the base branch and would NOT re-apply the
        reviewer feedback the human still needs here.
        """
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            feedback_failed=True,
        )
        assert "/rebase --fix" in result
        # The bare-rebase CTA (a no-op for feedback) must be gone.
        assert "re-run `/rebase` or" not in result

    def test_feedback_failure_label_drives_summary_not_log_prose(self):
        # Detection comes from the structured status the caller passes, not
        # from substring-matching log lines.  A failure marker left in the
        # log must NOT, on its own, flip the summary to "not applied".
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            [
                "Rebased onto origin/main",
                "Review feedback step crashed; continuing with rebase-only push",
            ],
            {"title": "Fix bug", "review_comments": "please fix the typo"},
            feedback_failed=False,
        )
        assert "## Rebase completed; review feedback not applied" not in result
        assert "## Simple rebase" in result

    def test_conflict_resolution_noted(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Resolved merge conflicts (1 round(s))"],
            {"title": "Fix bug"},
        )
        assert "## Rebase with conflict resolution" in result
        assert "automatic conflict resolution" in result

    def test_mechanical_actions_filtered(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Read PR comments and review feedback", "Rebased", "Commented on PR"],
            {"title": "Fix bug"},
        )
        assert "Read PR comments" not in result
        assert "Commented on PR" not in result
        assert "Rebased" in result

    def test_actions_in_collapsible_details(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Force-pushed"],
            {"title": "Fix bug"},
        )
        assert "<details>" in result
        assert "Actions performed" in result
        assert "</details>" in result

    def test_applied_and_skipped_rendered_in_separate_sections(self):
        # The reproducing scenario: one point fixed, one left as already-resolved.
        # The skipped point must NOT appear under "Changes applied".
        result = _build_rebase_comment(
            "42", "koan/implement-37", "main",
            ["Rebased onto origin/main", "Applied review feedback"],
            {"title": "App shell", "review_comments": "z-index + dot"},
            change_summary=(
                "APPLIED:\n"
                "- MobileNav drawer z-index: raised overlay/content to z-[var(--z-modal)]\n"
                "SKIPPED:\n"
                "- text-muted dot fix — already resolved in an earlier pass\n"
                "- main-frame padding — advisory, below severity filter\n"
            ),
        )
        assert "### Changes applied" in result
        assert "### Not changed (and why)" in result
        # The applied change is under Changes applied, before Not changed.
        changes_idx = result.index("### Changes applied")
        skipped_idx = result.index("### Not changed")
        assert result.index("drawer z-index") > changes_idx
        assert result.index("drawer z-index") < skipped_idx
        # The already-resolved point sits under Not changed, not Changes applied.
        assert result.index("already resolved") > skipped_idx
        assert "review feedback was applied" in result

    def test_only_skipped_reports_no_changes_needed(self):
        # Everything was already addressed / advisory — the comment must say so,
        # not imply a fix was made.
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Applied review feedback"],
            {"title": "Fix"},
            change_summary=(
                "SKIPPED:\n"
                "- the requested change was already present on the branch\n"
            ),
        )
        assert "No code changes were needed" in result
        assert "### Not changed (and why)" in result
        assert "### Changes applied" not in result
        # Must not falsely claim feedback produced a change.
        assert "review feedback was applied" not in result

    def test_unstructured_summary_defaults_to_changes_applied(self):
        # Backward compat: a summary without APPLIED/SKIPPED headers is all-applied.
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Applied review feedback"],
            {"title": "Fix"},
            change_summary="Renamed get_user() to fetch_user() per reviewer request",
        )
        assert "### Changes applied" in result
        assert "fetch_user()" in result
        assert "### Not changed (and why)" not in result


class TestSplitChangeSummary:
    def test_splits_applied_and_skipped(self):
        applied, skipped = _split_change_summary(
            "APPLIED:\n- raised z-index\nSKIPPED:\n- dot already fixed\n- padding advisory"
        )
        assert applied == ["raised z-index"]
        assert skipped == ["dot already fixed", "padding advisory"]

    def test_no_headers_all_applied(self):
        applied, skipped = _split_change_summary("- did a thing\n- did another")
        assert applied == ["did a thing", "did another"]
        assert skipped == []

    def test_only_skipped(self):
        applied, skipped = _split_change_summary("SKIPPED:\n- already done")
        assert applied == []
        assert skipped == ["already done"]

    def test_tolerates_bold_legacy_headers(self):
        applied, skipped = _split_change_summary(
            "**Changes**\n- fixed X\n**Skipped**\n- Y already resolved"
        )
        assert applied == ["fixed X"]
        assert skipped == ["Y already resolved"]

    def test_content_line_not_mistaken_for_header(self):
        applied, skipped = _split_change_summary("- Applied the fix to the parser")
        assert applied == ["Applied the fix to the parser"]
        assert skipped == []

    def test_empty_summary(self):
        assert _split_change_summary("") == ([], [])


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
        assert context["diff_skipped_files"] == []  # small diff, nothing cut

    @staticmethod
    def _big_two_file_diff():
        a = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+y\n"
        b = (
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n"
            + "+x\n" * 5000
        )
        return a + b

    @patch("app.github.subprocess.run")
    def test_records_diff_skips_under_small_cap(self, mock_run):
        big = self._big_two_file_diff()
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "T", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=0, stdout=big),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1", max_diff_chars=500)
        assert "b.py" in context["diff_skipped_files"]

    @patch("app.github.subprocess.run")
    def test_large_cap_admits_full_diff(self, mock_run):
        big = self._big_two_file_diff()
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "T", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=0, stdout=big),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1", max_diff_chars=1_000_000)
        assert context["diff_skipped_files"] == []

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
        assert "HTTP 406" in context["diff_error"]

    @patch("app.rebase_pr._fetch_diff_locally")
    @patch("app.github.subprocess.run")
    def test_diff_406_falls_back_to_local_diff(
        self, mock_run, mock_local,
    ):
        """When the diff endpoint returns 406 and project_path is set,
        fetch_pr_context falls back to a local git diff."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "Huge PR",
                "headRefName": "feat/huge",
                "baseRefName": "develop",
                "state": "OPEN",
                "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/9",
            })),
            MagicMock(returncode=0, stdout="0"),
            # Diff fails with 406
            MagicMock(
                returncode=1,
                stderr="HTTP 406: diff exceeded the maximum number of files",
            ),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        mock_local.return_value = "diff --git a/x b/x\n+local fallback diff"

        context = fetch_pr_context("o", "r", "9", project_path="/tmp/checkout")

        mock_local.assert_called_once_with(
            "/tmp/checkout", "o", "r", "9", "develop",
        )
        assert "local fallback diff" in context["diff"]
        assert context["diff_error"] == ""

    @patch("app.rebase_pr._fetch_diff_locally")
    @patch("app.github.subprocess.run")
    def test_diff_406_without_project_path_skips_fallback(
        self, mock_run, mock_local,
    ):
        """No project_path → no fallback attempt, diff stays empty."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "Huge PR",
                "headRefName": "feat/huge",
                "baseRefName": "main",
                "state": "OPEN",
                "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/9",
            })),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(
                returncode=1,
                stderr="HTTP 406: diff exceeded the maximum number of files",
            ),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]

        context = fetch_pr_context("o", "r", "9")

        mock_local.assert_not_called()
        assert context["diff"] == ""
        assert "HTTP 406" in context["diff_error"]

    @patch("app.rebase_pr._fetch_diff_locally")
    @patch("app.github.subprocess.run")
    def test_diff_non_406_failure_skips_fallback(
        self, mock_run, mock_local,
    ):
        """Other gh failures (e.g. transient 5xx) should not trigger the
        local fallback — only the 'too many files' signature does."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR",
                "headRefName": "br",
                "baseRefName": "main",
                "state": "OPEN",
                "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=1, stderr="HTTP 404: not found"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]

        context = fetch_pr_context("o", "r", "1", project_path="/tmp/checkout")

        mock_local.assert_not_called()
        assert context["diff"] == ""
        assert "HTTP 404" in context["diff_error"]

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

    @patch("app.rebase_pr.get_rebase_include_bot_feedback", return_value=False)
    @patch("app.github.subprocess.run")
    def test_filters_bot_feedback_when_disabled(self, mock_run, _mock_include_bots):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(
                returncode=0,
                stdout=(
                    "[f.py:10] @github-actions[bot]: bot inline\n"
                    "[f.py:11] @alice: human inline"
                ),
            ),
            MagicMock(
                returncode=0,
                stdout=(
                    "@github-actions[bot] (COMMENTED): bot review\n"
                    "@alice (COMMENTED): human review"
                ),
            ),
            MagicMock(
                returncode=0,
                stdout=(
                    "@github-actions[bot]: bot issue line 1\n"
                    "bot continuation\n"
                    "@alice: human issue"
                ),
            ),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert "@github-actions[bot]" not in context["review_comments"]
        assert "@github-actions[bot]" not in context["reviews"]
        assert "@github-actions[bot]" not in context["issue_comments"]
        assert "@alice: human issue" in context["issue_comments"]
        assert "@alice (COMMENTED): human review" in context["reviews"]
        assert "@alice: human inline" in context["review_comments"]

    @patch("app.rebase_pr.get_rebase_include_bot_feedback", return_value=True)
    @patch("app.github.subprocess.run")
    def test_can_include_bot_feedback_when_enabled(self, mock_run, _mock_include_bots):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(returncode=0, stdout="[f.py:10] @github-actions[bot]: bot inline"),
            MagicMock(returncode=0, stdout="@github-actions[bot] (COMMENTED): bot review"),
            MagicMock(returncode=0, stdout="@github-actions[bot]: bot issue"),
        ]
        context = fetch_pr_context("o", "r", "1")
        assert "@github-actions[bot]" in context["review_comments"]
        assert "@github-actions[bot]" in context["reviews"]
        assert "@github-actions[bot]" in context["issue_comments"]

    @patch("app.rebase_pr.get_rebase_include_bot_feedback", return_value=False)
    @patch("app.github.subprocess.run")
    def test_pending_reviews_not_triggered_by_filtered_bot_comments(
        self, mock_run, _mock_include_bots,
    ):
        # The API reports inline review comments (count > 0), but they are all
        # bot-authored and get filtered out of the prompt. Pending-review
        # detection must count the raw comments so it does not false-positive.
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({
                "title": "PR", "headRefName": "br", "baseRefName": "main",
                "state": "OPEN", "author": {"login": "dev"},
                "url": "https://github.com/o/r/pull/1",
            })),
            MagicMock(returncode=0, stdout="1"),  # .review_comments count
            MagicMock(returncode=0, stdout="+diff"),
            MagicMock(returncode=0, stdout="[f.py:10] @github-actions[bot]: bot inline"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
        ]
        context = fetch_pr_context("o", "r", "1")
        # Bot comment filtered from the prompt, but no false pending warning.
        assert "@github-actions[bot]" not in context["review_comments"]
        assert context["has_pending_reviews"] is False

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

    @patch("app.retry.time.sleep")
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
        # retry_with_backoff handles sleep internally — it sleeps once between the two attempts
        assert mock_sleep.call_count == 1

    @patch("app.retry.time.sleep")
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
        # retry_with_backoff sleeps once between the failed and successful attempt
        assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# _fetch_diff_locally
# ---------------------------------------------------------------------------

class TestFetchDiffLocally:
    @patch("app.rebase_pr._resolve_fetch_source", return_value=("origin", None))
    @patch("app.rebase_pr.subprocess.run")
    def test_runs_three_git_commands_and_returns_diff(self, mock_run, _mock_src):
        """The fallback fetches PR head + base then runs git diff."""
        from app.rebase_pr import _fetch_diff_locally

        # head fetch, base fetch, diff, then two best-effort update-ref deletes
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stdout="diff --git a/f b/f\n+new", stderr=""),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
        ]

        diff = _fetch_diff_locally("/tmp/co", "o", "r", "42", "main")

        assert "diff --git" in diff
        # The first three calls should be git fetch / fetch / diff in that order
        first_three = [c.args[0][:2] for c in mock_run.call_args_list[:3]]
        assert first_three == [["git", "fetch"], ["git", "fetch"], ["git", "diff"]]
        # PR head fetch must use the pull/<N>/head refspec and the resolved remote
        head_fetch_args = mock_run.call_args_list[0].args[0]
        assert "origin" in head_fetch_args
        assert any("pull/42/head" in a for a in head_fetch_args)

    @patch("app.rebase_pr._resolve_fetch_source", return_value=(None, None))
    @patch("app.rebase_pr.subprocess.run")
    def test_returns_empty_when_no_fetch_source(self, mock_run, _mock_src):
        """No matching remote and no token → no fetch attempt, empty diff."""
        from app.rebase_pr import _fetch_diff_locally

        diff = _fetch_diff_locally("/tmp/co", "o", "r", "42", "main")

        assert diff == ""
        mock_run.assert_not_called()

    @patch("app.rebase_pr._token_fetch_url", return_value=(None, None))
    @patch("app.rebase_pr._resolve_fetch_source", return_value=("origin", None))
    @patch("app.rebase_pr.subprocess.run")
    def test_returns_empty_on_fetch_failure(self, mock_run, _mock_src, _mock_tok):
        """Returns empty string when the PR head fetch fails and no token retry is possible."""
        from app.rebase_pr import _fetch_diff_locally

        mock_run.side_effect = [
            MagicMock(returncode=128, stderr=b"fatal: couldn't find remote ref"),
            # Cleanup calls still run
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
        ]

        diff = _fetch_diff_locally("/tmp/co", "o", "r", "42", "main")

        assert diff == ""

    @patch(
        "app.rebase_pr._token_fetch_url",
        return_value=("https://x-access-token:tok@github.com/o/r.git", "tok"),
    )
    @patch("app.rebase_pr._resolve_fetch_source", return_value=("origin", None))
    @patch("app.rebase_pr.subprocess.run")
    def test_retries_with_token_url_when_remote_fetch_fails(
        self, mock_run, _mock_src, _mock_tok,
    ):
        """An HTTPS remote without a helper fails, then the token-URL retry succeeds."""
        from app.rebase_pr import _fetch_diff_locally

        mock_run.side_effect = [
            # First attempt via "origin": head fetch fails (no credentials)
            MagicMock(
                returncode=128,
                stderr=b"fatal: could not read Username for 'https://github.com'",
            ),
            # Retry attempt via token URL: head, base, diff all succeed
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stdout="diff --git a/f b/f\n+new", stderr=""),
            # cleanup x2
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
        ]

        diff = _fetch_diff_locally("/tmp/co", "o", "r", "42", "main")

        assert "diff --git" in diff
        # The retry (second call) must use the authenticated token URL
        retry_head_args = mock_run.call_args_list[1].args[0]
        assert any("x-access-token" in a for a in retry_head_args)

    @patch("app.rebase_pr._token_fetch_url", return_value=(None, None))
    @patch(
        "app.rebase_pr._resolve_fetch_source",
        return_value=("https://x-access-token:tok@github.com/o/r.git", "tok"),
    )
    @patch("app.rebase_pr.subprocess.run")
    def test_no_token_retry_when_source_already_token_url(
        self, mock_run, _mock_src, mock_tok,
    ):
        """If the resolved source is already a token URL, don't retry (would loop)."""
        from app.rebase_pr import _fetch_diff_locally

        mock_run.side_effect = [
            MagicMock(returncode=128, stderr=b"fatal: repository not found"),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
        ]

        diff = _fetch_diff_locally("/tmp/co", "o", "r", "42", "main")

        assert diff == ""
        # secret was not None, so the token-URL retry path must be skipped
        mock_tok.assert_not_called()

    @patch("app.rebase_pr._resolve_fetch_source", return_value=("origin", None))
    @patch("app.rebase_pr.subprocess.run")
    def test_cleans_up_temp_refs_on_success(self, mock_run, _mock_src):
        """Temp refs are deleted after a successful diff."""
        from app.rebase_pr import _fetch_diff_locally

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stdout="+x", stderr=""),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
        ]

        _fetch_diff_locally("/tmp/co", "o", "r", "7", "main")

        cleanup_calls = [
            c for c in mock_run.call_args_list
            if c.args[0][:2] == ["git", "update-ref"]
        ]
        assert len(cleanup_calls) == 2

    @patch("app.rebase_pr._resolve_fetch_source")
    @patch("app.rebase_pr.subprocess.run")
    def test_redacts_token_from_error_logs(self, mock_run, mock_src, capsys):
        """A token in an authenticated URL must not leak into stderr logs."""
        from app.rebase_pr import _fetch_diff_locally

        token = "ghs_supersecret"
        url = f"https://x-access-token:{token}@github.com/o/r.git"
        mock_src.return_value = (url, token)
        mock_run.side_effect = [
            MagicMock(
                returncode=128,
                stderr=f"fatal: unable to access '{url}'".encode(),
            ),
            MagicMock(returncode=0, stderr=b""),
            MagicMock(returncode=0, stderr=b""),
        ]

        _fetch_diff_locally("/tmp/co", "o", "r", "42", "main")

        captured = capsys.readouterr()
        assert token not in captured.err
        assert "***" in captured.err


# ---------------------------------------------------------------------------
# _resolve_fetch_source
# ---------------------------------------------------------------------------

class TestResolveFetchSource:
    @patch("app.rebase_pr._find_remote_for_repo", return_value="origin")
    def test_prefers_matching_remote(self, _mock_find):
        from app.rebase_pr import _resolve_fetch_source

        source, secret = _resolve_fetch_source("o", "r", "/tmp/co")
        assert source == "origin"
        assert secret is None

    @patch("app.rebase_pr.run_gh", return_value="ghs_token123")
    @patch("app.rebase_pr._find_remote_for_repo", return_value=None)
    def test_falls_back_to_token_url(self, _mock_find, _mock_gh):
        from app.rebase_pr import _resolve_fetch_source

        source, secret = _resolve_fetch_source("o", "r", "/tmp/co")
        assert source == "https://x-access-token:ghs_token123@github.com/o/r.git"
        assert secret == "ghs_token123"

    @patch("app.rebase_pr.run_gh", side_effect=RuntimeError("not logged in"))
    @patch("app.rebase_pr._find_remote_for_repo", return_value=None)
    def test_returns_none_when_no_remote_and_no_token(self, _mock_find, _mock_gh):
        from app.rebase_pr import _resolve_fetch_source

        source, secret = _resolve_fetch_source("o", "r", "/tmp/co")
        assert source is None
        assert secret is None


class TestTokenFetchUrl:
    @patch("app.rebase_pr.run_gh", return_value="ghs_abc")
    def test_builds_authenticated_url(self, _mock_gh):
        from app.rebase_pr import _token_fetch_url

        url, secret = _token_fetch_url("o", "r")
        assert url == "https://x-access-token:ghs_abc@github.com/o/r.git"
        assert secret == "ghs_abc"

    @patch("app.rebase_pr.run_gh", return_value="")
    def test_returns_none_when_token_empty(self, _mock_gh):
        from app.rebase_pr import _token_fetch_url

        url, secret = _token_fetch_url("o", "r")
        assert url is None
        assert secret is None

    @patch("app.rebase_pr.run_gh", side_effect=RuntimeError("not logged in"))
    def test_returns_none_when_gh_fails(self, _mock_gh):
        from app.rebase_pr import _token_fetch_url

        url, secret = _token_fetch_url("o", "r")
        assert url is None
        assert secret is None


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

    def test_origin_only_repo_does_not_try_upstream(self):
        """When only origin exists, push fallback must stop after origin attempts."""
        push_count = [0]

        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"]:
                push_count[0] += 1
                raise RuntimeError("rejected")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.rebase_pr._ordered_remotes", return_value=["origin"]), \
             patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project"
            )

        assert result["success"] is False
        assert push_count[0] == 2  # origin: --force-with-lease + --force


# ---------------------------------------------------------------------------
# run_rebase — integration tests
# ---------------------------------------------------------------------------

class TestRunRebase:
    @pytest.fixture(autouse=True)
    def mock_already_solved(self):
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)):
            yield

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_successful_rebase(self, mock_ctx, mock_git, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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

    @patch("app.rebase_pr._run_rebase_impl")
    @patch("app.messaging_level.notify_outcome")
    def test_outcome_warns_when_feedback_unapplied(self, mock_outcome, mock_impl):
        """A rebase that succeeds but drops feedback must surface a warning
        outcome (not a green check) so the dropped feedback is noticed."""
        def impl(*args, **kwargs):
            kwargs["outcome_meta"]["feedback_unapplied"] = "timed out"
            return True, "PR #42 rebased."
        mock_impl.side_effect = impl
        run_rebase("o", "r", "42", "/p", notify_fn=MagicMock())
        msg = mock_outcome.call_args.args[0]
        assert "⚠️" in msg
        assert "feedback NOT applied" in msg
        assert "timed out" in msg

    @patch("app.rebase_pr._run_rebase_impl")
    @patch("app.messaging_level.notify_outcome")
    def test_outcome_green_check_when_feedback_ok(self, mock_outcome, mock_impl):
        def impl(*args, **kwargs):
            return True, "PR #42 rebased."
        mock_impl.side_effect = impl
        run_rebase("o", "r", "42", "/p", notify_fn=MagicMock())
        msg = mock_outcome.call_args.args[0]
        assert msg.startswith("✅")
        assert "NOT applied" not in msg

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

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_rebase_failure_surfaces_structured_reason(self, mock_ctx, mock_safe):
        """A structured failure code from the rebase (e.g. the post-rebase
        sanity gate) must reach the mission summary — and make clear that
        nothing was pushed."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }

        def fake_rebase(*args, **kwargs):
            kwargs["result_meta"]["error"] = "sanity_check_failed"
            kwargs["result_meta"]["detail"] = (
                "rebase grew the branch from 20 to 53 unique commits"
            )
            return None

        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="original"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution",
                   side_effect=fake_rebase):
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify)

        assert success is False
        assert "[sanity_check_failed]" in summary
        assert "20 to 53" in summary
        assert "aborted before any push" in summary

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_comment_failure_non_fatal(self, mock_ctx, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_logs_comments_read(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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
            # fix=True: this test asserts the feedback leg runs, so opt in
            # explicitly rather than relying on the transitional default.
            success, summary = run_rebase("o", "r", "1", "/p", notify_fn=notify, fix=True)
            assert success is True
            assert "comments" in summary.lower()
            # Claude step should be called when feedback exists
            mock_apply.assert_called_once()

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_explicit_feedback_context_runs_leg_without_comments(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
    ):
        """An explicit feedback_context must reach Claude even when the PR has
        no reviewer comments — otherwise the human's request is silently dropped
        (contradicts the rebase spec invariant)."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, _ = run_rebase(
                "o", "r", "1", "/p", fix=True, notify_fn=MagicMock(),
                feedback_context="fix the release-please commit scopes",
            )
        assert success is True
        mock_apply.assert_called_once()
        assert (
            mock_apply.call_args.kwargs["feedback_context"]
            == "fix the release-please commit scopes"
        )

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_no_comments_and_no_context_skips_feedback_leg(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
    ):
        """A bare --fix on a PR with no feedback and no explicit request stays a
        pure rebase — the Claude feedback step must not run."""
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        with patch("app.rebase_pr._get_current_branch", return_value="feat"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, _ = run_rebase("o", "r", "1", "/p", fix=True, notify_fn=MagicMock())
        assert success is True
        mock_apply.assert_not_called()

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_unexplained_no_change_feedback_does_not_push(
        self, mock_ctx, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
    ):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }

        def no_disposition(*args, **kwargs):
            kwargs["result_meta"]["status"] = "feedback_no_disposition"
            return ""

        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._apply_review_feedback", side_effect=no_disposition), \
             patch("app.rebase_pr._push_with_fallback") as mock_push:
            success, summary = run_rebase("o", "r", "1", "/p", fix=True)

        assert success is False
        assert "feedback_no_disposition" in summary
        mock_push.assert_not_called()

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_restores_branch_after_success(self, mock_ctx, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_passes_preferred_remote_to_rebase(self, mock_ctx, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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

    def test_includes_review_protocol(self):
        """The receiving-code-review {@include} fragment resolves in the prompt."""
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(context, skill_dir=REBASE_SKILL_DIR)
        # Protocol markers — present means the {@include} fragment resolved
        # rather than leaking the literal directive into the agent prompt.
        assert "VERIFY" in prompt
        assert "EVALUATE" in prompt
        assert "{@include" not in prompt

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

    def test_injects_project_skill_instructions(self, tmp_path):
        """project_path enables .koan/skills/rebase/*.md append on rebase prompts."""
        d = tmp_path / ".koan" / "skills" / "rebase"
        d.mkdir(parents=True)
        (d / "quality-gates.md").write_text("KOAN_REBASE_EXTRA_RULE")
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(
            context, skill_dir=REBASE_SKILL_DIR, project_path=str(tmp_path),
        )
        assert "KOAN_REBASE_EXTRA_RULE" in prompt
        assert "rebase skill" in prompt

    def test_includes_fenced_explicit_feedback_context(self):
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(
            context, skill_dir=REBASE_SKILL_DIR,
            feedback_context="fix the release-please commit scopes",
        )
        assert "## Explicit User Request" in prompt
        assert "fix the release-please commit scopes" in prompt
        assert "BEGIN EXTERNAL DATA (GitHub command context)" in prompt


class TestBuildConflictAndCiFixPrompts:
    def test_conflict_prompt_injects_project_skill(self, tmp_path):
        from app.rebase_pr import _build_conflict_resolution_prompt

        d = tmp_path / ".koan" / "skills" / "rebase"
        d.mkdir(parents=True)
        (d / "quality-gates.md").write_text("KOAN_CONFLICT_EXTRA")
        context = {
            "title": "T", "body": "B", "branch": "br", "base": "main",
        }
        prompt = _build_conflict_resolution_prompt(
            context, ["a.py"], "main",
            skill_dir=REBASE_SKILL_DIR,
            project_path=str(tmp_path),
        )
        assert "KOAN_CONFLICT_EXTRA" in prompt

    def test_conflict_prompt_uses_correct_rebase_sides_without_caveman(self):
        from app.rebase_pr import _build_conflict_resolution_prompt

        context = {"title": "T", "body": "B", "branch": "br", "base": "main"}
        with patch("app.prompts._maybe_append_caveman") as append_caveman:
            prompt = _build_conflict_resolution_prompt(
                context, ["a.py"], "main", skill_dir=REBASE_SKILL_DIR,
            )
        assert "`HEAD`/`ours` is the target branch's current version" in prompt
        assert "`theirs` is the\n   PR commit being replayed" in prompt
        assert "git diff --name-only --diff-filter=U" in prompt
        append_caveman.assert_not_called()

    def test_ci_fix_prompt_injects_project_skill(self, tmp_path):
        d = tmp_path / ".koan" / "skills" / "rebase"
        d.mkdir(parents=True)
        (d / "quality-gates.md").write_text("KOAN_CI_FIX_EXTRA")
        context = {"title": "T", "branch": "br", "base": "main"}
        prompt = _build_ci_fix_prompt(
            context, "log fail", "+diff",
            skill_dir=REBASE_SKILL_DIR,
            project_path=str(tmp_path),
        )
        assert "KOAN_CI_FIX_EXTRA" in prompt


# ---------------------------------------------------------------------------
# _apply_review_feedback
# ---------------------------------------------------------------------------

class TestApplyReviewFeedback:
    @patch("app.rebase_pr.run_claude_step")
    def test_invokes_claude_step_and_returns_summary(self, mock_step):
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=True, output="Changed things.")
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
        mock_step.assert_called_once()
        call_kwargs = mock_step.call_args.kwargs
        assert call_kwargs["commit_msg"] == "rebase: apply review feedback on #42"
        assert call_kwargs["success_label"] == "Applied review feedback"
        assert call_kwargs["bypass_hook_failures"] is True
        assert summary == "Changed things."

    @patch("app.rebase_pr.get_rebase_review_max_duration", return_value=10800)
    @patch("app.rebase_pr.get_rebase_review_idle_timeout", return_value=1800)
    @patch("app.rebase_pr.get_skill_timeout", return_value=7200)
    @patch("app.rebase_pr.get_skill_max_turns", return_value=200)
    @patch("app.rebase_pr.run_claude_step")
    def test_passes_activity_based_review_timeouts(
        self, mock_step, mock_turns, mock_timeout, mock_idle, mock_max_duration,
    ):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(committed=True, output="Changed things.")
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        _apply_review_feedback(
            context, "42", "/project", actions, skill_dir=REBASE_SKILL_DIR,
        )
        call_kwargs = mock_step.call_args.kwargs
        assert call_kwargs["timeout"] == 7200
        assert call_kwargs["idle_timeout"] == 1800
        assert call_kwargs["max_duration"] == 10800

    @patch("app.rebase_pr.run_claude_step")
    def test_passes_success_label(self, mock_step):
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=True, output="Applied changes.")
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
        # Verify run_claude_step receives the actions_log and correct label
        call_kwargs = mock_step.call_args.kwargs
        assert call_kwargs["success_label"] == "Applied review feedback"
        assert call_kwargs["actions_log"] is actions

    @patch("app.rebase_pr.run_claude_step")
    def test_returns_empty_when_no_commit(self, mock_step):
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=False, output="No changes needed.")
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
        assert summary == ""

    @patch("app.rebase_pr.run_claude_step")
    def test_no_commit_requires_skipped_disposition(self, mock_step):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(committed=False, output="No changes needed.")
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR, result_meta=meta,
        )
        assert meta["status"] == "feedback_no_disposition"
        assert "no changes or explanation" in actions[-1].lower()

    @patch("app.rebase_pr.run_claude_step")
    def test_no_commit_with_skipped_disposition_is_reported(self, mock_step):
        from app.claude_step import StepResult

        skipped = "SKIPPED:\n- release commits are already split on this branch"
        mock_step.return_value = StepResult(committed=False, output=skipped)
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR, result_meta=meta,
        )
        assert meta["status"] == "evaluated_no_changes"
        assert summary == skipped

    @patch("app.rebase_pr.run_claude_step")
    def test_agent_self_commit_is_reported_as_committed(self, mock_step):
        """A commit the agent made itself must not fail as "no disposition".

        Regression: the agent honoured a "rename PROJ-101 to PROJ-99" request
        with ``git commit --amend``. That leaves a clean worktree, so the step
        reported no runner commit and the finished rebase was thrown away
        unpushed instead of being force-pushed.
        """
        from app.claude_step import StepResult

        summary_text = "APPLIED:\n- Renamed the commit subject to TECH PROJ-99"
        mock_step.return_value = StepResult(
            committed=True, output=summary_text, self_committed=True,
        )
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR, result_meta=meta,
        )
        assert meta["status"] == "committed"
        assert summary == summary_text
        assert any("committed by the agent itself" in a for a in actions)

    @patch("app.rebase_pr.run_claude_step")
    def test_runner_commit_does_not_claim_agent_authorship(self, mock_step):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(committed=True, output="APPLIED:\n- edit")
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR, result_meta=meta,
        )
        assert meta["status"] == "committed"
        assert not any("committed by the agent itself" in a for a in actions)

    @patch("app.rebase_pr.run_claude_step")
    def test_sets_feedback_timeout_metadata(self, mock_step):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(
            committed=False, output="", error="Timeout (600s)",
        )
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
            result_meta=meta,
        )
        assert summary == ""
        assert meta["status"] == "feedback_timeout"
        assert "timed out" in actions[-1].lower()

    @patch("app.rebase_pr.run_claude_step")
    def test_sets_feedback_failed_metadata(self, mock_step):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(
            committed=False, output="", error="Exit code 1: no stderr",
        )
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
            result_meta=meta,
        )
        assert summary == ""
        assert meta["status"] == "feedback_failed"
        assert "feedback failed" in actions[-1].lower()

    @patch("app.rebase_pr.run_claude_step")
    def test_sets_feedback_quota_metadata(self, mock_step):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(
            committed=False,
            output="You've hit your session limit",
            quota_exhausted=True,
            error="quota exhausted",
        )
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
            result_meta=meta,
        )
        assert summary == ""
        assert meta["status"] == "feedback_quota"
        assert "quota" in actions[-1].lower()

    @patch("app.rebase_pr._run_git")
    @patch("app.rebase_pr.run_claude_step")
    def test_step_exception_becomes_feedback_failed(self, mock_step, mock_git):
        """A crash in the feedback step (e.g. a pre-commit hook rejecting the
        edits) must not propagate — the git rebase already succeeded, so it is
        converted to ``feedback_failed`` and the uncommitted edits are reset so
        the clean rebase can still be pushed."""
        from app.git_utils import GitCommandError

        mock_step.side_effect = GitCommandError(
            "git commit", 1, "husky pre-commit hook failed",
        )
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        actions = []
        meta = {}
        # Must not raise.
        summary = _apply_review_feedback(
            context, "42", "/project", actions,
            skill_dir=REBASE_SKILL_DIR,
            result_meta=meta,
        )
        assert summary == ""
        assert meta["status"] == "feedback_failed"
        # The partial feedback edits are reset so the clean rebase survives.
        reset_calls = [
            c for c in mock_git.call_args_list
            if c.args and c.args[0][:3] == ["git", "reset", "--hard"]
        ]
        assert reset_calls, "expected a git reset --hard to drop partial edits"


# ---------------------------------------------------------------------------
# run_rebase — Claude step integration
# ---------------------------------------------------------------------------

class TestRunRebaseClaude:
    @pytest.fixture(autouse=True)
    def mock_already_solved(self):
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)):
            yield

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_step_called_with_feedback(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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
                                     skill_dir=REBASE_SKILL_DIR, fix=True)
            assert success is True
            mock_apply.assert_called_once()

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_step_skipped_without_feedback(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_skill_dir_passed_to_apply(self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci):
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
                       skill_dir=REBASE_SKILL_DIR, fix=True)
            call_kwargs = mock_apply.call_args
            assert call_kwargs[1].get("skill_dir") == REBASE_SKILL_DIR

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_branch_switch_restored_after_feedback(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci
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
                                          skill_dir=REBASE_SKILL_DIR, fix=True)
            assert success is True
            # _safe_checkout should be called to restore the PR branch
            # (once for restoration + once at end for original branch)
            checkout_calls = [c[0][0] for c in mock_safe.call_args_list]
            assert "feat" in checkout_calls  # restored to PR branch

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_claude_stays_on_branch_no_restore(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci
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
                                          skill_dir=REBASE_SKILL_DIR, fix=True)
            assert success is True
            # _safe_checkout should only be called at the end (original branch)
            # NOT for branch restoration since Claude stayed on correct branch
            restore_calls = [
                c for c in mock_safe.call_args_list
                if c[0][0] == "feat"
            ]
            assert len(restore_calls) == 0  # no restoration needed

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_feedback_timeout_pushes_rebase_without_feedback(
        self, mock_ctx, mock_apply, mock_safe,
    ):
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }

        def _apply_side_effect(*args, **kwargs):
            kwargs["result_meta"]["status"] = "feedback_timeout"
            kwargs["result_meta"]["error"] = "Timeout (600s)"
            return ""

        mock_apply.side_effect = _apply_side_effect
        notify = MagicMock()
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)), \
             patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch(
                 "app.rebase_pr._run_git",
                 # pre-rebase head capture, rebase checkpoint, timeout-recovery reset
                 side_effect=["def456\n", "abc123\n", ""],
             ), \
             patch("app.rebase_pr._fix_existing_ci_failures", return_value=False), \
             patch("app.rebase_pr._enqueue_ci_check", return_value="CI queued"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": "",
             }) as mock_push, \
             patch("app.rebase_pr.run_gh"):
            success, summary = run_rebase(
                "o", "r", "1", "/p", notify_fn=notify, skill_dir=REBASE_SKILL_DIR,
                fix=True,
            )

        assert success is True
        assert "Review feedback timed out" in summary
        assert "rebase-only push" in summary
        mock_push.assert_called_once()

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch(
        "app.rebase_pr._build_rebase_recovery_guidance",
        return_value="Recovery hints:\n- next: git status",
    )
    @patch("app.rebase_pr.fetch_pr_context")
    def test_feedback_timeout_recovery_failure_returns_error(
        self, mock_ctx, _mock_guidance, mock_apply, mock_safe,
    ):
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }

        def _apply_side_effect(*args, **kwargs):
            kwargs["result_meta"]["status"] = "feedback_timeout"
            kwargs["result_meta"]["error"] = "Timeout (600s)"
            return ""

        mock_apply.side_effect = _apply_side_effect
        notify = MagicMock()

        def _run_git_side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return "abc123\n"
            if cmd[:3] == ["git", "reset", "--hard"]:
                raise RuntimeError("reset failed")
            return ""

        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)), \
             patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._run_git", side_effect=_run_git_side_effect):
            success, summary = run_rebase(
                "o", "r", "1", "/p", notify_fn=notify, skill_dir=REBASE_SKILL_DIR,
                fix=True,
            )

        assert success is False
        assert "automatic recovery" in summary
        assert "Recovery hints" in summary

    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch(
        "app.rebase_pr._build_rebase_recovery_guidance",
        return_value="Recovery hints:\n- next: git status",
    )
    @patch("app.rebase_pr.fetch_pr_context")
    def test_feedback_quota_returns_classified_failure(
        self, mock_ctx, mock_guidance, mock_apply, mock_safe,
    ):
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }

        def _apply_side_effect(*args, **kwargs):
            kwargs["result_meta"]["status"] = "feedback_quota"
            kwargs["result_meta"]["error"] = "quota exhausted"
            return ""

        mock_apply.side_effect = _apply_side_effect
        notify = MagicMock()
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)), \
             patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"):
            success, summary = run_rebase(
                "o", "r", "1", "/p", notify_fn=notify, skill_dir=REBASE_SKILL_DIR,
                fix=True,
            )

        assert success is False
        assert "[feedback_quota]" in summary
        assert "Recovery hints" in summary

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_feedback_failed_pushes_rebase_best_effort(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
    ):
        """A non-timeout, non-quota feedback error must not discard a clean
        rebase — the rebase should still be pushed, with a note."""
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "", "author": "", "url": "",
            "diff": "+code", "review_comments": "@reviewer: fix this",
            "reviews": "", "issue_comments": "",
        }

        def _apply_side_effect(*args, **kwargs):
            kwargs["result_meta"]["status"] = "feedback_failed"
            kwargs["result_meta"]["error"] = "Exit code 1: no stderr"
            return ""

        mock_apply.side_effect = _apply_side_effect
        notify = MagicMock()
        push_mock = MagicMock(return_value={
            "success": True, "actions": ["Force-pushed"], "error": "",
        })
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)), \
             patch("app.rebase_pr._get_current_branch", return_value="feat"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", push_mock):
            success, summary = run_rebase(
                "o", "r", "1", "/p", notify_fn=notify, skill_dir=REBASE_SKILL_DIR,
                fix=True,
            )

        assert success is True
        assert "[feedback_failed]" not in summary
        # The rebase was still pushed despite the feedback error.
        push_mock.assert_called_once()


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

    def test_detects_classified_conflict_tag(self):
        msg = "[conflict_unresolved] Rebase failed on `main`."
        assert _is_conflict_failure(msg) is True

    def test_rejects_non_conflict(self):
        assert _is_conflict_failure("Push failed: auth error") is False

    def test_rejects_empty(self):
        assert _is_conflict_failure("") is False


# ---------------------------------------------------------------------------
# Cross-fork PR rebase (strict target, no --onto)
# ---------------------------------------------------------------------------

class TestRebaseOntoTargetCrossFork:
    """Cross-fork PRs rebase onto the target remote only — never --onto.

    Rebasing with a fork's base branch as the --onto cut point is how
    PR #2309 replayed ~1,900 commits and resurrected 33 merged ones; a
    plain rebase onto the fresh target ref replays exactly the PR's own
    commits (git cuts at merge-base and drops patch-identical commits).
    """

    @staticmethod
    def _record_git(calls):
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")
        return mock_run

    def test_cross_fork_uses_plain_rebase_on_target(self):
        calls = []
        with patch("app.claude_step.subprocess.run",
                   side_effect=self._record_git(calls)):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result == "upstream"
        fetch_cmds = [c for c in calls if c[:2] == ["git", "fetch"]]
        assert ["git", "fetch", "upstream",
                "+refs/heads/main:refs/remotes/upstream/main"] in fetch_cmds
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "--onto" not in rebase_cmds[0]
        assert "upstream/main" in rebase_cmds[0]
        assert "origin/main" not in rebase_cmds[0]

    def test_same_remote_uses_plain_rebase(self):
        calls = []
        with patch("app.claude_step.subprocess.run",
                   side_effect=self._record_git(calls)):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="origin",
                head_remote="origin",
            )

        assert result == "origin"
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "--onto" not in rebase_cmds[0]

    def test_failed_rebase_never_retries_another_remote(self):
        """The old fallback loop rebased onto other remotes' (stale) mains."""
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if "rebase" in cmd and "--abort" not in cmd:
                raise RuntimeError("conflict")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run):
            result = _rebase_onto_target(
                "main", "/project",
                preferred_remote="upstream",
                head_remote="origin",
            )

        assert result is None
        rebase_cmds = [c for c in calls if "rebase" in c and "--abort" not in c]
        assert len(rebase_cmds) == 1
        assert "upstream/main" in rebase_cmds[0]


class TestRebaseWithConflictResolutionCrossFork:
    """_rebase_with_conflict_resolution follows the same strict contract."""

    def _base_context(self):
        return {
            "title": "Fix", "body": "", "branch": "feat",
            "base": "main", "diff": "", "review_comments": "",
            "reviews": "", "issue_comments": "",
        }

    def test_cross_fork_plain_rebase_on_target(self):
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
        assert rebase_cmds
        assert all("--onto" not in c for c in rebase_cmds)

    def test_failure_reports_structured_reason(self):
        def mock_run(cmd, **kwargs):
            if "rebase" in cmd and "--abort" not in cmd:
                raise RuntimeError("conflict")
            return MagicMock(returncode=0, stdout="", stderr="")

        meta = {}
        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.claude_step.has_rebase_in_progress", return_value=False):
            result = _rebase_with_conflict_resolution(
                "main", "/project", self._base_context(), [],
                preferred_remote="upstream",
                head_remote="origin",
                result_meta=meta,
            )

        assert result is None
        assert meta["error"] == "rebase_failed"


class TestResolveRebaseConflicts:
    def test_timeout_uses_configured_budget_and_reports_cause(self):
        failure_detail = []
        # run_claude reports a hard timeout as "Timeout (<n>s)" — the cause
        # classifier must recognize that exact form, not just "timed out".
        with patch("app.rebase_pr._get_conflicted_files", return_value=["a.py"]), \
             patch("app.cli_provider.build_full_command", return_value=["agent"]), \
             patch("app.rebase_pr.run_claude", return_value={
                 "success": False, "error": "Timeout (777s)",
             }) as run_agent, \
             patch("app.rebase_pr.get_rebase_conflict_timeout", return_value=777):
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=1,
                skill_dir=REBASE_SKILL_DIR, failure_detail=failure_detail,
            )

        assert result is False
        assert run_agent.call_args.kwargs["timeout"] == 777
        assert failure_detail == ["conflict-resolution agent timed out after 777s"]

    def test_non_timeout_failure_reports_generic_cause(self):
        failure_detail = []
        with patch("app.rebase_pr._get_conflicted_files", return_value=["a.py"]), \
             patch("app.cli_provider.build_full_command", return_value=["agent"]), \
             patch("app.rebase_pr.run_claude", return_value={
                 "success": False, "error": "exit 1: merge tool crashed",
             }), \
             patch("app.rebase_pr.get_rebase_conflict_timeout", return_value=600):
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=1,
                skill_dir=REBASE_SKILL_DIR, failure_detail=failure_detail,
            )

        assert result is False
        assert failure_detail == [
            "conflict-resolution agent failed: exit 1: merge tool crashed"
        ]


class TestContinueRebaseStagesResolvedTree:
    """`git rebase --continue` needs a fully staged tracked tree.

    Observed failure: the resolution agent also edited a file that was not
    conflicted, to keep the merged tree coherent, and left it unstaged. No
    unmerged path remained, so the loop believed the round succeeded, but
    every `git rebase --continue` was refused with "You must edit all merge
    conflicts..." until the rounds ran out.
    """

    @staticmethod
    def _record_git_calls(git_calls, subprocess_results=None):
        """Patches that record every git command `_continue_rebase` issues."""
        results = subprocess_results or {}

        def fake_run_git(cmd, **kwargs):
            git_calls.append(cmd)
            return ""

        def fake_subprocess_run(cmd, **kwargs):
            git_calls.append(cmd)
            key = "continue" if "--continue" in cmd else "status"
            return results.get(
                key, MagicMock(returncode=0, stdout="", stderr=""))

        return (
            patch("app.rebase_pr._run_git", side_effect=fake_run_git),
            patch("app.rebase_pr.subprocess.run", side_effect=fake_subprocess_run),
        )

    def test_stages_tracked_edits_before_continuing(self):
        from app.rebase_pr import _continue_rebase

        git_calls = []
        run_git_patch, subprocess_patch = self._record_git_calls(git_calls)

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            outcome = _continue_rebase("/project")

        assert outcome.completed is True
        assert outcome.detail == ""
        assert ["git", "add", "-u"] in git_calls
        assert git_calls.index(["git", "add", "-u"]) < len(git_calls) - 1
        assert git_calls[-1][:3] == ["git", "rebase", "--continue"]

    def test_untracked_files_are_not_swept_into_the_commit(self):
        """`add -u` only — scratch files never block `--continue`."""
        from app.rebase_pr import _continue_rebase

        git_calls = []
        run_git_patch, subprocess_patch = self._record_git_calls(git_calls)

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            _continue_rebase("/project")

        assert ["git", "add", "-A"] not in git_calls
        assert ["git", "add", "-u"] in git_calls

    def test_unmerged_check_never_uses_the_unreliable_diff_filter(self):
        """The backstop must ask `git status --porcelain`, not a worktree diff.

        `git diff --name-only --diff-filter=U` can under-report during
        `--onto` rebases and merge linearisation; an empty listing there would
        let `git add -u` stage conflict markers verbatim.
        """
        from app.rebase_pr import _continue_rebase

        git_calls = []
        run_git_patch, subprocess_patch = self._record_git_calls(git_calls)

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            _continue_rebase("/project")

        assert not any("--diff-filter=U" in cmd for cmd in git_calls)
        staged_at = git_calls.index(["git", "add", "-u"])
        assert ["git", "status", "--porcelain"] in git_calls[:staged_at]

    def test_untracked_resolution_artifacts_reach_the_actions_log(self):
        """A new file `add -u` leaves behind must reach the human, not stderr."""
        from app.rebase_pr import _continue_rebase

        def fake_run_git(cmd, **kwargs):
            if "ls-files" in cmd:
                return "app/new_helper.py\n"
            return ""

        actions_log = []
        with patch("app.rebase_pr._run_git", side_effect=fake_run_git), \
             patch("app.rebase_pr.subprocess.run", return_value=MagicMock(
                 returncode=0, stdout="", stderr="")), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            _continue_rebase("/project", actions_log=actions_log)

        assert any("app/new_helper.py" in entry for entry in actions_log)

    def test_refuses_to_stage_when_a_path_is_still_unmerged(self):
        """`add -u` would commit conflict markers verbatim — never guess."""
        from app.rebase_pr import _continue_rebase

        git_calls = []
        run_git_patch, subprocess_patch = self._record_git_calls(
            git_calls,
            {"status": MagicMock(returncode=0, stdout="UU a.py\n", stderr="")},
        )

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "still unmerged" in outcome.detail and "a.py" in outcome.detail
        # The agent left work behind; retrying the round would not change that.
        assert outcome.retryable is False
        assert ["git", "add", "-u"] not in git_calls

    def test_unverifiable_tree_is_retryable_not_terminal(self):
        """A failed check is not a clean tree — and nothing was attempted."""
        from app.rebase_pr import _continue_rebase

        git_calls = []
        run_git_patch, subprocess_patch = self._record_git_calls(
            git_calls,
            {"status": MagicMock(
                returncode=128, stdout="", stderr="fatal: index.lock exists")},
        )

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "could not verify the tree is unmerged-free" in outcome.detail
        assert "index.lock" in outcome.detail
        assert outcome.retryable is True
        assert ["git", "add", "-u"] not in git_calls
        assert not any("--continue" in cmd for cmd in git_calls)

    def test_failed_staging_is_the_reported_cause(self):
        """A refused `git add -u` must not surface as the `--continue` symptom."""
        from app.rebase_pr import _continue_rebase

        def fake_run_git(cmd, **kwargs):
            raise RuntimeError("fatal: Unable to create index.lock")

        with patch("app.rebase_pr._run_git", side_effect=fake_run_git), \
             patch("app.rebase_pr.subprocess.run", return_value=MagicMock(
                 returncode=0, stdout="", stderr="")), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "staging the resolved tree failed" in outcome.detail
        assert "index.lock" in outcome.detail

    def test_continue_timeout_is_a_failure_even_with_the_rebase_dir_gone(self):
        """A killed git is no evidence the rebase finished — never report done.

        git unlinks `.git/rebase-merge` before the last commit is replayed, so
        a missing directory after a SIGKILL can hide a rebase stopped
        mid-flight; claiming success there force-pushes a half-rebased branch.
        """
        from app.rebase_pr import _continue_rebase

        def fake_subprocess_run(cmd, **kwargs):
            if "--continue" in cmd:
                raise subprocess.TimeoutExpired(cmd, 60)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.rebase_pr._run_git", return_value=""), \
             patch("app.rebase_pr.subprocess.run", side_effect=fake_subprocess_run), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "git rebase --continue failed" in outcome.detail

    def test_continue_timeout_with_rebase_still_running_fails(self):
        from app.rebase_pr import _continue_rebase

        def fake_subprocess_run(cmd, **kwargs):
            if "--continue" in cmd:
                raise subprocess.TimeoutExpired(cmd, 60)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.rebase_pr._run_git", return_value=""), \
             patch("app.rebase_pr.subprocess.run", side_effect=fake_subprocess_run), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "git rebase --continue failed" in outcome.detail

    def test_refused_continue_returns_git_error_text(self):
        from app.rebase_pr import _continue_rebase

        run_git_patch, subprocess_patch = self._record_git_calls(
            [],
            {"continue": MagicMock(
                returncode=1, stdout="",
                stderr="You must edit all merge conflicts")},
        )

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "You must edit all merge conflicts" in outcome.detail

    def test_non_zero_exit_with_the_rebase_dir_gone_is_not_success(self):
        """A rejected commit hook tears the rebase down without committing."""
        from app.rebase_pr import _continue_rebase

        run_git_patch, subprocess_patch = self._record_git_calls(
            [],
            {"continue": MagicMock(
                returncode=1, stdout="",
                stderr="commit-msg hook refused the commit")},
        )

        with run_git_patch, subprocess_patch, \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert "commit-msg hook refused the commit" in outcome.detail

    def test_next_commit_conflict_is_not_an_error(self):
        """A `--continue` that stops on the next commit reports no detail."""
        from app.rebase_pr import _continue_rebase

        with patch("app.rebase_pr._run_git", return_value=""), \
             patch("app.rebase_pr.subprocess.run", return_value=MagicMock(
                 returncode=0, stdout="", stderr="")), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True):
            outcome = _continue_rebase("/project")

        assert outcome.completed is False
        assert outcome.detail == ""


class TestResolveRebaseConflictsProgress:
    """The round loop must not spin when the rebase cannot advance."""

    def test_stuck_continue_fails_fast_with_the_real_cause(self):
        from app.rebase_pr import _ContinueOutcome

        failure_detail = []
        continues = []

        def fake_continue(project_path, actions_log=None):
            continues.append(project_path)
            return _ContinueOutcome(
                False, "git rebase --continue failed: unstaged changes")

        with patch("app.rebase_pr._get_conflicted_files", return_value=[]), \
             patch("app.rebase_pr._continue_rebase", side_effect=fake_continue):
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=10,
                failure_detail=failure_detail,
            )

        assert result is False
        # One attempt, not ten — a stuck rebase never becomes unstuck by retrying.
        assert len(continues) == 1
        assert failure_detail == [
            "git rebase --continue failed: unstaged changes"
        ]

    def test_exhausted_rounds_report_that_reason(self):
        from app.rebase_pr import _ContinueOutcome

        failure_detail = []
        # Per round: conflicted → resolved → the next commit conflicts again.
        from itertools import cycle
        rounds = cycle([["a.py"], [], ["b.py"]])
        with patch("app.rebase_pr._get_conflicted_files",
                   side_effect=lambda *_a, **_k: next(rounds)), \
             patch("app.cli_provider.build_full_command", return_value=["agent"]), \
             patch("app.rebase_pr.run_claude", return_value={"success": True}), \
             patch("app.rebase_pr._continue_rebase",
                   return_value=_ContinueOutcome(False, "")), \
             patch("app.rebase_pr.get_rebase_conflict_timeout", return_value=600):
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=2,
                skill_dir=REBASE_SKILL_DIR, failure_detail=failure_detail,
            )

        assert result is False
        assert failure_detail == ["exceeded 2 conflict-resolution rounds"]

    def test_unresolved_files_report_the_files_not_the_initial_error(self):
        failure_detail = []
        with patch("app.rebase_pr._get_conflicted_files", return_value=["a.py"]), \
             patch("app.cli_provider.build_full_command", return_value=["agent"]), \
             patch("app.rebase_pr.run_claude", return_value={"success": True}), \
             patch("app.rebase_pr.get_rebase_conflict_timeout", return_value=600):
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=1,
                skill_dir=REBASE_SKILL_DIR, failure_detail=failure_detail,
            )

        assert result is False
        assert failure_detail == [
            "1 file(s) still conflicted after resolution: a.py"
        ]

    def test_unreadable_status_retries_instead_of_aborting(self):
        """A git that could not answer is not a rebase that cannot advance."""
        from app.rebase_pr import _ContinueOutcome

        failure_detail = []
        # Round 1: status unreadable. Round 2: no conflicts, continue succeeds.
        statuses = [None, []]
        with patch("app.rebase_pr._get_conflicted_files",
                   side_effect=lambda *_a, **_k: statuses.pop(0)), \
             patch("app.rebase_pr.time.sleep"), \
             patch("app.rebase_pr._continue_rebase",
                   return_value=_ContinueOutcome(True, "")) as cont:
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=3,
                failure_detail=failure_detail,
            )

        assert result is True
        assert cont.call_count == 1
        assert failure_detail == []

    def test_persistently_unreadable_status_reports_gits_own_error(self):
        """Round exhaustion must not stand in for "git was never queried"."""
        failure_detail = []

        def fake_status(project_path, errors=None):
            if errors is not None:
                errors.append("fatal: Unable to create index.lock")
            return None

        with patch("app.rebase_pr._get_conflicted_files",
                   side_effect=fake_status), \
             patch("app.rebase_pr.time.sleep") as sleep:
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=10,
                failure_detail=failure_detail,
            )

        assert result is False
        # Bounded: three attempts with a pause between them, not ten instant ones.
        assert sleep.call_count == 2
        assert failure_detail == [
            "git status could not be read after 3 attempts: "
            "fatal: Unable to create index.lock"
        ]

    def test_unverifiable_continue_keeps_the_rebase_alive(self):
        """Nothing staged, nothing continued — retry, don't abort the rebase.

        Aborting here would discard conflict resolutions already paid for in
        LLM quota because one `git status` hit a momentary lock.
        """
        from app.rebase_pr import _ContinueOutcome

        failure_detail = []
        outcomes = [
            _ContinueOutcome(
                False, "could not verify the tree is unmerged-free: locked",
                True),
            _ContinueOutcome(True, ""),
        ]
        with patch("app.rebase_pr._get_conflicted_files", return_value=[]), \
             patch("app.rebase_pr.time.sleep"), \
             patch("app.rebase_pr._continue_rebase",
                   side_effect=lambda *_a, **_k: outcomes.pop(0)) as cont:
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=3,
                failure_detail=failure_detail,
            )

        assert result is True
        assert cont.call_count == 2
        assert failure_detail == []

    def test_unreadable_status_after_continue_is_not_reported_as_stuck(self):
        """The stuck branch needs a confident "nothing conflicted", not a guess."""
        from app.rebase_pr import _ContinueOutcome

        failure_detail = []
        # Empty, then unreadable after the continue, then the real conflicts.
        statuses = [[], None, ["a.py"]]
        with patch("app.rebase_pr._get_conflicted_files",
                   side_effect=lambda *_a, **_k: statuses.pop(0)), \
             patch("app.rebase_pr.time.sleep"), \
             patch("app.rebase_pr._continue_rebase",
                   return_value=_ContinueOutcome(False, "")), \
             patch("app.cli_provider.build_full_command", return_value=["agent"]), \
             patch("app.rebase_pr.run_claude", return_value={
                 "success": False, "error": "boom",
             }), \
             patch("app.rebase_pr.get_rebase_conflict_timeout", return_value=600):
            result = _resolve_rebase_conflicts(
                "main", "", "/project", {}, [], max_rounds=3,
                failure_detail=failure_detail,
                skill_dir=REBASE_SKILL_DIR,
            )

        assert result is False
        # Reached the resolution agent — not aborted as "made no progress".
        assert failure_detail == ["conflict-resolution agent failed: boom"]


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


class TestCheckExistingCi:
    """Tests for check_existing_ci() in claude_step — single-shot CI status check."""

    @patch("app.claude_step.run_gh")
    def test_ci_success(self, mock_gh):
        mock_gh.return_value = json.dumps([{
            "databaseId": 100,
            "status": "completed",
            "conclusion": "success",
        }])
        status, run_id, logs = check_existing_ci("koan/fix", "owner/repo")
        assert status == "success"
        assert run_id == 100
        assert logs == ""

    @patch("app.claude_step._fetch_failed_logs", return_value="test_foo FAILED")
    @patch("app.claude_step.run_gh")
    def test_ci_failure(self, mock_gh, mock_logs):
        mock_gh.return_value = json.dumps([{
            "databaseId": 200,
            "status": "completed",
            "conclusion": "failure",
        }])
        status, run_id, logs = check_existing_ci("koan/fix", "owner/repo")
        assert status == "failure"
        assert run_id == 200
        assert "test_foo FAILED" in logs

    @patch("app.claude_step.run_gh")
    def test_ci_pending(self, mock_gh):
        mock_gh.return_value = json.dumps([{
            "databaseId": 300,
            "status": "in_progress",
            "conclusion": "",
        }])
        status, run_id, logs = check_existing_ci("koan/fix", "owner/repo")
        assert status == "pending"
        assert run_id == 300

    @patch("app.claude_step.run_gh")
    def test_no_ci_runs(self, mock_gh):
        mock_gh.return_value = "[]"
        status, run_id, logs = check_existing_ci("koan/fix", "owner/repo")
        assert status == "none"
        assert run_id is None

    @patch("app.claude_step.run_gh", side_effect=RuntimeError("network"))
    def test_gh_error_returns_none(self, mock_gh):
        status, run_id, logs = check_existing_ci("koan/fix", "owner/repo")
        assert status == "none"


class TestFixExistingCiFailures:
    """Tests for _fix_existing_ci_failures() — pre-push CI check and fix."""

    def _make_context(self):
        return {
            "title": "Fix bug",
            "branch": "koan/fix",
            "base": "main",
            "body": "",
            "diff": "",
            "url": "https://github.com/owner/repo/pull/42",
        }

    @patch("app.rebase_pr.check_existing_ci", return_value=("success", 100, ""))
    def test_ci_success_skips(self, mock_ci):
        actions = []
        result = _fix_existing_ci_failures(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert result is False
        assert any("previous run passed" in a for a in actions)

    @patch("app.rebase_pr.check_existing_ci", return_value=("none", None, ""))
    def test_no_ci_runs_skips(self, mock_ci):
        actions = []
        result = _fix_existing_ci_failures(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert result is False
        assert any("no CI runs" in a for a in actions)

    @patch("app.rebase_pr.check_existing_ci", return_value=("pending", 300, ""))
    def test_ci_pending_skips(self, mock_ci):
        actions = []
        result = _fix_existing_ci_failures(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert result is False
        assert any("pending" in a for a in actions)

    @patch("app.rebase_pr.run_claude_step", return_value=True)
    @patch("app.rebase_pr._build_ci_fix_prompt", return_value="fix prompt")
    @patch("app.rebase_pr._run_git", return_value="diff output")
    @patch("app.rebase_pr.check_existing_ci", return_value=("failure", 200, "test_foo FAILED"))
    def test_ci_failure_triggers_fix(self, mock_ci, mock_git, mock_prompt, mock_claude):
        actions = []
        notify_calls = []
        result = _fix_existing_ci_failures(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: notify_calls.append(m),
        )
        assert result is True
        mock_claude.assert_called_once()
        assert any("Pre-push CI fix applied" in a for a in actions)
        # Verify notify was called about analyzing logs
        assert any("analyzing" in m.lower() for m in notify_calls)

    @patch("app.rebase_pr.run_claude_step", return_value=False)
    @patch("app.rebase_pr._build_ci_fix_prompt", return_value="fix prompt")
    @patch("app.rebase_pr._run_git", return_value="diff output")
    @patch("app.rebase_pr.check_existing_ci", return_value=("failure", 200, "error"))
    def test_ci_failure_no_changes(self, mock_ci, mock_git, mock_prompt, mock_claude):
        actions = []
        result = _fix_existing_ci_failures(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert result is False
        assert any("no changes needed" in a for a in actions)


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

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._force_push")
    @patch("app.rebase_pr._run_ci_fix_step_with_timeout_retry")
    @patch("app.rebase_pr.wait_for_ci", side_effect=[
        ("failure", 456, "test_foo FAILED"),
        ("success", 457, ""),
    ])
    def test_ci_fails_then_fixed(self, mock_wait, mock_fix_step, mock_push, mock_state):
        from app.claude_step import StepResult

        mock_fix_step.return_value = (StepResult(committed=True, output="done"), False, 1)
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "fixed on attempt 1" in result
        mock_fix_step.assert_called_once()
        mock_push.assert_called_once()

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._force_push")
    @patch("app.rebase_pr._run_ci_fix_step_with_timeout_retry")
    @patch("app.rebase_pr.wait_for_ci", side_effect=[
        ("failure", 456, "persistent error"),
        ("failure", 457, "persistent error"),
        ("failure", 458, "persistent error"),
    ])
    def test_ci_fails_exhausts_retries(self, mock_wait, mock_fix_step, mock_push, mock_state):
        from app.claude_step import StepResult

        mock_fix_step.return_value = (StepResult(committed=True, output="done"), False, 1)
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert f"after {MAX_CI_FIX_ATTEMPTS} fix attempts" in result
        assert mock_fix_step.call_count == MAX_CI_FIX_ATTEMPTS
        assert mock_push.call_count == MAX_CI_FIX_ATTEMPTS

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._run_ci_fix_step_with_timeout_retry")
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_ci_fails_claude_no_changes(self, mock_wait, mock_fix_step, mock_state):
        """When CI fix step produces no changes, loop stops quickly."""
        from app.claude_step import StepResult

        mock_fix_step.return_value = (StepResult(committed=False, output=""), False, 1)
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert f"after {MAX_CI_FIX_ATTEMPTS} fix attempts" in result
        mock_fix_step.assert_called_once()

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._run_ci_fix_step_with_timeout_retry")
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_ci_fix_timeout_returns_actionable_message(
        self, mock_wait, mock_fix_step, mock_state,
    ):
        from app.claude_step import StepResult

        mock_fix_step.return_value = (StepResult(committed=False, output=""), True, 2)
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "timed out during `/rebase`" in result
        assert "/rebase https://github.com/owner/repo/pull/42" in result


class TestCiFixTimeoutRetry:
    @patch("app.rebase_pr.get_skill_timeout", return_value=999)
    @patch("app.rebase_pr.get_skill_max_turns", return_value=77)
    @patch("app.rebase_pr.get_rebase_ci_max_duration", return_value=8888)
    @patch("app.rebase_pr.get_rebase_ci_idle_timeout", return_value=555)
    @patch("app.rebase_pr.run_claude_step")
    def test_timeout_retries_once_with_tight_prompt(
        self, mock_step, mock_idle, mock_max_duration, mock_turns, mock_timeout,
    ):
        from app.claude_step import StepResult

        mock_step.side_effect = [
            StepResult(committed=False, output="", error="Timeout (999s)"),
            StepResult(committed=True, output="fixed", error=""),
        ]
        actions = []
        result, timed_out, attempts = _run_ci_fix_step_with_timeout_retry(
            prompt="base prompt",
            project_path="/project",
            commit_msg="fix: test",
            success_label="ok",
            failure_label="failed",
            actions_log=actions,
            use_convention_subject=False,
        )

        assert result.committed is True
        assert timed_out is False
        assert attempts == 2
        assert mock_step.call_count == 2
        first_prompt = mock_step.call_args_list[0].kwargs["prompt"]
        second_prompt = mock_step.call_args_list[1].kwargs["prompt"]
        assert first_prompt == "base prompt"
        assert "Retry Constraints" in second_prompt
        assert mock_step.call_args_list[0].kwargs["timeout"] == 999
        assert mock_step.call_args_list[0].kwargs["max_turns"] == 77
        assert mock_step.call_args_list[0].kwargs["idle_timeout"] == 555
        assert mock_step.call_args_list[0].kwargs["max_duration"] == 8888

    @patch("app.rebase_pr.get_skill_timeout", return_value=999)
    @patch("app.rebase_pr.get_skill_max_turns", return_value=77)
    @patch("app.rebase_pr.get_rebase_ci_max_duration", return_value=8888)
    @patch("app.rebase_pr.get_rebase_ci_idle_timeout", return_value=555)
    @patch("app.rebase_pr.run_claude_step")
    def test_non_timeout_failure_does_not_retry(
        self, mock_step, mock_idle, mock_max_duration, mock_turns, mock_timeout,
    ):
        from app.claude_step import StepResult

        mock_step.return_value = StepResult(
            committed=False, output="", error="Exit code 1: no stderr",
        )
        actions = []
        result, timed_out, attempts = _run_ci_fix_step_with_timeout_retry(
            prompt="base prompt",
            project_path="/project",
            commit_msg="fix: test",
            success_label="ok",
            failure_label="failed",
            actions_log=actions,
            use_convention_subject=False,
        )

        assert result.committed is False
        assert timed_out is False
        assert attempts == 1
        mock_step.assert_called_once()


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

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._force_push")
    @patch("app.rebase_pr._run_ci_fix_step_with_timeout_retry")
    @patch("app.rebase_pr.wait_for_ci", side_effect=[
        ("failure", 456, "test FAILED"),
        ("success", 457, ""),
    ])
    def test_fix_attempt_includes_pr_link(self, mock_wait, mock_fix_step, mock_push, mock_state):
        from app.claude_step import StepResult

        mock_fix_step.return_value = (StepResult(committed=True, output=""), False, 1)
        messages = []
        _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), [], lambda m: messages.append(m),
        )
        # The "CI failed" notification should include the PR link
        ci_failed_msgs = [m for m in messages if "failed" in m.lower()]
        assert len(ci_failed_msgs) > 0
        assert all("owner/repo/pull/42" in m for m in ci_failed_msgs)


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

    @patch("app.rebase_pr.check_pr_state", return_value=("MERGED", "UNKNOWN"))
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_aborts_when_pr_merged(self, mock_wait, mock_state):
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "merged" in result.lower()
        assert any("merged" in a.lower() for a in actions)

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "CONFLICTING"))
    @patch("app.rebase_pr.wait_for_ci", return_value=("failure", 456, "error"))
    def test_aborts_when_pr_has_conflicts(self, mock_wait, mock_state):
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "conflict" in result.lower()
        assert any("conflict" in a.lower() for a in actions)

    @patch("app.rebase_pr.check_pr_state", return_value=("OPEN", "MERGEABLE"))
    @patch("app.rebase_pr._force_push")
    @patch("app.rebase_pr._run_ci_fix_step_with_timeout_retry")
    @patch("app.rebase_pr.wait_for_ci", side_effect=[
        ("failure", 456, "test FAILED"),
        ("success", 457, ""),
    ])
    def test_proceeds_when_pr_open_and_mergeable(
        self, mock_wait, mock_fix_step, mock_push, mock_state,
    ):
        from app.claude_step import StepResult

        mock_fix_step.return_value = (StepResult(committed=True, output=""), False, 1)
        actions = []
        result = _run_ci_check_and_fix(
            "koan/fix", "main", "owner/repo", "42", "/project",
            self._make_context(), actions, lambda m: None,
        )
        assert "fixed on attempt 1" in result
        mock_fix_step.assert_called_once()


class TestCheckPrState:
    """Tests for check_pr_state() helper."""

    @patch("app.rebase_pr.run_gh", return_value='{"state":"MERGED","mergeable":"UNKNOWN"}')
    def test_returns_merged(self, mock_gh):
        from app.rebase_pr import check_pr_state
        state, mergeable = check_pr_state("42", "owner/repo")
        assert state == "MERGED"
        assert mergeable == "UNKNOWN"

    @patch("app.rebase_pr.run_gh", return_value='{"state":"OPEN","mergeable":"CONFLICTING"}')
    def test_returns_conflicting(self, mock_gh):
        from app.rebase_pr import check_pr_state
        state, mergeable = check_pr_state("42", "owner/repo")
        assert state == "OPEN"
        assert mergeable == "CONFLICTING"

    @patch("app.rebase_pr.run_gh", side_effect=RuntimeError("API error"))
    def test_returns_unknown_on_error(self, mock_gh):
        from app.rebase_pr import check_pr_state
        state, mergeable = check_pr_state("42", "owner/repo")
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
        assert "### CI status" in result
        assert "CI passed." in result

    def test_no_ci_section_when_empty(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            ci_section="",
        )
        assert "### CI status" not in result


# ---------------------------------------------------------------------------
# Descriptive commit messages for review feedback (issue #964)
# ---------------------------------------------------------------------------

class TestApplyReviewFeedbackDescriptiveCommit:
    """_apply_review_feedback should return a change summary from Claude's output."""

    @patch("app.rebase_pr.run_claude_step")
    def test_returns_change_summary(self, mock_step):
        """When Claude produces changes, _apply_review_feedback returns the summary."""
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(
            committed=True,
            output="Refactored auth to use JWT tokens and updated tests.",
        )
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

    @patch("app.rebase_pr.run_claude_step")
    def test_returns_empty_when_no_changes(self, mock_step):
        """When Claude produces no changes, returns empty string."""
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=False, output="No changes needed.")
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

    @patch("app.rebase_pr.run_claude_step")
    def test_passes_correct_commit_msg(self, mock_step):
        """The commit_msg passed to run_claude_step should follow the convention."""
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=True, output="Updated error handling.")
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix error handling",
            "reviews": "", "issue_comments": "",
        }
        _apply_review_feedback(
            context, "42", "/project", [],
            skill_dir=REBASE_SKILL_DIR,
        )
        call_kwargs = mock_step.call_args.kwargs
        assert call_kwargs["commit_msg"] == "rebase: apply review feedback on #42"


class TestApplyReviewFeedbackConventionAware:
    """_apply_review_feedback should pass use_convention_subject to run_claude_step
    and strip COMMIT_SUBJECT from the returned change summary."""

    @patch("app.rebase_pr.run_claude_step")
    def test_enables_convention_subject_when_conventions_provided(self, mock_step):
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=True, output="Fixed it.")
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        _apply_review_feedback(
            context, "42", "/project", [],
            skill_dir=REBASE_SKILL_DIR,
            commit_conventions="## Commit Conventions\nUse Case PROJECT-XXXXX.",
        )
        call_kwargs = mock_step.call_args.kwargs
        assert call_kwargs["use_convention_subject"] is True

    @patch("app.rebase_pr.run_claude_step")
    def test_no_convention_subject_without_conventions(self, mock_step):
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(committed=True, output="Fixed it.")
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        _apply_review_feedback(
            context, "42", "/project", [],
            skill_dir=REBASE_SKILL_DIR,
        )
        call_kwargs = mock_step.call_args.kwargs
        assert call_kwargs["use_convention_subject"] is False

    @patch("app.rebase_pr.run_claude_step")
    def test_strips_subject_line_from_summary(self, mock_step):
        """The COMMIT_SUBJECT line should not appear in the returned summary."""
        from app.claude_step import StepResult
        mock_step.return_value = StepResult(
            committed=True,
            output=(
                "Fixed auth bug.\n"
                "COMMIT_SUBJECT: Case PROJECT-123 Fix auth\n"
                "More details here."
            ),
        )
        context = {
            "title": "Fix", "body": "", "branch": "br", "base": "main",
            "diff": "+code", "review_comments": "fix this",
            "reviews": "", "issue_comments": "",
        }
        summary = _apply_review_feedback(
            context, "42", "/project", [],
            skill_dir=REBASE_SKILL_DIR,
            commit_conventions="## Commit Conventions\nUse Case prefix.",
        )
        assert "COMMIT_SUBJECT:" not in summary
        assert "Fixed auth bug" in summary


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
        assert "### Changes applied" in result
        assert "Fixed typo in error message" in result

    def test_no_change_summary_when_empty(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
            change_summary="",
        )
        assert "### Changes applied" not in result

    def test_no_change_summary_when_none(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
        )
        assert "### Changes applied" not in result


class TestRunRebasePassesChangeSummary:
    """run_rebase should pass the change summary from _apply_review_feedback
    through to _build_rebase_comment."""

    @pytest.fixture(autouse=True)
    def mock_already_solved(self):
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)):
            yield

    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._build_rebase_comment")
    @patch("app.rebase_pr._apply_review_feedback", return_value="Fixed the auth bug.")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_summary_forwarded_to_comment(
        self, mock_ctx, mock_apply, mock_comment, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
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
                       skill_dir=REBASE_SKILL_DIR, fix=True)
            # Verify _build_rebase_comment was called with change_summary
            call_kwargs = mock_comment.call_args
            assert call_kwargs[1].get("change_summary") == "Fixed the auth bug."


class TestRunRebasePrivateReviewGate:
    @pytest.fixture(autouse=True)
    def mock_already_solved(self):
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)):
            yield

    def _context(self):
        return {
            "title": "Fix auth",
            "body": "",
            "branch": "feat",
            "base": "main",
            "state": "OPEN",
            "author": "",
            "url": "https://github.com/o/r/pull/1",
            "diff": "+code",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
            "head_owner": "o",
        }

    @patch("app.commit_conventions.get_project_commit_guidance", return_value="")
    @patch("app.rebase_pr._enqueue_ci_check", return_value="CI queued")
    @patch("app.rebase_pr._get_diffstat", return_value="1 file changed")
    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_runs_gate_after_push_and_before_ci_enqueue(
        self, mock_ctx, _mock_gh, _mock_safe, _mock_fix_ci, _mock_diffstat,
        mock_enqueue, _mock_conventions, tmp_path,
    ):
        mock_ctx.return_value = self._context()
        events = []

        def push_side_effect(*_args, **_kwargs):
            events.append("push")
            return {"success": True, "actions": ["Force-pushed"], "error": ""}

        def gate_side_effect(**kwargs):
            events.append("gate")
            assert kwargs["skill_origin"] == "rebase"
            assert kwargs["review_skill_dir"] == REBASE_SKILL_DIR.parent / "review"
            return SimpleNamespace(
                ran=True,
                summary="Private review gate passed",
                skipped_reason="",
            )

        def enqueue_side_effect(*_args, **_kwargs):
            events.append("ci")
            return "CI queued"

        mock_enqueue.side_effect = enqueue_side_effect

        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch", return_value="origin"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._run_git", return_value="abc123\n"), \
             patch("app.rebase_pr._push_with_fallback", side_effect=push_side_effect), \
             patch(
                 "app.private_review_gate.run_private_review_gate",
                 side_effect=gate_side_effect,
             ):
            success, summary = run_rebase(
                "o", "r", "1", str(tmp_path), notify_fn=MagicMock(),
                skill_dir=REBASE_SKILL_DIR,
            )

        assert success is True
        assert events == ["push", "gate", "ci"]
        assert "Private review gate passed" in summary

    @patch("app.commit_conventions.get_project_commit_guidance", return_value="")
    @patch("app.rebase_pr._enqueue_ci_check", return_value="CI queued")
    @patch("app.rebase_pr._get_diffstat", return_value="1 file changed")
    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_gate_uses_rebase_push_callback(
        self, mock_ctx, _mock_gh, _mock_safe, _mock_fix_ci, _mock_diffstat,
        _mock_enqueue, _mock_conventions, tmp_path,
    ):
        mock_ctx.return_value = self._context()

        def gate_side_effect(**kwargs):
            kwargs["push_fn"]()
            return SimpleNamespace(
                ran=True,
                summary="Private review gate passed after 1 fix round(s)",
                skipped_reason="",
            )

        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch", return_value="origin"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._run_git", return_value="abc123\n"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": "",
             }) as mock_push, \
             patch(
                 "app.private_review_gate.run_private_review_gate",
                 side_effect=gate_side_effect,
             ):
            success, summary = run_rebase(
                "o", "r", "1", str(tmp_path), notify_fn=MagicMock(),
                skill_dir=REBASE_SKILL_DIR,
            )

        assert success is True
        assert mock_push.call_count == 2
        assert "after 1 fix round" in summary

    @patch("app.commit_conventions.get_project_commit_guidance", return_value="")
    @patch("app.rebase_pr._enqueue_ci_check", return_value="CI queued")
    @patch("app.rebase_pr._get_diffstat", return_value="1 file changed")
    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_gate_failure_is_non_blocking(
        self, mock_ctx, _mock_gh, _mock_safe, _mock_fix_ci, _mock_diffstat,
        _mock_enqueue, _mock_conventions, tmp_path,
    ):
        mock_ctx.return_value = self._context()

        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch", return_value="origin"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._run_git", return_value="abc123\n"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": "",
             }), \
             patch(
                 "app.private_review_gate.run_private_review_gate",
                 side_effect=RuntimeError("review crashed"),
             ):
            success, summary = run_rebase(
                "o", "r", "1", str(tmp_path), notify_fn=MagicMock(),
                skill_dir=REBASE_SKILL_DIR,
            )

        assert success is True
        assert "Private review gate failed after rebase" in summary
        assert "non-fatal" in summary

# ---------------------------------------------------------------------------
# _check_if_already_solved
# ---------------------------------------------------------------------------

class TestCheckIfAlreadySolved:
    """Unit tests for _check_if_already_solved()."""

    _PR_CONTEXT = {
        "title": "Fix auth bug",
        "body": "Fixes a login issue.",
        "branch": "koan/fix-auth",
        "base": "main",
        "diff": "+fix",
        "review_comments": "",
        "reviews": "",
        "issue_comments": "",
    }

    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f", "review": "r"})
    @patch("app.rebase_pr._run_git", return_value="abc1234 fix auth login\ndef5678 refactor utils")
    def test_high_confidence_positive_returns_true(self, _git, _mc, _cmd, mock_claude):
        mock_claude.return_value = {
            "success": True,
            "output": '{"already_solved": true, "resolved_by": "https://github.com/o/r/pull/99", "confidence": "high", "reasoning": "PR #99 already fixed this."}',
            "error": "",
        }
        actions = []
        result, resolved_by = _check_if_already_solved(actions, self._PR_CONTEXT, REBASE_SKILL_DIR, "/project")
        assert result is True
        assert resolved_by == "https://github.com/o/r/pull/99"
        assert any("positive" in a for a in actions)

    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f", "review": "r"})
    @patch("app.rebase_pr._run_git", return_value="abc1234 some commit")
    def test_negative_returns_false(self, _git, _mc, _cmd, mock_claude):
        mock_claude.return_value = {
            "success": True,
            "output": '{"already_solved": false, "resolved_by": null, "confidence": "high", "reasoning": "Work is still needed."}',
            "error": "",
        }
        actions = []
        result, resolved_by = _check_if_already_solved(actions, self._PR_CONTEXT, REBASE_SKILL_DIR, "/project")
        assert result is False
        assert resolved_by is None

    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f", "review": "r"})
    @patch("app.rebase_pr._run_git", return_value="")
    def test_medium_confidence_skipped(self, _git, _mc, _cmd, mock_claude):
        """Medium confidence should NOT close the PR."""
        mock_claude.return_value = {
            "success": True,
            "output": '{"already_solved": true, "resolved_by": "abc1234", "confidence": "medium", "reasoning": "Possibly."}',
            "error": "",
        }
        actions = []
        result, _ = _check_if_already_solved(actions, self._PR_CONTEXT, REBASE_SKILL_DIR, "/project")
        assert result is False
        assert any("skipped" in a.lower() or "not high" in a.lower() for a in actions)

    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f", "review": "r"})
    @patch("app.rebase_pr._run_git", return_value="")
    def test_claude_failure_returns_false(self, _git, _mc, _cmd, mock_claude):
        mock_claude.return_value = {"success": False, "output": "", "error": "timeout"}
        actions = []
        result, _ = _check_if_already_solved(actions, self._PR_CONTEXT, REBASE_SKILL_DIR, "/project")
        assert result is False
        assert any("skipped" in a for a in actions)

    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f", "review": "r"})
    @patch("app.rebase_pr._run_git", return_value="")
    def test_malformed_json_returns_false(self, _git, _mc, _cmd, mock_claude):
        mock_claude.return_value = {
            "success": True,
            "output": "This is not JSON at all.",
            "error": "",
        }
        actions = []
        result, _ = _check_if_already_solved(actions, self._PR_CONTEXT, REBASE_SKILL_DIR, "/project")
        assert result is False

    @patch("app.rebase_pr.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--fake"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f", "review": "r"})
    @patch("app.rebase_pr._run_git", return_value="")
    def test_json_embedded_in_text_is_parsed(self, _git, _mc, _cmd, mock_claude):
        """JSON embedded in verbose output should still be parsed."""
        mock_claude.return_value = {
            "success": True,
            "output": 'Let me analyze... {"already_solved": true, "resolved_by": "abc1234", "confidence": "high", "reasoning": "Fixed."} Done.',
            "error": "",
        }
        actions = []
        result, resolved_by = _check_if_already_solved(actions, self._PR_CONTEXT, REBASE_SKILL_DIR, "/project")
        assert result is True
        assert resolved_by == "abc1234"


# ---------------------------------------------------------------------------
# _close_pr_as_duplicate
# ---------------------------------------------------------------------------

class TestClosePrAsDuplicate:
    """Unit tests for _close_pr_as_duplicate()."""

    _PR_CONTEXT = {
        "title": "Fix auth bug",
        "body": "Fixes the login issue.\n\nCloses #123",
        "branch": "koan/fix-auth",
        "base": "main",
    }

    @patch("app.rebase_pr.run_gh")
    def test_posts_comment_and_closes_pr(self, mock_gh):
        _close_pr_as_duplicate(
            owner="o", repo="r", pr_number="42",
            resolved_by="https://github.com/o/r/pull/99",
            pr_context={"title": "Fix", "body": ""},
            project_path="/project",
        )
        gh_calls = [call[0] for call in mock_gh.call_args_list]
        # Must comment on the PR
        assert any(c[0] == "pr" and c[1] == "comment" for c in gh_calls)
        # Must close the PR
        assert any(c[0] == "pr" and c[1] == "close" for c in gh_calls)

    @patch("app.rebase_pr.run_gh")
    def test_closes_linked_issue(self, mock_gh):
        _close_pr_as_duplicate(
            owner="o", repo="r", pr_number="42",
            resolved_by="abc1234",
            pr_context=self._PR_CONTEXT,
            project_path="/project",
        )
        gh_calls = [call[0] for call in mock_gh.call_args_list]
        # Must comment on and close the linked issue #123
        assert any(c[0] == "issue" and c[1] == "comment" and c[2] == "123" for c in gh_calls)
        assert any(c[0] == "issue" and c[1] == "close" and c[2] == "123" for c in gh_calls)

    @patch("app.rebase_pr.run_gh")
    def test_no_linked_issue_skips_issue_close(self, mock_gh):
        _close_pr_as_duplicate(
            owner="o", repo="r", pr_number="42",
            resolved_by="abc1234",
            pr_context={"title": "Fix", "body": "No issue reference here."},
            project_path="/project",
        )
        gh_calls = [call[0] for call in mock_gh.call_args_list]
        assert not any(c[0] == "issue" for c in gh_calls)

    @patch("app.rebase_pr.run_gh")
    def test_notify_fn_called(self, mock_gh):
        notify = MagicMock()
        _close_pr_as_duplicate(
            owner="o", repo="r", pr_number="42",
            resolved_by="https://github.com/o/r/pull/99",
            pr_context={"title": "Fix auth", "body": ""},
            project_path="/project",
            notify_fn=notify,
        )
        notify.assert_called_once()
        msg = notify.call_args[0][0]
        assert "42" in msg
        assert "closed" in msg.lower()

    @patch("app.rebase_pr.run_gh")
    def test_gh_failure_non_fatal(self, mock_gh):
        """run_gh errors should not propagate — the function is best-effort."""
        mock_gh.side_effect = RuntimeError("network error")
        # Should not raise
        _close_pr_as_duplicate(
            owner="o", repo="r", pr_number="42",
            resolved_by=None,
            pr_context={"title": "Fix", "body": ""},
            project_path="/project",
        )


# ---------------------------------------------------------------------------
# run_rebase — already-solved integration
# ---------------------------------------------------------------------------

class TestRunRebaseAlreadySolved:
    """run_rebase should close PRs detected as already solved."""

    @patch("app.rebase_pr._close_pr_as_duplicate")
    @patch("app.rebase_pr._check_if_already_solved", return_value=(True, "https://github.com/o/r/pull/55"))
    @patch("app.rebase_pr._checkout_pr_branch")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_already_solved_closes_pr_without_checkout(
        self, mock_ctx, mock_checkout, mock_check, mock_close
    ):
        mock_ctx.return_value = {
            "title": "Fix auth", "body": "", "branch": "feat",
            "base": "main", "state": "OPEN", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        success, summary = run_rebase("o", "r", "42", "/project", notify_fn=notify)
        assert success is False
        assert "already solved" in summary.lower()
        mock_close.assert_called_once()
        # Checkout must NOT have been called
        mock_checkout.assert_not_called()

    @patch("app.rebase_pr._check_if_already_solved", return_value=(False, None))
    @patch("app.rebase_pr._close_pr_as_duplicate")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_not_already_solved_continues_rebase(
        self, mock_ctx, mock_close, mock_check
    ):
        mock_ctx.return_value = {
            "title": "T", "body": "", "branch": "feat",
            "base": "main", "state": "OPEN", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch") as mock_checkout, \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }), \
             patch("app.rebase_pr.run_gh"), \
             patch("app.rebase_pr._safe_checkout"), \
             patch("app.rebase_pr._run_ci_check_and_fix", return_value=""), \
             patch("app.rebase_pr._fix_existing_ci_failures", return_value=False):
            success, _ = run_rebase("o", "r", "1", "/project", notify_fn=notify)
        mock_close.assert_not_called()
        mock_checkout.assert_called_once()


# ---------------------------------------------------------------------------
# Severity filter — parse_severity / severity_at_or_above
# ---------------------------------------------------------------------------

class TestParseSeverity:
    def test_canonical_names(self):
        assert parse_severity("critical") == "critical"
        assert parse_severity("warning") == "warning"
        assert parse_severity("suggestion") == "suggestion"

    def test_aliases(self):
        assert parse_severity("important") == "warning"
        assert parse_severity("blocking") == "critical"
        assert parse_severity("all") == "suggestion"
        assert parse_severity("suggestions") == "suggestion"

    def test_case_insensitive(self):
        assert parse_severity("Critical") == "critical"
        assert parse_severity("IMPORTANT") == "warning"

    def test_strips_dashes(self):
        assert parse_severity("-critical") == "critical"
        assert parse_severity("--important") == "warning"
        assert parse_severity("---warning") == "warning"

    def test_strips_em_dash(self):
        assert parse_severity("\u2014critical") == "critical"
        assert parse_severity("\u2013important") == "warning"

    def test_unknown_returns_none(self):
        assert parse_severity("unknown") is None
        assert parse_severity("") is None
        assert parse_severity("---") is None


class TestSeverityAtOrAbove:
    def test_critical_only(self):
        assert severity_at_or_above("critical") == ["critical"]

    def test_warning_and_above(self):
        assert severity_at_or_above("warning") == ["critical", "warning"]

    def test_suggestion_includes_all(self):
        assert severity_at_or_above("suggestion") == ["critical", "warning", "suggestion"]

    def test_unknown_returns_all(self):
        assert severity_at_or_above("bogus") == list(SEVERITY_LEVELS)


class TestBuildRebasePromptSeverityFilter:
    def test_no_filter_by_default(self):
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(context, skill_dir=REBASE_SKILL_DIR)
        assert "Severity Filter" not in prompt

    def test_critical_filter_appended(self):
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(
            context, skill_dir=REBASE_SKILL_DIR, min_severity="critical",
        )
        assert "Severity Filter" in prompt
        assert "critical" in prompt.lower()
        assert "Skip" in prompt

    def test_warning_filter_includes_critical(self):
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(
            context, skill_dir=REBASE_SKILL_DIR, min_severity="warning",
        )
        assert "Severity Filter" in prompt
        assert "critical" in prompt
        assert "warning" in prompt
        assert "suggestion" in prompt  # mentioned as skipped

    def test_suggestion_filter_not_appended(self):
        """suggestion means 'all' — no filter needed."""
        context = {
            "title": "T", "body": "", "branch": "br", "base": "main",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        prompt = _build_rebase_prompt(
            context, skill_dir=REBASE_SKILL_DIR, min_severity="suggestion",
        )
        assert "Severity Filter" not in prompt


class TestMainMinSeverity:
    def test_passes_min_severity_to_run_rebase(self):
        with patch("app.rebase_pr.run_rebase", return_value=(True, "OK")) as mock:
            rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
                "--min-severity", "warning",
            ])
            assert mock.call_args[1]["min_severity"] == "warning"

    def test_no_min_severity_by_default(self):
        with patch("app.rebase_pr.run_rebase", return_value=(True, "OK")) as mock:
            rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert mock.call_args[1]["min_severity"] is None


class TestMainFix:
    def test_passes_fix_to_run_rebase(self):
        with patch("app.rebase_pr.run_rebase", return_value=(True, "OK")) as mock:
            rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
                "--fix",
            ])
            assert mock.call_args[1]["fix"] is True

    def test_no_fix_by_default(self):
        with patch("app.rebase_pr.run_rebase", return_value=(True, "OK")) as mock:
            rebase_main([
                "https://github.com/sukria/koan/pull/42",
                "--project-path", "/project",
            ])
            assert mock.call_args[1]["fix"] is False


class TestFeedbackGate:
    """Step 4 (apply review feedback) runs only when fix=True or the default is on."""

    @pytest.fixture(autouse=True)
    def mock_already_solved(self):
        with patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)):
            yield

    _CTX = {
        "title": "T", "body": "", "branch": "feat",
        "base": "main", "state": "", "author": "", "url": "",
        "diff": "+code", "review_comments": "@reviewer: fix this",
        "reviews": "@reviewer (CHANGES_REQUESTED): please fix",
        "issue_comments": "",
    }

    @patch("app.rebase_pr._FEEDBACK_ON_BY_DEFAULT", False)
    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_bare_rebase_skips_feedback(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
    ):
        mock_ctx.return_value = dict(self._CTX)
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, _ = run_rebase("o", "r", "1", "/p", notify_fn=MagicMock(), fix=False)
            assert success is True
            mock_apply.assert_not_called()

    @patch("app.rebase_pr._FEEDBACK_ON_BY_DEFAULT", False)
    @patch("app.rebase_pr._fix_existing_ci_failures", return_value=False)
    @patch("app.rebase_pr._run_ci_check_and_fix", return_value="")
    @patch("app.rebase_pr._safe_checkout")
    @patch("app.rebase_pr.run_gh")
    @patch("app.rebase_pr._apply_review_feedback")
    @patch("app.rebase_pr.fetch_pr_context")
    def test_fix_applies_feedback(
        self, mock_ctx, mock_apply, mock_gh, mock_safe, mock_ci_check, mock_fix_ci,
    ):
        mock_ctx.return_value = dict(self._CTX)
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._checkout_pr_branch"), \
             patch("app.rebase_pr._rebase_with_conflict_resolution", return_value="origin"), \
             patch("app.rebase_pr._push_with_fallback", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": ""
             }):
            success, _ = run_rebase("o", "r", "1", "/p", notify_fn=MagicMock(), fix=True)
            assert success is True
            mock_apply.assert_called_once()


class TestFilterBotIssueComments:
    """Tests for _filter_bot_issue_comments."""

    def test_removes_third_party_bot_comments(self):
        raw = (
            "@human: Please fix this bug\n"
            "@github-actions[bot]: ## CI run results\n"
            "All checks passed.\n"
            "### Stats\n"
            "7 files changed\n"
            "@human: Now add config option\n"
            "@human: @koan-bot rebase"
        )
        with patch("app.rebase_pr._resolve_own_login", return_value="koan-bot"):
            result = _filter_bot_issue_comments(raw)
        assert "@human: Please fix this bug" in result
        assert "@human: Now add config option" in result
        assert "@github-actions[bot]:" not in result
        assert "All checks passed" not in result

    def test_keeps_own_identity_comments(self):
        # Our own prior review/rebase feedback must survive filtering so a
        # later rebase can act on it (review + rebase flow).
        raw = (
            "@human: Please fix this bug\n"
            "@koan-bot: ## Review feedback from last iteration\n"
            "Consider renaming the helper.\n"
            "@github-actions[bot]: CI noise"
        )
        with patch("app.rebase_pr._resolve_own_login", return_value="koan-bot"):
            result = _filter_bot_issue_comments(raw)
        assert "@koan-bot: ## Review feedback from last iteration" in result
        assert "Consider renaming the helper." in result
        assert "@github-actions[bot]: CI noise" not in result

    def test_keeps_own_identity_even_with_bot_suffix(self):
        # If our configured identity is a GitHub App (login ends in [bot]),
        # it is still exempt from filtering; other [bot] authors are removed.
        raw = "@koan-app[bot]: our prior feedback\n@other[bot]: third-party noise"
        with patch("app.rebase_pr._resolve_own_login", return_value="koan-app[bot]"):
            result = _filter_bot_issue_comments(raw)
        assert "@koan-app[bot]: our prior feedback" in result
        assert "@other[bot]: third-party noise" not in result

    def test_no_own_login_keeps_non_bot_authors(self):
        raw = "@bot: some text\n@human: other text"
        with patch("app.rebase_pr._resolve_own_login", return_value=""):
            result = _filter_bot_issue_comments(raw)
        assert result == raw

    def test_empty_input(self):
        with patch("app.rebase_pr._resolve_own_login", return_value="koan-bot"):
            assert _filter_bot_issue_comments("") == ""


class TestTruncateRecent:
    """Tests for _truncate_recent — tail-prioritized truncation."""

    def test_short_text_unchanged(self):
        assert _truncate_recent("short text", 1000) == "short text"

    def test_long_text_keeps_tail(self):
        early = "A" * 5000
        late = "RECENT_FEEDBACK"
        text = early + "\n" + late
        result = _truncate_recent(text, 200)
        assert "RECENT_FEEDBACK" in result
        assert result.startswith("(earlier comments truncated)")
        assert len(result) <= 200

    def test_recent_user_comment_preserved(self):
        """Simulates the real bug: bot comments bloat early text, user
        comment at the end gets truncated by head-based truncation."""
        bot_noise = "Bot rebase summary " * 200  # ~3800 chars
        user_request = "@human: Add config option for review_compressor"
        text = bot_noise + "\n" + user_request
        result = _truncate_recent(text, 3000)
        assert "review_compressor" in result


# ---------------------------------------------------------------------------
# _build_rebase_recovery_guidance
# ---------------------------------------------------------------------------

class TestBuildRebaseRecoveryGuidance:
    def test_includes_branch_and_dirty_state(self):
        with patch("app.rebase_pr._get_current_branch", return_value="koan/fix-auth"), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False), \
             patch("app.rebase_pr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=" M file.py\n",
            )
            result = _build_rebase_recovery_guidance("/project")
            assert "koan/fix-auth" in result
            assert "dirty: yes" in result.lower() or "yes" in result

    def test_rebase_in_progress_guidance(self):
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=True), \
             patch("app.rebase_pr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _build_rebase_recovery_guidance("/project")
            assert "rebase --continue" in result or "rebase --abort" in result

    def test_branch_detection_failure_logs_error(self, capsys):
        with patch("app.rebase_pr._get_current_branch", side_effect=RuntimeError("git broke")), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False), \
             patch("app.rebase_pr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _build_rebase_recovery_guidance("/project")
            assert "unknown" in result
            captured = capsys.readouterr()
            assert "branch detection failed" in captured.err

    def test_git_status_timeout_logs_error(self, capsys):
        with patch("app.rebase_pr._get_current_branch", return_value="main"), \
             patch("app.rebase_pr._has_rebase_in_progress", return_value=False), \
             patch("app.rebase_pr.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 15)):
            result = _build_rebase_recovery_guidance("/project")
            assert "unknown" in result.lower() or "dirty" in result.lower()
            captured = capsys.readouterr()
            assert "git status failed" in captured.err


class TestEnqueueCiCheckConfigGate:
    """Verify _enqueue_ci_check respects ci_check.enabled config."""

    def test_skips_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        actions = []
        with patch("app.config.is_ci_check_enabled", return_value=False):
            result = _enqueue_ci_check(
                branch="koan/fix",
                full_repo="owner/repo",
                pr_number="42",
                project_path=str(tmp_path),
                context={"url": "https://github.com/owner/repo/pull/42"},
                actions_log=actions,
            )
        assert "disabled" in result.lower()
        assert any("disabled" in a.lower() for a in actions)


class TestRebaseOutcomeGating:
    def test_success_emits_single_outcome_with_pr_url(self, monkeypatch):
        import app.rebase_pr as rp
        sent = []
        monkeypatch.setattr("app.messaging_level.is_debug", lambda: False)
        monkeypatch.setattr("app.notify.send_telegram", lambda m, **k: sent.append(m))
        monkeypatch.setattr(rp, "_run_rebase_impl", lambda *a, **k: (True, "done"))
        ok, summary = rp.run_rebase("o", "r", "7", "/tmp/x")
        assert ok is True
        assert sent == ["✅ Rebased https://github.com/o/r/pull/7"]

    def test_failure_emits_outcome_with_context(self, monkeypatch):
        import app.rebase_pr as rp
        sent = []
        monkeypatch.setattr("app.messaging_level.is_debug", lambda: False)
        monkeypatch.setattr("app.notify.send_telegram", lambda m, **k: sent.append(m))
        monkeypatch.setattr(rp, "_run_rebase_impl", lambda *a, **k: (False, "boom"))
        ok, summary = rp.run_rebase("o", "r", "7", "/tmp/x")
        assert ok is False
        assert len(sent) == 1
        assert "https://github.com/o/r/pull/7" in sent[0] and "boom" in sent[0]

    def test_progress_suppressed_under_normal(self, monkeypatch):
        import app.rebase_pr as rp
        sent = []
        monkeypatch.setattr("app.messaging_level.is_debug", lambda: False)
        monkeypatch.setattr("app.notify.send_telegram", lambda m, **k: sent.append(m))

        def _impl(owner, repo, pr_number, project_path, notify_fn=None, **k):
            notify_fn("Reading PR #7...")
            return True, "done"

        monkeypatch.setattr(rp, "_run_rebase_impl", _impl)
        rp.run_rebase("o", "r", "7", "/tmp/x")
        assert not any("..." in m for m in sent)  # progress gated
        assert sent == ["✅ Rebased https://github.com/o/r/pull/7"]


# ---------------------------------------------------------------------------
# Force-push content-preservation guard integration
# ---------------------------------------------------------------------------

class TestPushGuardComment:
    def test_section_rendered_at_top(self):
        section = "> [!CAUTION]\n> content was dropped"
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main", "Force-pushed `koan/fix` to origin"],
            {"title": "Fix bug"},
            diffstat="1 file changed",
            push_guard_section=section,
        )
        assert section in result
        # The guard alert comes before every other section of the comment.
        assert result.index("[!CAUTION]") < result.index("### Stats")
        assert result.index("[!CAUTION]") < result.index("<details>")

    def test_no_section_by_default(self):
        result = _build_rebase_comment(
            "42", "koan/fix", "main",
            ["Rebased onto origin/main"],
            {"title": "Fix bug"},
        )
        assert "[!CAUTION]" not in result


class TestPushWithFallbackGuardKeys:
    def test_success_reports_remote_observed_and_pushed_heads(self):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step.subprocess.run", return_value=mock_result), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 return_value="cafe" * 10,
             ) as observe:
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project",
            )
        assert result["success"] is True
        assert result["remote"] == "origin"
        assert result["pre_push_remote_heads"] == ["cafe" * 10]
        assert result["pushed_head"] == "beef" * 10
        observe.assert_called_once_with("origin", "koan/fix", "/project")

    def test_lease_rejection_records_the_tip_that_caused_it(self):
        """A rejected --force-with-lease means the remote moved after the first
        observation; the tip the plain --force is about to clobber must be
        observed too, or the guard can never name it."""
        def mock_run(cmd, **kwargs):
            if "--force-with-lease" in cmd:
                raise RuntimeError("stale info")
            return MagicMock(returncode=0, stdout="", stderr="")

        observed = ["cafe" * 10, "dead" * 10]  # tip before, tip after it moved
        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 side_effect=observed,
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project",
            )
        assert result["success"] is True
        assert result["pre_push_remote_heads"] == observed

    def test_unchanged_tip_is_not_observed_twice(self):
        """The lease can also fail for reasons other than a moved tip — the
        same SHA must not be listed twice."""
        def mock_run(cmd, **kwargs):
            if "--force-with-lease" in cmd:
                raise RuntimeError("stale info")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 return_value="cafe" * 10,
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": "https://..."}, "/project",
            )
        assert result["pre_push_remote_heads"] == ["cafe" * 10]

    def test_observations_of_a_failed_remote_are_not_reported(self):
        """A tip seen on a remote whose push then failed was never overwritten;
        reporting it would raise a false 'concurrent push clobbered' alarm."""
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"] and "origin" in cmd:
                raise RuntimeError("permission denied")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 side_effect=["cafe" * 10, "cafe" * 10, "dead" * 10],
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project",
            )
        assert result["remote"] == "upstream"
        assert result["pre_push_remote_heads"] == ["dead" * 10]

    def test_unobservable_remote_is_flagged_not_assumed_clean(self):
        """`ls-remote` failing is 'could not look', not 'nothing was there'."""
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step.subprocess.run", return_value=mock_result), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 return_value=None,
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project",
            )
        assert result["pre_push_remote_heads"] == []
        assert result["observation_failed"] is True

    def test_observation_crash_does_not_block_the_push(self):
        """The observation runs on the live push path. The guard detects and
        warns — an unexpected failure inside it must not gate the push, divert
        it to another remote, or fail the rebase."""
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.claude_step.subprocess.run", return_value=mock_result), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 side_effect=AttributeError("guard bug"),
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project",
            )
        assert result["success"] is True
        assert result["remote"] == "origin"  # not diverted to the fallback
        assert result["pushed_head"] == "beef" * 10
        assert result["observation_failed"] is True

    def test_lease_hook_crash_still_pushes_and_reports_unverified(self):
        """Same on the lease-failure re-observation, which runs between the
        rejected --force-with-lease and the plain --force."""
        def mock_run(cmd, **kwargs):
            if "--force-with-lease" in cmd:
                raise RuntimeError("stale info")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 side_effect=["cafe" * 10, RuntimeError("guard bug")],
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project",
            )
        assert result["success"] is True
        assert result["pre_push_remote_heads"] == ["cafe" * 10]
        assert result["observation_failed"] is True

    def test_failed_push_reports_no_pushed_head(self):
        """A SHA that never reached the remote must not be reported as pushed."""
        def mock_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"]:
                raise RuntimeError("rejected")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("app.claude_step.subprocess.run", side_effect=mock_run), \
             patch("app.rebase_pr._run_git", return_value="beef" * 10 + "\n"), \
             patch(
                 "app.rebase_pr.force_push_guard.observe_remote_head",
                 return_value="cafe" * 10,
             ):
            result = _push_with_fallback(
                "koan/fix", "main", "sukria/koan", "42",
                {"title": "Fix", "url": ""}, "/project",
            )
        assert result["success"] is False
        assert not result.get("pushed_head")


class TestRunForcePushGuard:
    def _run(self, *, pre_rebase_head="a" * 40, findings="unused",
             section="", actions_log=None, notify_calls=None):
        actions_log = actions_log if actions_log is not None else []
        notify_calls = notify_calls if notify_calls is not None else []
        with patch(
            "app.rebase_pr.force_push_guard.verify_content_preserved",
            return_value=findings,
        ), patch(
            "app.rebase_pr.force_push_guard.build_push_warning",
            return_value=section,
        ):
            result = _run_force_push_guard(
                pre_rebase_head=pre_rebase_head,
                target_ref="origin/main",
                project_path="/project",
                push_state={
                    "remote": "origin",
                    "pushed_head": "c" * 40,
                    "observations": ["b" * 40],
                },
                branch="koan/fix",
                pr_number="42",
                actions_log=actions_log,
                notify_fn=notify_calls.append,
            )
        return result, actions_log, notify_calls

    def test_findings_produce_section_action_and_notification(self):
        section = "> [!CAUTION]\n> boom"
        result, log, notified = self._run(section=section)
        assert result == section
        assert any("Force-push guard" in a and "see the warning" in a for a in log)
        assert len(notified) == 1
        assert "PR #42" in notified[0]

    def test_alert_is_not_progress_gated(self):
        """The guard finding is a safety outcome: it must reach the human even
        in normal mode, where progress messages are suppressed."""
        from app.messaging_level import progress_notify
        sent = []
        with patch("app.messaging_level.is_debug", return_value=False):
            notifier = progress_notify(sent.append, log_category="rebase")
            notifier("Pushing `koan/fix`...")  # progress — suppressed
            with patch(
                "app.rebase_pr.force_push_guard.verify_content_preserved",
                return_value="findings",
            ), patch(
                "app.rebase_pr.force_push_guard.build_push_warning",
                return_value="> [!CAUTION]\n> boom",
            ):
                _run_force_push_guard(
                    pre_rebase_head="a" * 40,
                    target_ref="origin/main",
                    project_path="/project",
                    push_state={"remote": "origin", "pushed_head": "c" * 40},
                    branch="koan/fix",
                    pr_number="42",
                    actions_log=[],
                    notify_fn=notifier,
                )
        assert len(sent) == 1
        assert "Force-push guard" in sent[0]

    def test_alert_delivery_failure_is_non_fatal(self):
        def boom(_msg):
            raise RuntimeError("telegram down")

        with patch(
            "app.rebase_pr.force_push_guard.verify_content_preserved",
            return_value="findings",
        ), patch(
            "app.rebase_pr.force_push_guard.build_push_warning",
            return_value="> [!CAUTION]\n> boom",
        ):
            section = _run_force_push_guard(
                pre_rebase_head="a" * 40,
                target_ref="origin/main",
                project_path="/project",
                push_state={"remote": "origin", "pushed_head": "c" * 40},
                branch="koan/fix",
                pr_number="42",
                actions_log=[],
                notify_fn=boom,
            )
        # The PR comment still carries the finding even if chat delivery failed.
        assert section == "> [!CAUTION]\n> boom"

    def test_clean_check_logs_preserved(self):
        result, log, notified = self._run(section="")
        assert result == ""
        assert any("verified preserved" in a for a in log)
        assert notified == []

    def test_guard_failure_is_non_fatal(self):
        result, log, notified = self._run(findings=None, section="")
        assert result == ""
        assert any("could not run" in a for a in log)
        assert notified == []

    def test_unexpected_guard_exception_does_not_fail_the_rebase(self):
        """The branch is already pushed by the time the guard runs: an
        unexpected failure must not turn a completed rebase into a failed
        mission or skip the PR comment that follows."""
        log = []
        with patch(
            "app.rebase_pr.force_push_guard.verify_content_preserved",
            side_effect=AttributeError("boom"),
        ):
            result = _run_force_push_guard(
                pre_rebase_head="a" * 40,
                target_ref="origin/main",
                project_path="/project",
                push_state={"remote": "origin", "pushed_head": "c" * 40},
                branch="koan/fix",
                pr_number="42",
                actions_log=log,
                notify_fn=lambda m: None,
            )
        assert result == ""
        assert any("unexpected guard failure" in a for a in log)

    def test_unexpected_render_exception_does_not_fail_the_rebase(self):
        log = []
        with patch(
            "app.rebase_pr.force_push_guard.verify_content_preserved",
            return_value="findings",
        ), patch(
            "app.rebase_pr.force_push_guard.build_push_warning",
            side_effect=TypeError("boom"),
        ):
            result = _run_force_push_guard(
                pre_rebase_head="a" * 40,
                target_ref="origin/main",
                project_path="/project",
                push_state={"remote": "origin", "pushed_head": "c" * 40},
                branch="koan/fix",
                pr_number="42",
                actions_log=log,
                notify_fn=lambda m: None,
            )
        assert result == ""
        assert any("unexpected guard failure" in a for a in log)

    def test_missing_baseline_skips(self):
        log = []
        result = _run_force_push_guard(
            pre_rebase_head="",
            target_ref="origin/main",
            project_path="/project",
            push_state={},
            branch="koan/fix",
            pr_number="42",
            actions_log=log,
            notify_fn=lambda m: None,
        )
        assert result == ""
        assert any("pre-rebase head was not captured" in a for a in log)


class TestGatePushStatePropagation:
    def test_gate_push_feeds_observation_into_push_state(self):
        """push_gate_fix must surface its own fresh observation + pushed SHA
        so the guard also covers the gate-push window (review finding #3)."""
        push_state = {
            "remote": "origin",
            "pushed_head": "old0" * 10,
            "observations": ["pre0" * 10],
        }

        def fake_gate(**kwargs):
            kwargs["push_fn"]()  # the gate pushes a fix
            return SimpleNamespace(ran=True, summary="ok", skipped_reason="")

        with patch("app.rebase_pr._push_with_fallback", return_value={
            "success": True,
            "actions": ["Force-pushed `koan/fix` to origin"],
            "error": "",
            "remote": "origin",
            "pre_push_remote_heads": ["obs1" * 10],
            "pushed_head": "new1" * 10,
        }), patch("app.private_review_gate.run_private_review_gate",
                  side_effect=fake_gate), \
             patch("app.utils.project_name_for_path", return_value="proj"):
            from app.rebase_pr import _run_rebase_private_review_gate
            _run_rebase_private_review_gate(
                owner="o", repo="r", pr_number="42", branch="koan/fix",
                base="main", full_repo="o/r", context={"url": ""},
                project_path="/tmp", head_remote=None,
                notify_fn=lambda m: None, skill_dir=None, actions_log=[],
                push_state=push_state,
            )

        assert push_state["remote"] == "origin"
        assert push_state["pushed_head"] == "new1" * 10
        assert push_state["observations"] == ["pre0" * 10, "obs1" * 10]

    def test_gate_push_to_another_remote_drops_stale_observations(self):
        """Observations only describe the remote they were taken on. If the
        gate lands somewhere else, the earlier tips belong to a branch this
        pipeline never overwrote — keeping them would be a false clobber."""
        push_state = {
            "remote": "origin",
            "pushed_head": "old0" * 10,
            "observations": ["pre0" * 10],
            "observation_failed": True,
        }

        def fake_gate(**kwargs):
            kwargs["push_fn"]()
            return SimpleNamespace(ran=True, summary="ok", skipped_reason="")

        with patch("app.rebase_pr._push_with_fallback", return_value={
            "success": True,
            "actions": [],
            "error": "",
            "remote": "upstream",
            "pre_push_remote_heads": ["obs1" * 10],
            "observation_failed": False,
            "pushed_head": "new1" * 10,
        }), patch("app.private_review_gate.run_private_review_gate",
                  side_effect=fake_gate), \
             patch("app.utils.project_name_for_path", return_value="proj"):
            from app.rebase_pr import _run_rebase_private_review_gate
            _run_rebase_private_review_gate(
                owner="o", repo="r", pr_number="42", branch="koan/fix",
                base="main", full_repo="o/r", context={"url": ""},
                project_path="/tmp", head_remote=None,
                notify_fn=lambda m: None, skill_dir=None, actions_log=[],
                push_state=push_state,
            )

        assert push_state["remote"] == "upstream"
        assert push_state["observations"] == ["obs1" * 10]
        assert push_state["observation_failed"] is False

    def test_gate_push_without_confirmed_sha_clears_pushed_head(self):
        """If the gate pushed but its SHA capture failed, the step-6 SHA is
        stale: keeping it would fake a post-push race. Clearing it makes the
        guard skip instead — the honest outcome."""
        push_state = {
            "remote": "origin",
            "pushed_head": "old0" * 10,
            "observations": ["pre0" * 10],
        }

        def fake_gate(**kwargs):
            kwargs["push_fn"]()
            return SimpleNamespace(ran=True, summary="ok", skipped_reason="")

        with patch("app.rebase_pr._push_with_fallback", return_value={
            "success": True,
            "actions": [],
            "error": "",
            "remote": "origin",
            "pre_push_remote_heads": [],
            "pushed_head": "",
        }), patch("app.private_review_gate.run_private_review_gate",
                  side_effect=fake_gate), \
             patch("app.utils.project_name_for_path", return_value="proj"):
            from app.rebase_pr import _run_rebase_private_review_gate
            _run_rebase_private_review_gate(
                owner="o", repo="r", pr_number="42", branch="koan/fix",
                base="main", full_repo="o/r", context={"url": ""},
                project_path="/tmp", head_remote=None,
                notify_fn=lambda m: None, skill_dir=None, actions_log=[],
                push_state=push_state,
            )

        assert push_state["pushed_head"] == ""


def _git_cmd(cwd, *args):
    """Run git in *cwd* with a deterministic identity, raising on failure."""
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _commit_file(repo, name, content, message):
    (repo / name).write_text(content)
    _git_cmd(repo, "add", name)
    _git_cmd(repo, "commit", "-m", message)


def _make_repo_hermetic(repo):
    """Pin identity/signing/hooks in *repo* so git works without a global config.

    The rebase pipeline shells out to plain ``git``, so it inherits none of
    ``_git_cmd``'s ``-c`` flags. On a CI runner there is no global identity and
    the auto-detected one is rejected (``user@host.(none)``), which would fail
    the replayed commits rather than the behaviour under test.
    """
    _git_cmd(repo, "config", "user.email", "t@t")
    _git_cmd(repo, "config", "user.name", "t")
    _git_cmd(repo, "config", "commit.gpgsign", "false")
    _git_cmd(repo, "config", "core.hooksPath", str(repo / ".no-hooks"))


class TestConcurrentPushDuringPrePushCiFix:
    """Step 5 (pre-push CI fix) is inside the guard's observation window.

    ``_fix_existing_ci_failures`` amends the local commit and never pushes —
    the pipeline's first force-push is Step 6, whose pre-push observation is
    taken *after* Step 5 returns. A commit someone else lands while the CI fix
    is running is therefore still observed, and named in the PR comment after
    the force-push erases it.
    """

    @pytest.fixture
    def repos(self, tmp_path):
        """Bare remote + seed clone (the human) + work clone (the pipeline)."""
        bare = tmp_path / "remote.git"
        bare.mkdir()
        _git_cmd(bare, "init", "--bare", "-b", "main", ".")

        seed = tmp_path / "seed"
        _git_cmd(tmp_path, "clone", str(bare), "seed")
        _make_repo_hermetic(seed)
        # An empty clone takes its branch name from init.defaultBranch on older
        # git, so name it explicitly rather than inheriting the runner's setting.
        _git_cmd(seed, "symbolic-ref", "HEAD", "refs/heads/main")
        _commit_file(seed, "a.txt", "one", "c1")
        _git_cmd(seed, "push", "origin", "main")
        _git_cmd(seed, "checkout", "-b", "feature")
        _commit_file(seed, "f1.txt", "f1", "feature work")
        _git_cmd(seed, "push", "origin", "feature")
        _git_cmd(seed, "checkout", "main")
        _commit_file(seed, "b.txt", "two", "c2")
        _git_cmd(seed, "push", "origin", "main")

        work = tmp_path / "work"
        _git_cmd(tmp_path, "clone", str(bare), "work")
        _make_repo_hermetic(work)
        return SimpleNamespace(bare=bare, seed=seed, work=work)

    def _context(self):
        return {
            "title": "Fix auth",
            "body": "",
            "branch": "feature",
            "base": "main",
            "state": "OPEN",
            "author": "",
            "url": "https://github.com/o/r/pull/1",
            "diff": "",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
            "head_owner": "o",
        }

    def test_commit_landing_during_ci_fix_is_named_in_pr_comment(self, repos):
        posted: list = []

        def fake_gh(*args, **_kwargs):
            posted.append(args)
            return ""

        def human_pushes_during_ci_fix(**_kwargs):
            """The Step 5 CI fix runs while a human lands a commit on the PR."""
            _git_cmd(repos.seed, "fetch", "origin")
            _git_cmd(repos.seed, "checkout", "feature")
            _commit_file(
                repos.seed, "hotfix.txt", "urgent", "human hotfix mid-rebase",
            )
            _git_cmd(repos.seed, "push", "origin", "feature")
            return False

        gate_skipped = SimpleNamespace(
            ran=False, summary="", skipped_reason="not a backend project",
        )

        with patch("app.rebase_pr.resolve_pr_location", return_value=("o", "r")), \
             patch("app.rebase_pr.fetch_pr_context", return_value=self._context()), \
             patch("app.rebase_pr._check_if_already_solved", return_value=(False, None)), \
             patch("app.commit_conventions.get_project_commit_guidance", return_value=""), \
             patch("app.rebase_pr._find_remote_for_repo", return_value="origin"), \
             patch("app.rebase_pr._fix_existing_ci_failures",
                   side_effect=human_pushes_during_ci_fix), \
             patch("app.rebase_pr._enqueue_ci_check", return_value=""), \
             patch("app.rebase_pr._get_diffstat", return_value=""), \
             patch("app.rebase_pr.run_gh", side_effect=fake_gh), \
             patch("app.private_review_gate.run_private_review_gate",
                   return_value=gate_skipped):
            success, summary = run_rebase(
                "o", "r", "1", str(repos.work), notify_fn=MagicMock(),
            )

        assert success is True, f"rebase pipeline failed: {summary}"
        comments = [a for a in posted if a[:2] == ("pr", "comment")]
        assert comments, "the pipeline must comment on the PR"
        body = comments[-1][-1]
        assert "human hotfix mid-rebase" in body
        assert "force-push" in body.lower()
        assert "guard" in summary.lower()
