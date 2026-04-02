"""Tests for koan/app/branch_limiter.py — branch saturation limiter."""

import pytest
from unittest.mock import patch, MagicMock

from app.branch_limiter import (
    count_pending_branches,
    is_project_branch_saturated,
)


class TestCountPendingBranches:
    """Tests for count_pending_branches() — union of local + PR branches."""

    @patch("app.branch_limiter._get_open_pr_branches")
    @patch("app.branch_limiter._get_local_unmerged_branches")
    def test_union_deduplicates(self, mock_local, mock_pr):
        """Branch with both local copy and open PR counted once."""
        mock_local.return_value = {"koan/fix-a", "koan/fix-b"}
        mock_pr.return_value = {"koan/fix-b", "koan/fix-c"}

        count = count_pending_branches(
            "/instance", "myapp", "/code/myapp", ["owner/myapp"], "bot",
        )
        assert count == 3  # fix-a, fix-b, fix-c

    @patch("app.branch_limiter._get_open_pr_branches")
    @patch("app.branch_limiter._get_local_unmerged_branches")
    def test_local_only(self, mock_local, mock_pr):
        """No GitHub URLs — count only local branches."""
        mock_local.return_value = {"koan/fix-a", "koan/fix-b"}
        mock_pr.return_value = set()

        count = count_pending_branches(
            "/instance", "myapp", "/code/myapp", [], "bot",
        )
        assert count == 2

    @patch("app.branch_limiter._get_open_pr_branches")
    @patch("app.branch_limiter._get_local_unmerged_branches")
    def test_pr_only(self, mock_local, mock_pr):
        """No local branches — count only PR branches."""
        mock_local.return_value = set()
        mock_pr.return_value = {"koan/fix-a"}

        count = count_pending_branches(
            "/instance", "myapp", "/code/myapp", ["owner/myapp"], "bot",
        )
        assert count == 1

    @patch("app.branch_limiter._get_open_pr_branches")
    @patch("app.branch_limiter._get_local_unmerged_branches")
    def test_empty_both(self, mock_local, mock_pr):
        """No branches at all."""
        mock_local.return_value = set()
        mock_pr.return_value = set()

        count = count_pending_branches(
            "/instance", "myapp", "/code/myapp", ["owner/myapp"], "bot",
        )
        assert count == 0

    @patch("app.branch_limiter._get_open_pr_branches")
    @patch("app.branch_limiter._get_local_unmerged_branches")
    def test_github_error_falls_back_to_local(self, mock_local, mock_pr):
        """GitHub API error → local-only count."""
        mock_local.return_value = {"koan/fix-a", "koan/fix-b"}
        mock_pr.return_value = set()  # Empty on error (handled internally)

        count = count_pending_branches(
            "/instance", "myapp", "/code/myapp", ["owner/myapp"], "bot",
        )
        assert count == 2


class TestIsProjectBranchSaturated:
    """Tests for is_project_branch_saturated()."""

    @patch("app.branch_limiter.count_pending_branches", return_value=10)
    def test_saturated_at_limit(self, mock_count):
        config = {
            "defaults": {"max_pending_branches": 10},
            "projects": {"myapp": {"path": "/code/myapp"}},
        }
        assert is_project_branch_saturated(
            config, "myapp", "/instance", "/code/myapp", ["owner/myapp"], "bot",
        ) is True

    @patch("app.branch_limiter.count_pending_branches", return_value=11)
    def test_saturated_over_limit(self, mock_count):
        config = {
            "projects": {"myapp": {"path": "/code/myapp", "max_pending_branches": 5}},
        }
        assert is_project_branch_saturated(
            config, "myapp", "/instance", "/code/myapp", ["owner/myapp"], "bot",
        ) is True

    @patch("app.branch_limiter.count_pending_branches", return_value=4)
    def test_not_saturated_under_limit(self, mock_count):
        config = {
            "projects": {"myapp": {"path": "/code/myapp", "max_pending_branches": 5}},
        }
        assert is_project_branch_saturated(
            config, "myapp", "/instance", "/code/myapp", ["owner/myapp"], "bot",
        ) is False

    def test_unlimited_returns_false(self):
        """max_pending_branches: 0 means unlimited — never saturated."""
        config = {
            "projects": {"myapp": {"path": "/code/myapp", "max_pending_branches": 0}},
        }
        assert is_project_branch_saturated(
            config, "myapp", "/instance", "/code/myapp", ["owner/myapp"], "bot",
        ) is False
