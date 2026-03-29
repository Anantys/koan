"""Tests for git_sync.py — git awareness module."""

import subprocess
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.git_sync import run_git, GitSync, RECENT_BRANCH_DAYS

# Patch path for the lazy import of run_gh inside get_github_merged_branches.
_RUN_GH_PATH = "app.github.run_gh"


class TestRunGit:
    def test_returns_stdout(self, tmp_path):
        """run_git returns stdout of successful command."""
        result = run_git(str(tmp_path), "version")
        assert "git version" in result

    def test_returns_empty_on_failure(self, tmp_path):
        """run_git returns empty string on non-existent command."""
        result = run_git(str(tmp_path), "nonexistent-command-xyz")
        assert result == ""

    def test_returns_empty_on_timeout(self):
        """run_git returns empty on timeout."""
        with patch("app.git_sync._run_git_core", return_value=(1, "", "Git command timed out")):
            assert run_git("/tmp", "status") == ""


@pytest.fixture(autouse=True)
def default_prefix():
    """Ensure tests use default koan/ prefix."""
    with patch("app.git_sync._get_prefix", return_value="koan/"):
        yield


def _sync(project_path="/fake", instance_dir="", project_name=""):
    """Helper to create a GitSync instance for testing."""
    return GitSync(instance_dir, project_name, project_path)


class TestGetKoanBranches:
    def test_parses_local_and_remote(self):
        """Extracts koan/* branches from mixed branch listing."""
        mock_output = (
            "  koan/fix-bug\n"
            "* koan/current\n"
            "  remotes/origin/koan/fix-bug\n"
            "  remotes/origin/koan/other\n"
            "  main\n"
        )
        with patch("app.git_sync.run_git", return_value=mock_output):
            branches = _sync().get_koan_branches()
        assert "koan/fix-bug" in branches
        assert "koan/current" in branches
        assert "koan/other" in branches
        # No duplicates
        assert len([b for b in branches if b == "koan/fix-bug"]) == 1

    def test_empty_output(self):
        with patch("app.git_sync.run_git", return_value=""):
            assert _sync().get_koan_branches() == []

    def test_glob_pattern_uses_prefix_start(self):
        """Glob pattern must match prefix at start, not anywhere in the name.

        Bug: the old pattern f"*{prefix}*" would cause git branch --list to
        scan branches like "feature/fix-koan/stuff" that merely contain the
        prefix. The correct pattern is f"{prefix}*" (prefix-anchored).
        """
        with patch("app.git_sync.run_git", return_value="") as mock_git:
            _sync().get_koan_branches()
        # Verify the glob pattern passed to git branch --list
        call_args = mock_git.call_args
        assert call_args is not None
        git_args = call_args[0]  # positional args: (cwd, "branch", "-a", "--list", pattern)
        glob_pattern = git_args[-1]  # last argument is the pattern
        assert glob_pattern == "koan/*", f"Expected 'koan/*' but got '{glob_pattern}'"


class TestGetMergedBranches:
    def test_parses_merged(self):
        mock_output = "  remotes/origin/koan/done-feature\n  remotes/origin/koan/old-fix\n"
        with patch("app.git_sync.run_git", return_value=mock_output):
            merged = _sync().get_merged_branches()
        assert "koan/done-feature" in merged
        assert "koan/old-fix" in merged


    def test_glob_pattern_uses_prefix_start(self):
        """Glob pattern must match prefix at start for merged branches too."""
        with patch("app.git_sync.run_git", return_value="") as mock_git:
            _sync().get_merged_branches()
        # Find the call that includes "--list" (the branch listing call)
        list_calls = [
            c for c in mock_git.call_args_list
            if len(c[0]) >= 4 and "--list" in c[0]
        ]
        assert list_calls, "Expected at least one git branch --list call"
        glob_pattern = list_calls[0][0][-1]
        assert glob_pattern == "koan/*", f"Expected 'koan/*' but got '{glob_pattern}'"


class TestGetUnmergedBranches:
    def test_parses_unmerged(self):
        all_branches = "  koan/wip\n  remotes/origin/koan/pending-review\n  koan/merged-one\n"
        merged_output = "  koan/merged-one\n"

        def side_effect(cwd, *args):
            args_str = " ".join(args)
            if "rev-parse" in args_str:
                return "abc123"  # branch exists
            if "--merged" in args_str:
                return merged_output
            return all_branches

        with patch("app.git_sync.run_git", side_effect=side_effect):
            unmerged = _sync().get_unmerged_branches()
        assert "koan/wip" in unmerged
        assert "koan/pending-review" in unmerged
        assert "koan/merged-one" not in unmerged


class TestGetRecentMainCommits:
    def test_parses_commits(self):
        mock_output = "abc1234 fix: something\ndef5678 feat: other thing\n"
        with patch("app.git_sync.run_git", return_value=mock_output):
            commits = _sync().get_recent_main_commits()
        assert len(commits) == 2
        assert "abc1234 fix: something" in commits[0]

    def test_empty(self):
        with patch("app.git_sync.run_git", return_value=""):
            assert _sync().get_recent_main_commits() == []


class TestCleanupMergedBranches:
    """Tests for automatic cleanup of merged local branches."""

    def test_deletes_merged_local_branches(self):
        """Merged local branches are deleted via git branch -d."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"  # current branch
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/merged-one\n  koan/merged-two\n"
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                ["koan/merged-one", "koan/merged-two"]
            )
        assert deleted == ["koan/merged-one", "koan/merged-two"]

    def test_skips_current_branch(self):
        """Never deletes the branch we're currently on."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "koan/merged-one"  # we ARE on this branch
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/merged-one\n"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(["koan/merged-one"])
        assert deleted == []

    def test_skips_remote_only_branches(self):
        """Branches that only exist on remote are not deleted locally."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return ""  # no local branches
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(["koan/remote-only"])
        assert deleted == []

    def test_empty_merged_list(self):
        """No-op when there are no merged branches."""
        deleted = _sync().cleanup_merged_branches([])
        assert deleted == []

    def test_handles_delete_failure(self):
        """Branch not added to deleted list if git branch -d fails."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/stuck\n"
            if args[0] == "branch" and args[1] == "-d":
                return ""  # empty = failure
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(["koan/stuck"])
        assert deleted == []

    def test_custom_prefix(self):
        """Works with custom branch prefix like koan.atoomic/."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan.atoomic/done-feature\n"
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync._get_prefix", return_value="koan.atoomic/"):
            with patch("app.git_sync.run_git", side_effect=side_effect):
                deleted = _sync().cleanup_merged_branches(
                    ["koan.atoomic/done-feature"]
                )
        assert deleted == ["koan.atoomic/done-feature"]

    def test_mixed_local_and_remote(self):
        """Only deletes branches that exist locally."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/local-merged\n"  # only one local
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                ["koan/local-merged", "koan/remote-only-merged"]
            )
        assert deleted == ["koan/local-merged"]
        assert "koan/remote-only-merged" not in deleted


class TestGetBranchAges:
    """Tests for GitSync.get_branch_ages()."""

    def test_returns_empty_for_no_branches(self):
        assert _sync().get_branch_ages([]) == {}

    def test_parses_local_and_remote_refs(self):
        """Parses for-each-ref output and normalizes remote refs."""
        now = datetime.now().timestamp()
        two_days_ago = int(now - 2 * 86400)
        ten_days_ago = int(now - 10 * 86400)

        output = (
            f"{two_days_ago} koan/recent-branch\n"
            f"{ten_days_ago} origin/koan/old-branch\n"
        )
        with patch("app.git_sync.run_git", return_value=output):
            ages = _sync().get_branch_ages(["koan/recent-branch", "koan/old-branch"])

        assert ages["koan/recent-branch"] == 2
        assert ages["koan/old-branch"] == 10

    def test_keeps_most_recent_timestamp(self):
        """When a branch has both local and remote refs, use the newest."""
        now = datetime.now().timestamp()
        old_ts = int(now - 15 * 86400)
        new_ts = int(now - 1 * 86400)

        output = (
            f"{old_ts} koan/feature\n"
            f"{new_ts} origin/koan/feature\n"
        )
        with patch("app.git_sync.run_git", return_value=output):
            ages = _sync().get_branch_ages(["koan/feature"])

        assert ages["koan/feature"] == 1

    def test_omits_branches_not_found(self):
        """Branches not in for-each-ref output are omitted."""
        with patch("app.git_sync.run_git", return_value=""):
            ages = _sync().get_branch_ages(["koan/missing"])
        assert "koan/missing" not in ages

    def test_handles_malformed_lines(self):
        """Malformed for-each-ref lines are silently skipped."""
        now = datetime.now().timestamp()
        valid_ts = int(now - 3 * 86400)
        output = (
            f"{valid_ts} koan/good\n"
            "not-a-timestamp koan/bad\n"
            "single-field-only\n"
            "\n"
        )
        with patch("app.git_sync.run_git", return_value=output):
            ages = _sync().get_branch_ages(["koan/good", "koan/bad"])
        assert ages["koan/good"] == 3
        assert "koan/bad" not in ages


class TestSplitBranchesByRecency:
    """Tests for GitSync._split_branches_by_recency()."""

    def test_all_recent(self):
        """All branches within threshold are recent."""
        ages = {"koan/a": 1, "koan/b": 3}
        with patch.object(GitSync, "get_branch_ages", return_value=ages):
            recent, stale = _sync()._split_branches_by_recency(["koan/a", "koan/b"])
        assert recent == ["koan/a", "koan/b"]
        assert stale == []

    def test_all_stale(self):
        """All branches beyond threshold are stale."""
        ages = {"koan/old1": 30, "koan/old2": 60}
        with patch.object(GitSync, "get_branch_ages", return_value=ages):
            recent, stale = _sync()._split_branches_by_recency(["koan/old1", "koan/old2"])
        assert recent == []
        assert stale == ["koan/old1", "koan/old2"]

    def test_mixed_recent_and_stale(self):
        """Branches are correctly split by the threshold."""
        ages = {"koan/new": 2, "koan/old": 20}
        with patch.object(GitSync, "get_branch_ages", return_value=ages):
            recent, stale = _sync()._split_branches_by_recency(["koan/new", "koan/old"])
        assert recent == ["koan/new"]
        assert stale == ["koan/old"]

    def test_unknown_age_treated_as_recent(self):
        """Branches with unknown age are shown (conservative: don't hide)."""
        ages = {"koan/known": 2}  # koan/mystery not in ages
        with patch.object(GitSync, "get_branch_ages", return_value=ages):
            recent, stale = _sync()._split_branches_by_recency(
                ["koan/known", "koan/mystery"]
            )
        assert "koan/mystery" in recent
        assert "koan/known" in recent
        assert stale == []

    def test_custom_threshold(self):
        """Custom max_age_days is respected."""
        ages = {"koan/a": 2, "koan/b": 4}
        with patch.object(GitSync, "get_branch_ages", return_value=ages):
            recent, stale = _sync()._split_branches_by_recency(
                ["koan/a", "koan/b"], max_age_days=3
            )
        assert recent == ["koan/a"]
        assert stale == ["koan/b"]

    def test_boundary_age_is_recent(self):
        """Branch exactly at the threshold is still recent."""
        ages = {"koan/edge": RECENT_BRANCH_DAYS}
        with patch.object(GitSync, "get_branch_ages", return_value=ages):
            recent, stale = _sync()._split_branches_by_recency(["koan/edge"])
        assert recent == ["koan/edge"]
        assert stale == []


class TestGetGithubMergedBranches:
    """Tests for GitHub-based merged branch detection."""

    def test_returns_matching_branches(self):
        """Returns branches matching the agent prefix from merged PRs."""
        pr_json = '[{"headRefName": "koan/done-feature"}, {"headRefName": "koan/old-fix"}]'
        with patch(_RUN_GH_PATH, return_value=pr_json) as mock_gh:
            branches = _sync().get_github_merged_branches()
        assert branches == ["koan/done-feature", "koan/old-fix"]
        mock_gh.assert_called_once()

    def test_filters_non_agent_branches(self):
        """Only returns branches with the agent prefix."""
        pr_json = '[{"headRefName": "koan/my-pr"}, {"headRefName": "feature/other"}]'
        with patch(_RUN_GH_PATH, return_value=pr_json):
            branches = _sync().get_github_merged_branches()
        assert branches == ["koan/my-pr"]
        assert "feature/other" not in branches

    def test_returns_empty_on_error(self):
        """Returns empty list when gh CLI fails."""
        with patch(_RUN_GH_PATH, side_effect=RuntimeError("gh failed")):
            assert _sync().get_github_merged_branches() == []

    def test_returns_empty_on_invalid_json(self):
        """Returns empty list on malformed JSON."""
        with patch(_RUN_GH_PATH, return_value="not json"):
            assert _sync().get_github_merged_branches() == []

    def test_returns_empty_on_empty_response(self):
        """Returns empty list when gh returns empty string."""
        with patch(_RUN_GH_PATH, return_value=""):
            assert _sync().get_github_merged_branches() == []

    def test_returns_empty_on_non_list_response(self):
        """Returns empty list when response is not a JSON array."""
        with patch(_RUN_GH_PATH, return_value='{"error": "bad"}'):
            assert _sync().get_github_merged_branches() == []

    def test_handles_malformed_pr_entries(self):
        """Skips PR entries that aren't dicts or lack headRefName."""
        pr_json = '[{"headRefName": "koan/good"}, "bad", {"no_ref": true}, null]'
        with patch(_RUN_GH_PATH, return_value=pr_json):
            branches = _sync().get_github_merged_branches()
        assert branches == ["koan/good"]

    def test_deduplicates_branches(self):
        """Multiple PRs for the same branch are deduplicated."""
        pr_json = '[{"headRefName": "koan/x"}, {"headRefName": "koan/x"}]'
        with patch(_RUN_GH_PATH, return_value=pr_json):
            branches = _sync().get_github_merged_branches()
        assert branches == ["koan/x"]

    def test_custom_prefix(self):
        """Respects custom branch prefix."""
        pr_json = '[{"headRefName": "koan.atoomic/my-pr"}, {"headRefName": "koan/other"}]'
        with patch("app.git_sync._get_prefix", return_value="koan.atoomic/"):
            with patch(_RUN_GH_PATH, return_value=pr_json):
                branches = _sync().get_github_merged_branches()
        assert branches == ["koan.atoomic/my-pr"]

    def test_returns_empty_on_os_error(self):
        """Returns empty list when gh binary not found."""
        with patch(_RUN_GH_PATH, side_effect=OSError("not found")):
            assert _sync().get_github_merged_branches() == []


class TestCleanupWithGithubMerged:
    """Tests for cleanup_merged_branches with github_merged parameter."""

    def test_force_deletes_github_merged_branches(self):
        """GitHub-detected merges use git branch -D (force delete)."""
        delete_calls = []

        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/squash-merged\n"
            if args[0] == "branch" and args[1] == "-D":
                delete_calls.append(args[2])
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=[],
                github_merged=["koan/squash-merged"],
            )
        assert deleted == ["koan/squash-merged"]
        assert "koan/squash-merged" in delete_calls

    def test_skips_branches_already_in_git_merged(self):
        """Branches in both git-merged and github-merged only delete once."""
        calls = []

        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/both\n"
            if args[0] == "branch" and (args[1] == "-d" or args[1] == "-D"):
                calls.append((args[1], args[2]))
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=["koan/both"],
                github_merged=["koan/both"],
            )
        assert deleted == ["koan/both"]
        # Should use safe delete (-d), not force (-D)
        assert calls == [("-d", "koan/both")]

    def test_mixed_git_and_github_merges(self):
        """Both git-detected and GitHub-detected branches are cleaned up."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/git-merged\n  koan/squash-merged\n"
            if args[0] == "branch" and (args[1] == "-d" or args[1] == "-D"):
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=["koan/git-merged"],
                github_merged=["koan/squash-merged"],
            )
        assert "koan/git-merged" in deleted
        assert "koan/squash-merged" in deleted

    def test_github_merged_skips_current_branch(self):
        """Never force-delete the current branch, even if GitHub says merged."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "koan/squash-merged"  # we ARE on this branch
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/squash-merged\n"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=[],
                github_merged=["koan/squash-merged"],
            )
        assert deleted == []

    def test_github_merged_skips_remote_only(self):
        """GitHub-merged branches with no local copy are skipped."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return ""  # no local branches
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=[],
                github_merged=["koan/remote-only"],
            )
        assert deleted == []

    def test_none_github_merged(self):
        """None github_merged is treated as empty list."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/local\n"
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=["koan/local"],
                github_merged=None,
            )
        assert deleted == ["koan/local"]


class TestBuildSyncReport:
    def test_report_includes_merged_and_unmerged(self):
        with patch("app.git_sync.run_git") as mock_git:
            def side_effect(cwd, *args):
                args_str = " ".join(args)
                if "fetch" in args_str:
                    return ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return "main"  # current branch for cleanup
                if "rev-parse" in args_str:
                    return "abc123"  # branch exists
                if "--merged" in args_str:
                    return "  remotes/origin/koan/merged-one\n"
                if args[0] == "branch" and args[1] == "-d":
                    return ""  # no local branches to delete
                if "branch" in args_str and "--list" in args_str:
                    # get_koan_branches: return all branches
                    return "  remotes/origin/koan/merged-one\n  remotes/origin/koan/pending-one\n"
                if "for-each-ref" in args_str:
                    return ""  # no age info → all treated as recent
                if "log" in args_str:
                    return "abc123 some commit\n"
                return ""

            mock_git.side_effect = side_effect
            with patch.object(GitSync, "get_github_merged_branches", return_value=[]):
                report = _sync().build_sync_report()

        assert "koan/merged-one" in report
        assert "koan/pending-one" in report
        assert "abc123" in report
        assert "Git sync" in report

    def test_report_no_changes(self):
        with patch("app.git_sync.run_git", return_value=""):
            with patch.object(GitSync, "get_github_merged_branches", return_value=[]):
                report = _sync().build_sync_report()
        assert "No notable changes" in report

    def test_report_shows_cleanup(self):
        """Report includes cleanup summary when branches are deleted."""
        with patch("app.git_sync.run_git") as mock_git:
            def side_effect(cwd, *args):
                args_str = " ".join(args)
                if "fetch" in args_str:
                    return ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return "main"
                if "rev-parse" in args_str:
                    return "abc123"
                if "--merged" in args_str:
                    return "  koan/old-feature\n"
                if args[0] == "branch" and args[1] == "--list":
                    if "koan/*" in args_str or "*koan/*" in args_str:
                        return "  koan/old-feature\n"
                    # _get_local_branches for cleanup
                    return "  koan/old-feature\n"
                if args[0] == "branch" and args[1] == "-d":
                    return "Deleted branch koan/old-feature"
                if "log" in args_str:
                    return ""
                return ""

            mock_git.side_effect = side_effect
            with patch.object(GitSync, "get_github_merged_branches", return_value=[]):
                report = _sync().build_sync_report()

        assert "cleaned up" in report.lower()
        assert "koan/old-feature" in report

    def test_report_collapses_stale_branches(self):
        """Stale branches are collapsed into a summary line."""
        sync = _sync()
        with patch.object(
            GitSync, "get_merged_branches", return_value=[]
        ), patch.object(
            GitSync, "get_github_merged_branches", return_value=[],
        ), patch.object(
            GitSync, "get_unmerged_branches",
            return_value=["koan/new", "koan/old1", "koan/old2", "koan/old3"],
        ), patch.object(
            GitSync, "_split_branches_by_recency",
            return_value=(["koan/new"], ["koan/old1", "koan/old2", "koan/old3"]),
        ), patch.object(
            GitSync, "get_recent_main_commits", return_value=[],
        ), patch("app.git_sync.run_git", return_value=""):
            report = sync.build_sync_report()

        assert "koan/new" in report
        assert "koan/old1" not in report
        assert "3 older branch(es)" in report
        assert f">{RECENT_BRANCH_DAYS}d" in report

    def test_report_shows_all_when_no_stale(self):
        """When all branches are recent, no summary line is added."""
        sync = _sync()
        with patch.object(
            GitSync, "get_merged_branches", return_value=[]
        ), patch.object(
            GitSync, "get_github_merged_branches", return_value=[],
        ), patch.object(
            GitSync, "get_unmerged_branches",
            return_value=["koan/a", "koan/b"],
        ), patch.object(
            GitSync, "_split_branches_by_recency",
            return_value=(["koan/a", "koan/b"], []),
        ), patch.object(
            GitSync, "get_recent_main_commits", return_value=[],
        ), patch("app.git_sync.run_git", return_value=""):
            report = sync.build_sync_report()

        assert "koan/a" in report
        assert "koan/b" in report
        assert "older branch" not in report

    def test_report_total_count_includes_stale(self):
        """The header count includes all branches (recent + stale)."""
        sync = _sync()
        with patch.object(
            GitSync, "get_merged_branches", return_value=[]
        ), patch.object(
            GitSync, "get_github_merged_branches", return_value=[],
        ), patch.object(
            GitSync, "get_unmerged_branches",
            return_value=["koan/a", "koan/b", "koan/c"],
        ), patch.object(
            GitSync, "_split_branches_by_recency",
            return_value=(["koan/a"], ["koan/b", "koan/c"]),
        ), patch.object(
            GitSync, "get_recent_main_commits", return_value=[],
        ), patch("app.git_sync.run_git", return_value=""):
            report = sync.build_sync_report()

        assert "(3)" in report  # total count in header

    def test_report_includes_github_merged_branches(self):
        """GitHub-detected merges appear in the report."""
        sync = _sync()
        with patch.object(
            GitSync, "get_merged_branches", return_value=["koan/git-merged"],
        ), patch.object(
            GitSync, "get_github_merged_branches",
            return_value=["koan/git-merged", "koan/squash-merged"],
        ), patch.object(
            GitSync, "get_unmerged_branches",
            return_value=["koan/squash-merged", "koan/wip"],
        ), patch.object(
            GitSync, "_split_branches_by_recency",
            return_value=(["koan/wip"], []),
        ), patch.object(
            GitSync, "get_recent_main_commits", return_value=[],
        ), patch.object(
            GitSync, "cleanup_merged_branches",
            return_value=["koan/git-merged", "koan/squash-merged"],
        ), patch("app.git_sync.run_git", return_value=""):
            report = sync.build_sync_report()

        # Both git-merged and squash-merged should show as merged
        assert "koan/git-merged" in report
        assert "koan/squash-merged" in report
        # Cleaned squash-merged should not appear in unmerged
        assert "koan/wip" in report

    def test_report_removes_cleaned_from_unmerged(self):
        """Branches cleaned via GitHub detection are removed from unmerged list."""
        sync = _sync()
        with patch.object(
            GitSync, "get_merged_branches", return_value=[],
        ), patch.object(
            GitSync, "get_github_merged_branches",
            return_value=["koan/was-squash-merged"],
        ), patch.object(
            GitSync, "get_unmerged_branches",
            return_value=["koan/was-squash-merged", "koan/truly-wip"],
        ), patch.object(
            GitSync, "_split_branches_by_recency",
            return_value=(["koan/truly-wip"], []),
        ), patch.object(
            GitSync, "get_recent_main_commits", return_value=[],
        ), patch.object(
            GitSync, "cleanup_merged_branches",
            return_value=["koan/was-squash-merged"],
        ), patch("app.git_sync.run_git", return_value=""):
            report = sync.build_sync_report()

        # was-squash-merged should show in merged section, not unmerged
        lines = report.split("\n")
        unmerged_section = False
        for line in lines:
            if "Unmerged" in line:
                unmerged_section = True
            if unmerged_section and "was-squash-merged" in line:
                pytest.fail("Cleaned branch should not appear in unmerged section")


class TestRemoteBranchDeletion:
    """Tests for remote branch deletion in cleanup_merged_branches."""

    def test_deletes_remote_branches_by_default(self):
        """Successfully deleted local branches also get deleted on remote."""
        push_calls = []

        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/merged-one\n"
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            if args[0] == "push" and "--delete" in args:
                push_calls.append(args[-1])
                return "To origin\n - [deleted] koan/merged-one\n"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(["koan/merged-one"])

        assert deleted == ["koan/merged-one"]
        assert push_calls == ["koan/merged-one"]

    def test_skips_remote_deletion_when_disabled(self):
        """delete_remote=False skips git push origin --delete."""
        push_calls = []

        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/merged-one\n"
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            if args[0] == "push":
                push_calls.append(args)
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                ["koan/merged-one"], delete_remote=False
            )

        assert deleted == ["koan/merged-one"]
        assert push_calls == [], "Expected no remote push when delete_remote=False"

    def test_remote_deletion_failure_does_not_affect_local_result(self):
        """Remote push failure is tolerated — local deletion still reported."""
        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/merged-one\n"
            if args[0] == "branch" and args[1] == "-d":
                return f"Deleted branch {args[2]}"
            if args[0] == "push" and "--delete" in args:
                return ""  # empty = push failed (remote already gone)
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(["koan/merged-one"])

        assert deleted == ["koan/merged-one"]

    def test_remote_deletion_skips_non_local_deleted(self):
        """Only tries remote deletion for branches successfully deleted locally."""
        push_calls = []

        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/stuck\n"
            if args[0] == "branch" and args[1] == "-d":
                return ""  # local delete failed
            if args[0] == "push":
                push_calls.append(args)
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(["koan/stuck"])

        assert deleted == []
        assert push_calls == [], "Should not push delete if local deletion failed"

    def test_github_merged_remote_deletion(self):
        """Remote branches for github-detected merges are also deleted."""
        push_calls = []

        def side_effect(cwd, *args):
            if args[0] == "rev-parse":
                return "main"
            if args[0] == "branch" and args[1] == "--list":
                return "  koan/squash-merged\n"
            if args[0] == "branch" and args[1] == "-D":
                return f"Deleted branch {args[2]}"
            if args[0] == "push" and "--delete" in args:
                push_calls.append(args[-1])
                return "To origin\n - [deleted] koan/squash-merged\n"
            return ""

        with patch("app.git_sync.run_git", side_effect=side_effect):
            deleted = _sync().cleanup_merged_branches(
                merged=[], github_merged=["koan/squash-merged"]
            )

        assert deleted == ["koan/squash-merged"]
        assert push_calls == ["koan/squash-merged"]


class TestBranchCleanupConfig:
    """Tests for config-controlled branch cleanup in build_sync_report."""

    def test_cleanup_disabled_by_config_skips_deletion(self):
        """When branch_cleanup.enabled=False, no branches are deleted."""
        sync = _sync()
        with patch.object(GitSync, "get_merged_branches", return_value=["koan/old"]), \
             patch.object(GitSync, "get_github_merged_branches", return_value=[]), \
             patch.object(GitSync, "get_unmerged_branches", return_value=[]), \
             patch.object(GitSync, "get_recent_main_commits", return_value=[]), \
             patch.object(GitSync, "cleanup_merged_branches") as mock_cleanup, \
             patch("app.git_sync.run_git", return_value=""), \
             patch("app.git_sync.get_branch_cleanup_config",
                   return_value={"enabled": False, "delete_remote_branches": True}):
            sync.build_sync_report()

        mock_cleanup.assert_not_called()

    def test_delete_remote_false_passed_to_cleanup(self):
        """When delete_remote_branches=False, cleanup is called with delete_remote=False."""
        sync = _sync()
        with patch.object(GitSync, "get_merged_branches", return_value=["koan/old"]), \
             patch.object(GitSync, "get_github_merged_branches", return_value=[]), \
             patch.object(GitSync, "get_unmerged_branches", return_value=[]), \
             patch.object(GitSync, "get_recent_main_commits", return_value=[]), \
             patch.object(GitSync, "cleanup_merged_branches", return_value=[]) as mock_cleanup, \
             patch("app.git_sync.run_git", return_value=""), \
             patch("app.git_sync.get_branch_cleanup_config",
                   return_value={"enabled": True, "delete_remote_branches": False}):
            sync.build_sync_report()

        mock_cleanup.assert_called_once()
        _, kwargs = mock_cleanup.call_args
        assert kwargs.get("delete_remote") is False

    def test_cleanup_enabled_by_default(self):
        """When no config override, cleanup runs with delete_remote=True."""
        sync = _sync()
        with patch.object(GitSync, "get_merged_branches", return_value=["koan/old"]), \
             patch.object(GitSync, "get_github_merged_branches", return_value=[]), \
             patch.object(GitSync, "get_unmerged_branches", return_value=[]), \
             patch.object(GitSync, "get_recent_main_commits", return_value=[]), \
             patch.object(GitSync, "cleanup_merged_branches", return_value=[]) as mock_cleanup, \
             patch("app.git_sync.run_git", return_value=""), \
             patch("app.git_sync.get_branch_cleanup_config",
                   return_value={"enabled": True, "delete_remote_branches": True}):
            sync.build_sync_report()

        mock_cleanup.assert_called_once()
        _, kwargs = mock_cleanup.call_args
        assert kwargs.get("delete_remote") is True


class TestWriteSyncToJournal:
    def test_creates_journal_entry(self, tmp_path):
        """Writes sync report to journal file."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "journal").mkdir()

        sync = GitSync(str(instance), "koan", "/fake")
        sync.write_sync_to_journal("Test sync report")

        today = date.today().strftime("%Y-%m-%d")
        journal_file = instance / "journal" / today / "koan.md"
        assert journal_file.exists()
        content = journal_file.read_text()
        assert "Git Sync" in content
        assert "Test sync report" in content

    def test_appends_to_existing(self, tmp_path):
        """Appends to existing journal file, doesn't overwrite."""
        instance = tmp_path / "instance"
        instance.mkdir()
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = instance / "journal" / today
        journal_dir.mkdir(parents=True)
        journal_file = journal_dir / "koan.md"
        journal_file.write_text("## Previous Entry\n\nSome work.\n")

        sync = GitSync(str(instance), "koan", "/fake")
        sync.write_sync_to_journal("New sync")

        content = journal_file.read_text()
        assert "Previous Entry" in content
        assert "New sync" in content


class TestGitSyncCLI:
    """Tests for git_sync.py __main__ block."""

    def test_cli_usage_error(self):
        """Exit 1 with usage message when called with too few args."""
        from tests._helpers import run_module
        import sys
        with patch.object(sys, "argv", ["git_sync.py"]):
            with pytest.raises(SystemExit) as exc:
                run_module("app.git_sync", run_name="__main__")
            assert exc.value.code == 1

    def test_cli_runs_sync(self, tmp_path):
        """Full CLI run: builds report, writes to journal, prints output."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "journal").mkdir()

        from tests._helpers import run_module
        import sys
        with patch.object(sys, "argv", [
            "git_sync.py", str(instance), "koan", "/fake/path"
        ]):
            with patch("app.git_sync.run_git", return_value=""):
                with patch("builtins.print") as mock_print:
                    run_module("app.git_sync", run_name="__main__")
                    mock_print.assert_called_once()

    def test_cli_with_branches(self, tmp_path):
        """CLI prints the sync report to stdout."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "journal").mkdir()

        def subprocess_side_effect(cmd, **kwargs):
            args_str = " ".join(cmd)
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stderr = ""
            if "fetch" in args_str:
                mock_result.stdout = ""
            elif "--merged" in args_str:
                mock_result.stdout = "  remotes/origin/koan/done\n"
            elif "--no-merged" in args_str:
                mock_result.stdout = ""
            elif "log" in args_str:
                mock_result.stdout = "abc1234 fix something\n"
            else:
                mock_result.stdout = ""
            return mock_result

        from tests._helpers import run_module
        import sys
        with patch.object(sys, "argv", [
            "git_sync.py", str(instance), "koan", "/fake/path"
        ]):
            with patch("subprocess.run", side_effect=subprocess_side_effect):
                with patch("builtins.print") as mock_print:
                    run_module("app.git_sync", run_name="__main__")
                    output = mock_print.call_args[0][0]
                    assert "koan/done" in output
