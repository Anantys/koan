"""Tests for the /branches skill handler."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills.core.branches.handler import (
    handle,
    _parse_shortstat,
    _merge_score,
    _recommend_merge_order,
    _format_output,
    _enrich_and_merge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def instance_dir(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    return inst


@pytest.fixture
def koan_root(tmp_path):
    return tmp_path


def _make_ctx(koan_root, instance_dir, args=""):
    return SimpleNamespace(
        koan_root=koan_root,
        instance_dir=instance_dir,
        command_name="branches",
        args=args,
        send_message=None,
        handle_chat=None,
    )


# ---------------------------------------------------------------------------
# _parse_shortstat
# ---------------------------------------------------------------------------

class TestParseShortstat:
    def test_full_stat(self):
        assert _parse_shortstat("3 files changed, 42 insertions(+), 10 deletions(-)") == (3, 42, 10)

    def test_insertions_only(self):
        assert _parse_shortstat("1 file changed, 5 insertions(+)") == (1, 5, 0)

    def test_deletions_only(self):
        assert _parse_shortstat("2 files changed, 8 deletions(-)") == (2, 0, 8)

    def test_empty(self):
        assert _parse_shortstat("") == (0, 0, 0)


# ---------------------------------------------------------------------------
# _merge_score
# ---------------------------------------------------------------------------

class TestMergeScore:
    def test_approved_pr_scores_lowest(self):
        approved = {"pr_review_decision": "APPROVED", "pr_has_reviews": True,
                     "has_pr": True, "pr_additions": 10, "pr_deletions": 5,
                     "conflicts": False, "timestamp": 100}
        not_reviewed = {"pr_review_decision": "", "pr_has_reviews": False,
                         "has_pr": True, "pr_additions": 10, "pr_deletions": 5,
                         "conflicts": False, "timestamp": 100}
        assert _merge_score(approved) < _merge_score(not_reviewed)

    def test_no_conflicts_before_conflicts(self):
        clean = {"pr_review_decision": "", "pr_has_reviews": False,
                  "has_pr": True, "pr_additions": 10, "pr_deletions": 5,
                  "conflicts": False, "timestamp": 100}
        dirty = {"pr_review_decision": "", "pr_has_reviews": False,
                  "has_pr": True, "pr_additions": 10, "pr_deletions": 5,
                  "conflicts": True, "timestamp": 100}
        assert _merge_score(clean) < _merge_score(dirty)

    def test_smaller_changes_first(self):
        small = {"pr_review_decision": "", "pr_has_reviews": False,
                  "has_pr": True, "pr_additions": 5, "pr_deletions": 2,
                  "conflicts": False, "timestamp": 100}
        large = {"pr_review_decision": "", "pr_has_reviews": False,
                  "has_pr": True, "pr_additions": 500, "pr_deletions": 200,
                  "conflicts": False, "timestamp": 100}
        assert _merge_score(small) < _merge_score(large)

    def test_older_first_when_equal_size(self):
        old = {"pr_review_decision": "", "pr_has_reviews": False,
                "has_pr": True, "pr_additions": 10, "pr_deletions": 5,
                "conflicts": False, "timestamp": 100}
        new = {"pr_review_decision": "", "pr_has_reviews": False,
                "has_pr": True, "pr_additions": 10, "pr_deletions": 5,
                "conflicts": False, "timestamp": 9999}
        assert _merge_score(old) < _merge_score(new)


# ---------------------------------------------------------------------------
# _recommend_merge_order
# ---------------------------------------------------------------------------

class TestRecommendMergeOrder:
    def test_sorts_by_score(self):
        entries = [
            {"branch": "koan/big", "has_pr": True, "pr_additions": 500,
             "pr_deletions": 200, "conflicts": False, "timestamp": 100,
             "pr_review_decision": "", "pr_has_reviews": False},
            {"branch": "koan/approved", "has_pr": True, "pr_additions": 100,
             "pr_deletions": 50, "conflicts": False, "timestamp": 200,
             "pr_review_decision": "APPROVED", "pr_has_reviews": True},
            {"branch": "koan/small", "has_pr": True, "pr_additions": 5,
             "pr_deletions": 2, "conflicts": False, "timestamp": 50,
             "pr_review_decision": "", "pr_has_reviews": False},
        ]
        ordered = _recommend_merge_order(entries)
        assert ordered[0]["branch"] == "koan/approved"
        assert ordered[1]["branch"] == "koan/small"
        assert ordered[2]["branch"] == "koan/big"


# ---------------------------------------------------------------------------
# _enrich_and_merge
# ---------------------------------------------------------------------------

class TestEnrichAndMerge:
    def test_branch_with_pr(self):
        branches = [{"branch": "koan/foo", "has_pr": False, "commits": 3,
                      "age": "2 days ago", "timestamp": 100,
                      "diffstat": (2, 10, 5), "conflicts": False}]
        prs = [{"branch": "koan/foo", "number": 42, "title": "Fix foo",
                "additions": 10, "deletions": 5, "created_at": "",
                "is_draft": False, "review_decision": "APPROVED",
                "has_reviews": True, "labels": [], "url": "https://github.com/org/repo/pull/42"}]

        result = _enrich_and_merge(branches, prs)
        assert len(result) == 1
        assert result[0]["has_pr"] is True
        assert result[0]["pr_number"] == 42
        assert result[0]["pr_review_decision"] == "APPROVED"

    def test_branch_without_pr(self):
        branches = [{"branch": "koan/bar", "has_pr": False, "commits": 1,
                      "age": "1 day ago", "timestamp": 200,
                      "diffstat": (1, 3, 0), "conflicts": False}]
        result = _enrich_and_merge(branches, [])
        assert len(result) == 1
        assert result[0]["has_pr"] is False

    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_remote_pr_without_local_branch(self, mock_prefix):
        prs = [{"branch": "koan/remote-only", "number": 99, "title": "Remote PR",
                "additions": 50, "deletions": 10, "created_at": "",
                "is_draft": True, "review_decision": "",
                "has_reviews": False, "labels": [], "url": "https://github.com/org/repo/pull/99"}]
        result = _enrich_and_merge([], prs)
        assert len(result) == 1
        assert result[0]["has_pr"] is True
        assert result[0]["pr_is_draft"] is True


# ---------------------------------------------------------------------------
# _format_output
# ---------------------------------------------------------------------------

class TestFormatOutput:
    def test_empty_entries(self):
        assert "No koan branches" in _format_output("koan", [])

    def test_basic_formatting(self):
        entries = [
            {"branch": "koan/fix-bug", "has_pr": True, "pr_number": 42,
             "pr_title": "Fix the bug", "pr_additions": 10, "pr_deletions": 3,
             "pr_is_draft": False, "pr_review_decision": "APPROVED",
             "pr_has_reviews": True, "pr_labels": [],
             "pr_url": "https://github.com/org/repo/pull/42",
             "age": "2 days ago", "timestamp": 100, "commits": 2,
             "diffstat": (2, 10, 3), "conflicts": False},
        ]
        output = _format_output("koan", entries)
        assert "fix-bug" in output
        assert "PR #42" in output
        assert "+10/-3" in output
        assert "approved" in output
        assert "1 approved" in output
        assert "https://github.com/org/repo/pull/42" in output

    def test_conflicts_shown(self):
        entries = [
            {"branch": "koan/conflict-branch", "has_pr": False,
             "age": "1 day ago", "timestamp": 200, "commits": 1,
             "diffstat": (1, 5, 0), "conflicts": True},
        ]
        output = _format_output("koan", entries)
        assert "conflicts" in output.lower()

    def test_no_pr_shown(self):
        entries = [
            {"branch": "koan/no-pr", "has_pr": False,
             "age": "3 days ago", "timestamp": 50, "commits": 5,
             "diffstat": (3, 20, 10), "conflicts": False},
        ]
        output = _format_output("koan", entries)
        assert "no PR" in output
        assert "https://" not in output

    def test_pr_url_displayed(self):
        entries = [
            {"branch": "koan/with-url", "has_pr": True, "pr_number": 77,
             "pr_title": "Add feature", "pr_additions": 20, "pr_deletions": 5,
             "pr_is_draft": False, "pr_review_decision": "",
             "pr_has_reviews": False, "pr_labels": [],
             "pr_url": "https://github.com/org/repo/pull/77",
             "age": "1 day ago", "timestamp": 300, "commits": 3,
             "diffstat": (2, 20, 5), "conflicts": False},
        ]
        output = _format_output("koan", entries)
        assert "https://github.com/org/repo/pull/77" in output


# ---------------------------------------------------------------------------
# _get_branches_info (merged branch filtering)
# ---------------------------------------------------------------------------

class TestGetBranchesInfoFiltering:
    """Branches fully merged into origin/main (0 commits ahead) are excluded."""

    def test_merged_branches_excluded(self):
        """Branches with 0 commits ahead of origin/main should not appear."""
        from skills.core.branches.handler import _get_branches_info

        call_count = {"rev-list": 0}

        def fake_run_git(*args, cwd=None, timeout=None):
            cmd = args[0] if args else ""
            if cmd == "branch":
                return 0, "  koan/merged-branch\n  koan/active-branch\n", ""
            if cmd == "for-each-ref":
                return 0, "", ""
            if cmd == "rev-list":
                branch = args[-1] if len(args) > 2 else ""
                call_count["rev-list"] += 1
                if "merged-branch" in branch:
                    return 0, "0", ""  # merged: 0 commits ahead
                return 0, "3", ""  # active: 3 commits ahead
            if cmd == "log":
                if "%cr" in args:
                    return 0, "2 days ago", ""
                if "%ct" in args:
                    return 0, "1000000", ""
            if cmd == "diff":
                return 0, "1 file changed, 5 insertions(+)", ""
            return 0, "", ""

        with patch("app.git_utils.run_git", side_effect=fake_run_git), \
             patch("app.config.get_branch_prefix", return_value="koan/"), \
             patch("skills.core.branches.handler._check_conflicts", return_value=False):
            result = _get_branches_info("/fake/path")

        branch_names = [b["branch"] for b in result]
        assert "koan/active-branch" in branch_names
        assert "koan/merged-branch" not in branch_names
        assert len(result) == 1


# ---------------------------------------------------------------------------
# handle (integration with mocks)
# ---------------------------------------------------------------------------

class TestHandle:
    def test_no_projects(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.utils.get_known_projects", return_value={}):
            result = handle(ctx)
        assert "No project" in result

    def test_no_args_multiple_projects_prompts(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        projects = {"alpha": "/tmp/alpha", "beta": "/tmp/beta"}
        with patch("app.utils.get_known_projects", return_value=projects):
            result = handle(ctx)
        assert "Which project?" in result
        assert "alpha" in result
        assert "beta" in result

    def test_no_args_single_project_auto_selects(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.utils.get_known_projects",
                    return_value={"solo": "/tmp/solo"}), \
             patch("skills.core.branches.handler._get_branches_info", return_value=[]), \
             patch("skills.core.branches.handler._get_open_prs", return_value=[]):
            result = handle(ctx)
        assert "No koan branches" in result
        assert "solo" in result

    def test_no_branches_no_prs(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="myproject")
        with patch("app.utils.get_known_projects",
                    return_value={"myproject": "/tmp/myproject"}), \
             patch("skills.core.branches.handler._get_branches_info", return_value=[]), \
             patch("skills.core.branches.handler._get_open_prs", return_value=[]):
            result = handle(ctx)
        assert "No koan branches" in result
        assert "myproject" in result

    def test_with_data(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="koan")
        branches = [
            {"branch": "koan/a", "has_pr": False, "commits": 1,
             "age": "1 day ago", "timestamp": 100,
             "diffstat": (1, 5, 2), "conflicts": False},
        ]
        prs = [
            {"branch": "koan/a", "number": 10, "title": "Feature A",
             "additions": 5, "deletions": 2, "created_at": "",
             "is_draft": False, "review_decision": "",
             "has_reviews": False, "labels": [],
             "url": "https://github.com/org/repo/pull/10"},
        ]
        with patch("app.utils.get_known_projects",
                    return_value={"koan": "/tmp/koan"}), \
             patch("skills.core.branches.handler._get_branches_info",
                    return_value=branches), \
             patch("skills.core.branches.handler._get_open_prs",
                    return_value=prs):
            result = handle(ctx)
        assert "Feature A" in result
        assert "PR #10" in result
        assert "https://github.com/org/repo/pull/10" in result
