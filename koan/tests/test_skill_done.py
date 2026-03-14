"""Tests for the /done skill handler."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Ensure the koan package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills.core.done.handler import (
    handle,
    _parse_args,
    _fetch_merged_prs,
    _fetch_open_prs,
    _format_output,
    _truncate_title,
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
        command_name="done",
        args=args,
        send_message=None,
        handle_chat=None,
    )


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_empty_args(self):
        project, hours = _parse_args("")
        assert project == ""
        assert hours == 24

    def test_project_only(self):
        project, hours = _parse_args("koan")
        assert project == "koan"
        assert hours == 24

    def test_hours_only(self):
        project, hours = _parse_args("--hours=48")
        assert project == ""
        assert hours == 48

    def test_project_and_hours(self):
        project, hours = _parse_args("myproject --hours=12")
        assert project == "myproject"
        assert hours == 12

    def test_hours_capped_at_168(self):
        _, hours = _parse_args("--hours=999")
        assert hours == 168

    def test_hours_minimum_1(self):
        _, hours = _parse_args("--hours=0")
        assert hours == 1


# ---------------------------------------------------------------------------
# _fetch_merged_prs
# ---------------------------------------------------------------------------

class TestFetchMergedPrs:
    def test_returns_prs_within_window(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        merged_at = datetime.now(timezone.utc) - timedelta(hours=2)
        merged_at_str = merged_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        pr_data = [
            {
                "number": 42,
                "title": "feat: add thing",
                "url": "https://github.com/org/repo/pull/42",
                "mergedAt": merged_at_str,
            }
        ]

        with patch("app.github.run_gh", return_value=json.dumps(pr_data)):
            result = _fetch_merged_prs("org/repo", "testuser", since)

        assert len(result) == 1
        assert result[0]["number"] == 42
        assert result[0]["title"] == "feat: add thing"

    def test_filters_old_prs(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        old_merged = datetime.now(timezone.utc) - timedelta(hours=48)
        old_str = old_merged.strftime("%Y-%m-%dT%H:%M:%SZ")

        pr_data = [
            {
                "number": 10,
                "title": "old PR",
                "url": "https://github.com/org/repo/pull/10",
                "mergedAt": old_str,
            }
        ]

        with patch("app.github.run_gh", return_value=json.dumps(pr_data)):
            result = _fetch_merged_prs("org/repo", "testuser", since)

        assert len(result) == 0

    def test_handles_runtime_error(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        with patch("app.github.run_gh", side_effect=RuntimeError("gh failed")):
            result = _fetch_merged_prs("org/repo", "testuser", since)

        assert result == []

    def test_handles_empty_output(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        with patch("app.github.run_gh", return_value=""):
            result = _fetch_merged_prs("org/repo", "testuser", since)

        assert result == []


# ---------------------------------------------------------------------------
# _fetch_open_prs
# ---------------------------------------------------------------------------

class TestFetchOpenPrs:
    def test_returns_prs_within_window(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        created_at_str = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        pr_data = [
            {
                "number": 55,
                "title": "feat: new feature",
                "url": "https://github.com/org/repo/pull/55",
                "createdAt": created_at_str,
            }
        ]

        with patch("app.github.run_gh", return_value=json.dumps(pr_data)):
            result = _fetch_open_prs("org/repo", "testuser", since)

        assert len(result) == 1
        assert result[0]["number"] == 55
        assert result[0]["title"] == "feat: new feature"

    def test_filters_old_prs(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        old_created = datetime.now(timezone.utc) - timedelta(hours=48)
        old_str = old_created.strftime("%Y-%m-%dT%H:%M:%SZ")

        pr_data = [
            {
                "number": 10,
                "title": "old open PR",
                "url": "https://github.com/org/repo/pull/10",
                "createdAt": old_str,
            }
        ]

        with patch("app.github.run_gh", return_value=json.dumps(pr_data)):
            result = _fetch_open_prs("org/repo", "testuser", since)

        assert len(result) == 0

    def test_handles_runtime_error(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        with patch("app.github.run_gh", side_effect=RuntimeError("gh failed")):
            result = _fetch_open_prs("org/repo", "testuser", since)

        assert result == []

    def test_handles_empty_output(self):
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        with patch("app.github.run_gh", return_value=""):
            result = _fetch_open_prs("org/repo", "testuser", since)

        assert result == []


# ---------------------------------------------------------------------------
# _truncate_title
# ---------------------------------------------------------------------------

class TestTruncateTitle:
    def test_short_title_unchanged(self):
        assert _truncate_title("short") == "short"

    def test_long_title_truncated(self):
        long_title = "x" * 80
        result = _truncate_title(long_title)
        assert len(result) == 70
        assert result.endswith("...")

    def test_exactly_70_chars_unchanged(self):
        title = "x" * 70
        assert _truncate_title(title) == title


# ---------------------------------------------------------------------------
# _format_output
# ---------------------------------------------------------------------------

class TestFormatOutput:
    def test_merged_only_single_project(self):
        by_project = {
            "koan": {
                "merged": [
                    {"number": 1, "title": "feat: X", "url": "...", "merged_at": ""},
                    {"number": 2, "title": "fix: Y", "url": "...", "merged_at": ""},
                ],
                "open": [],
            },
        }
        output = _format_output(by_project, 24)
        assert "Work (last 24h): 2 merged" in output
        assert "koan:" in output
        assert "✅ #1 feat: X" in output
        assert "✅ #2 fix: Y" in output

    def test_open_only_single_project(self):
        by_project = {
            "koan": {
                "merged": [],
                "open": [
                    {"number": 3, "title": "feat: Z", "url": "...", "created_at": ""},
                ],
            },
        }
        output = _format_output(by_project, 24)
        assert "Work (last 24h): 1 open" in output
        assert "⏳ #3 feat: Z" in output

    def test_mixed_merged_and_open(self):
        by_project = {
            "koan": {
                "merged": [
                    {"number": 1, "title": "feat: A", "url": "...", "merged_at": ""},
                ],
                "open": [
                    {"number": 2, "title": "feat: B", "url": "...", "created_at": ""},
                ],
            },
        }
        output = _format_output(by_project, 24)
        assert "Work (last 24h): 1 merged, 1 open" in output
        assert "✅ #1" in output
        assert "⏳ #2" in output

    def test_multi_project_grouping(self):
        by_project = {
            "alpha": {
                "merged": [
                    {"number": 1, "title": "A", "url": "...", "merged_at": ""},
                ],
                "open": [],
            },
            "beta": {
                "merged": [],
                "open": [
                    {"number": 2, "title": "B", "url": "...", "created_at": ""},
                ],
            },
        }
        output = _format_output(by_project, 24)
        assert "alpha:" in output
        assert "beta:" in output
        assert "1 merged, 1 open" in output

    def test_always_shows_project_header(self):
        """Even with a single project, the header is shown."""
        by_project = {
            "solo": {
                "merged": [
                    {"number": 1, "title": "X", "url": "...", "merged_at": ""},
                ],
                "open": [],
            },
        }
        output = _format_output(by_project, 24)
        assert "solo:" in output

    def test_custom_hours(self):
        by_project = {
            "p": {
                "merged": [
                    {"number": 1, "title": "A", "url": "...", "merged_at": ""},
                ],
                "open": [],
            },
        }
        output = _format_output(by_project, 48)
        assert "last 48h" in output


# ---------------------------------------------------------------------------
# handle (integration)
# ---------------------------------------------------------------------------

class TestHandle:
    def test_no_github_user(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.github.get_gh_username", return_value=""), \
             patch("app.utils.get_known_projects", return_value=[("p", "/p")]):
            result = handle(ctx)
        assert "Cannot determine GitHub username" in result

    def test_no_projects(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[]):
            result = handle(ctx)
        assert "No projects configured" in result

    def test_project_not_found(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="nonexistent")
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/koan")]):
            result = handle(ctx)
        assert "not found" in result

    def test_no_activity(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/koan")]), \
             patch("app.utils.get_github_remote", return_value="org/koan"), \
             patch("app.github.run_gh", return_value="[]"):
            result = handle(ctx)
        assert "No activity" in result

    def test_returns_merged_prs(self, koan_root, instance_dir):
        merged_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr_data = json.dumps([{
            "number": 99,
            "title": "feat: awesome",
            "url": "https://github.com/org/koan/pull/99",
            "mergedAt": merged_at,
        }])

        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/koan")]), \
             patch("app.utils.get_github_remote", return_value="org/koan"), \
             patch("app.github.run_gh", return_value=pr_data):
            result = handle(ctx)

        assert "✅ #99" in result
        assert "feat: awesome" in result
        assert "Work (last 24h)" in result

    def test_returns_open_prs(self, koan_root, instance_dir):
        created_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # run_gh is called twice per project: once for merged (returns []),
        # once for open (returns PR data)
        open_data = json.dumps([{
            "number": 77,
            "title": "feat: pending work",
            "url": "https://github.com/org/koan/pull/77",
            "createdAt": created_at,
        }])

        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/koan")]), \
             patch("app.utils.get_github_remote", return_value="org/koan"), \
             patch("app.github.run_gh", side_effect=["[]", open_data]):
            result = handle(ctx)

        assert "⏳ #77" in result
        assert "feat: pending work" in result
        assert "1 open" in result

    def test_filters_by_project(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="koan")
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/k"), ("other", "/o")]), \
             patch("app.utils.get_github_remote", return_value="org/koan"), \
             patch("app.github.run_gh", return_value="[]"):
            result = handle(ctx)

        assert "No activity" in result

    def test_no_repo_slug_skips_project(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("app.github.get_gh_username", return_value="user"), \
             patch("app.utils.get_known_projects", return_value=[("local", "/local")]), \
             patch("app.utils.get_github_remote", return_value=None):
            result = handle(ctx)
        assert "No activity" in result
