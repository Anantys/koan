"""Tests for koan/app/pr_tracker.py and dashboard PR routes."""

import json
import shutil
import subprocess

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jinja2 import FileSystemLoader

from app import pr_tracker, dashboard

REAL_TEMPLATES = Path(__file__).parent.parent / "templates"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear PR cache before each test."""
    pr_tracker._pr_cache.clear()
    yield
    pr_tracker._pr_cache.clear()


SAMPLE_PR = {
    "number": 42,
    "title": "Add feature X",
    "author": {"login": "koan-bot"},
    "headRefName": "koan/feature-x",
    "isDraft": False,
    "url": "https://github.com/owner/repo/pull/42",
    "createdAt": "2026-03-13T10:00:00Z",
    "reviewDecision": "APPROVED",
    "statusCheckRollup": [
        {"name": "ci", "state": "SUCCESS", "conclusion": "SUCCESS"},
    ],
    "state": "OPEN",
}

SAMPLE_CONFIG = {
    "projects": {
        "myproject": {
            "path": "/tmp/myproject",
            "github_url": "owner/repo",
        },
        "other": {
            "path": "/tmp/other",
            "github_url": "owner/other",
        },
    },
}


# ---------------------------------------------------------------------------
# Unit tests: fetch_project_prs
# ---------------------------------------------------------------------------

class TestFetchProjectPrs:
    @patch("app.pr_tracker.run_gh")
    def test_returns_prs(self, mock_gh):
        mock_gh.return_value = json.dumps([SAMPLE_PR])
        result = pr_tracker.fetch_project_prs("myproject", "/tmp/myproject", "owner/repo")
        assert len(result) == 1
        assert result[0]["number"] == 42
        assert result[0]["project"] == "myproject"

    @patch("app.pr_tracker.run_gh")
    def test_with_author_filter(self, mock_gh):
        mock_gh.return_value = json.dumps([])
        pr_tracker.fetch_project_prs("p", "/tmp/p", "o/r", author_filter="bot")
        args = mock_gh.call_args[0]
        assert "--author" in args
        assert "bot" in args

    @patch("app.pr_tracker.run_gh")
    def test_error_returns_empty(self, mock_gh):
        mock_gh.side_effect = RuntimeError("gh failed")
        result = pr_tracker.fetch_project_prs("p", "/tmp/p", "o/r")
        assert result == []

    @patch("app.pr_tracker.run_gh")
    def test_timeout_returns_empty(self, mock_gh):
        mock_gh.side_effect = subprocess.TimeoutExpired("gh", 20)
        result = pr_tracker.fetch_project_prs("p", "/tmp/p", "o/r")
        assert result == []

    @patch("app.pr_tracker.run_gh")
    def test_invalid_json_returns_empty(self, mock_gh):
        mock_gh.return_value = "not json"
        result = pr_tracker.fetch_project_prs("p", "/tmp/p", "o/r")
        assert result == []


# ---------------------------------------------------------------------------
# Unit tests: fetch_all_prs
# ---------------------------------------------------------------------------

class TestFetchAllPrs:
    @patch("app.pr_tracker.get_gh_username", return_value="koan-bot")
    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_aggregates_projects(self, mock_config, mock_gh, mock_user):
        mock_config.return_value = SAMPLE_CONFIG
        pr1 = {**SAMPLE_PR, "number": 1, "createdAt": "2026-03-13T10:00:00Z"}
        pr2 = {**SAMPLE_PR, "number": 2, "createdAt": "2026-03-13T12:00:00Z"}
        mock_gh.side_effect = [json.dumps([pr1]), json.dumps([pr2])]

        result = pr_tracker.fetch_all_prs("/tmp/koan")
        assert result["error"] is None
        assert len(result["prs"]) == 2
        # Newest first
        assert result["prs"][0]["number"] == 2

    @patch("app.pr_tracker.load_projects_config", return_value=None)
    def test_no_config(self, mock_config):
        result = pr_tracker.fetch_all_prs("/tmp/koan")
        assert result["error"] == "No projects configured"
        assert result["prs"] == []

    @patch("app.pr_tracker.get_gh_username", return_value="koan-bot")
    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_project_filter(self, mock_config, mock_gh, mock_user):
        mock_config.return_value = SAMPLE_CONFIG
        mock_gh.return_value = json.dumps([SAMPLE_PR])

        result = pr_tracker.fetch_all_prs("/tmp/koan", project_filter="myproject")
        assert len(result["prs"]) == 1
        # Only one gh call (filtered to myproject)
        assert mock_gh.call_count == 1

    @patch("app.pr_tracker.get_gh_username", return_value="koan-bot")
    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_caching(self, mock_config, mock_gh, mock_user):
        mock_config.return_value = SAMPLE_CONFIG
        mock_gh.return_value = json.dumps([SAMPLE_PR])

        # First call fetches
        pr_tracker.fetch_all_prs("/tmp/koan", project_filter="myproject")
        assert mock_gh.call_count == 1

        # Second call uses cache
        pr_tracker.fetch_all_prs("/tmp/koan", project_filter="myproject")
        assert mock_gh.call_count == 1

    @patch("app.pr_tracker.get_gh_username", return_value="koan-bot")
    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_author_only_false(self, mock_config, mock_gh, mock_user):
        mock_config.return_value = SAMPLE_CONFIG
        mock_gh.return_value = json.dumps([])

        pr_tracker.fetch_all_prs("/tmp/koan", project_filter="myproject",
                                  author_only=False)
        args = mock_gh.call_args[0]
        assert "--author" not in args


# ---------------------------------------------------------------------------
# Unit tests: fetch_pr_checks
# ---------------------------------------------------------------------------

class TestFetchPrChecks:
    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_returns_checks(self, mock_config, mock_gh):
        mock_config.return_value = SAMPLE_CONFIG
        checks = [{"name": "ci", "state": "completed", "conclusion": "success"}]
        mock_gh.return_value = json.dumps(checks)

        result = pr_tracker.fetch_pr_checks("myproject", 42, "/tmp/koan")
        assert len(result) == 1
        assert result[0]["name"] == "ci"

    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_error_returns_empty(self, mock_config, mock_gh):
        mock_config.return_value = SAMPLE_CONFIG
        mock_gh.side_effect = RuntimeError("gh failed")
        result = pr_tracker.fetch_pr_checks("myproject", 42, "/tmp/koan")
        assert result == []

    @patch("app.pr_tracker.load_projects_config", return_value=None)
    def test_no_config(self, mock_config):
        result = pr_tracker.fetch_pr_checks("myproject", 42, "/tmp/koan")
        assert result == []


# ---------------------------------------------------------------------------
# Unit tests: merge_pr
# ---------------------------------------------------------------------------

class TestMergePr:
    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_merge_success(self, mock_config, mock_gh):
        config = {
            **SAMPLE_CONFIG,
            "defaults": {"git_auto_merge": {"enabled": True, "strategy": "squash"}},
        }
        mock_config.return_value = config
        mock_gh.return_value = "https://github.com/owner/repo/pull/42"

        result = pr_tracker.merge_pr("myproject", 42, "/tmp/koan")
        assert result["ok"] is True
        args = mock_gh.call_args[0]
        assert "--squash" in args

    @patch("app.pr_tracker.load_projects_config")
    def test_merge_disabled(self, mock_config):
        mock_config.return_value = SAMPLE_CONFIG  # no auto-merge config
        result = pr_tracker.merge_pr("myproject", 42, "/tmp/koan")
        assert result["ok"] is False
        assert "disabled" in result["error"]

    @patch("app.pr_tracker.run_gh")
    @patch("app.pr_tracker.load_projects_config")
    def test_merge_error(self, mock_config, mock_gh):
        config = {
            **SAMPLE_CONFIG,
            "defaults": {"git_auto_merge": {"enabled": True}},
        }
        mock_config.return_value = config
        mock_gh.side_effect = RuntimeError("merge conflict")

        result = pr_tracker.merge_pr("myproject", 42, "/tmp/koan")
        assert result["ok"] is False
        assert "merge conflict" in result["error"]


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------

class TestCacheInvalidation:
    def test_invalidate_specific_project(self):
        pr_tracker._pr_cache["proj1"] = ([], 0)
        pr_tracker._pr_cache["proj2"] = ([], 0)
        pr_tracker._invalidate_cache("proj1")
        assert "proj1" not in pr_tracker._pr_cache
        assert "proj2" in pr_tracker._pr_cache

    def test_invalidate_all(self):
        pr_tracker._pr_cache["proj1"] = ([], 0)
        pr_tracker._pr_cache["proj2"] = ([], 0)
        pr_tracker._invalidate_cache()
        assert len(pr_tracker._pr_cache) == 0


# ---------------------------------------------------------------------------
# Dashboard route tests
# ---------------------------------------------------------------------------

@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory."""
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "memory" / "global").mkdir(parents=True)
    (inst / "journal").mkdir(parents=True)
    (inst / "soul.md").write_text("You are Kōan.")
    (inst / "memory" / "summary.md").write_text("Summary.")
    (inst / "missions.md").write_text(
        "# Missions\n\n## Pending\n\n- Task 1\n\n## In Progress\n\n## Done\n\n"
    )
    return inst


@pytest.fixture
def app_client(instance_dir, tmp_path):
    """Create a Flask test client with patched paths."""
    tpl_dest = tmp_path / "koan" / "templates"
    shutil.copytree(REAL_TEMPLATES, tpl_dest)
    with patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
         patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"), \
         patch.object(dashboard, "OUTBOX_FILE", instance_dir / "outbox.md"), \
         patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
         patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
         patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"), \
         patch.object(dashboard, "PENDING_FILE", instance_dir / "journal" / "pending.md"), \
         patch.object(dashboard, "KOAN_ROOT", tmp_path):
        dashboard.app.config["TESTING"] = True
        dashboard.app.jinja_loader = FileSystemLoader(str(tpl_dest))
        with dashboard.app.test_client() as client:
            yield client


class TestPRsPage:
    def test_prs_page_renders(self, app_client):
        resp = app_client.get("/prs")
        assert resp.status_code == 200
        assert b"Pull Requests" in resp.data
        assert b"loadPRs" in resp.data

    def test_prs_page_has_refresh_button(self, app_client):
        resp = app_client.get("/prs")
        assert b"refresh-btn" in resp.data


class TestApiPRs:
    @patch("app.pr_tracker.fetch_all_prs")
    def test_returns_prs(self, mock_fetch, app_client):
        mock_fetch.return_value = {
            "prs": [SAMPLE_PR],
            "error": None,
            "stale": False,
        }
        resp = app_client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["prs"]) == 1
        assert data["error"] is None

    @patch("app.pr_tracker.fetch_all_prs")
    def test_project_filter(self, mock_fetch, app_client):
        mock_fetch.return_value = {"prs": [], "error": None, "stale": False}
        app_client.get("/api/prs?project=koan")
        mock_fetch.assert_called_once()
        args = mock_fetch.call_args
        assert args[1]["project_filter"] == "koan" or args[0][1] == "koan"

    @patch("app.pr_tracker.fetch_all_prs")
    def test_author_only_param(self, mock_fetch, app_client):
        mock_fetch.return_value = {"prs": [], "error": None, "stale": False}
        app_client.get("/api/prs?author_only=false")
        args = mock_fetch.call_args
        assert args[1].get("author_only") is False or not args[1].get("author_only", True)


class TestApiPRChecks:
    @patch("app.pr_tracker.fetch_pr_checks")
    def test_returns_checks(self, mock_fetch, app_client):
        mock_fetch.return_value = [
            {"name": "ci", "state": "completed", "conclusion": "success"},
        ]
        resp = app_client.get("/api/prs/myproject/42/checks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["checks"]) == 1


class TestApiPRMerge:
    @patch("app.pr_tracker.merge_pr")
    def test_merge_success(self, mock_merge, app_client):
        mock_merge.return_value = {"ok": True, "error": None, "url": "https://..."}
        resp = app_client.post("/api/prs/myproject/42/merge")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    @patch("app.pr_tracker.merge_pr")
    def test_merge_failure(self, mock_merge, app_client):
        mock_merge.return_value = {"ok": False, "error": "Auto-merge disabled"}
        resp = app_client.post("/api/prs/myproject/42/merge")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False


class TestNavLink:
    def test_prs_link_in_nav(self, app_client):
        resp = app_client.get("/")
        assert b'href="/prs"' in resp.data
        assert b">PRs<" in resp.data
