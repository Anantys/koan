"""Tests for forge/github.py — GitHubForge thin delegation wrapper."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.forge.base import FEATURE_PR, FEATURE_ISSUES, FEATURE_CI_STATUS, FEATURE_PR_REVIEW_COMMENTS
from app.forge.github import GitHubForge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_forge(base_url="https://github.com"):
    return GitHubForge(base_url=base_url)


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------

class TestGitHubForgeInit:
    def test_default_base_url(self):
        forge = GitHubForge()
        assert forge.base_url == "https://github.com"

    def test_custom_base_url(self):
        forge = GitHubForge(base_url="https://github.example.com")
        assert forge.base_url == "https://github.example.com"

    def test_trailing_slash_stripped(self):
        forge = GitHubForge(base_url="https://github.com/")
        assert forge.base_url == "https://github.com"

    def test_name_attribute(self):
        assert GitHubForge.name == "github"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCliName:
    def test_cli_name_is_gh(self):
        assert _make_forge().cli_name() == "gh"

    def test_is_cli_available_returns_bool(self):
        forge = _make_forge()
        # Just assert it's a bool; the actual result depends on the test env.
        assert isinstance(forge.is_cli_available(), bool)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuthEnv:
    def test_delegates_to_github_auth(self):
        forge = _make_forge()
        with patch("app.github_auth.get_gh_env", return_value={"GH_TOKEN": "tok"}) as mock:
            result = forge.auth_env()
        mock.assert_called_once()
        assert result == {"GH_TOKEN": "tok"}

    def test_returns_empty_dict_when_no_user_configured(self):
        forge = _make_forge()
        with patch("app.github_auth.get_gh_env", return_value={}):
            assert forge.auth_env() == {}


# ---------------------------------------------------------------------------
# URL parsing — delegates to github_url_parser
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_delegates_and_returns_tuple(self):
        forge = _make_forge()
        owner, repo, number = forge.parse_pr_url(
            "https://github.com/owner/repo/pull/42"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert number == "42"

    def test_invalid_url_raises_value_error(self):
        forge = _make_forge()
        with pytest.raises(ValueError):
            forge.parse_pr_url("https://github.com/owner/repo/issues/1")


class TestParseIssueUrl:
    def test_delegates_and_returns_tuple(self):
        forge = _make_forge()
        owner, repo, number = forge.parse_issue_url(
            "https://github.com/owner/repo/issues/99"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert number == "99"

    def test_invalid_url_raises_value_error(self):
        forge = _make_forge()
        with pytest.raises(ValueError):
            forge.parse_issue_url("https://github.com/owner/repo/pull/1")


class TestSearchPrUrl:
    def test_finds_embedded_url(self):
        forge = _make_forge()
        owner, repo, number = forge.search_pr_url(
            "See https://github.com/acme/widget/pull/7 for details"
        )
        assert owner == "acme"
        assert number == "7"

    def test_no_url_raises(self):
        forge = _make_forge()
        with pytest.raises(ValueError):
            forge.search_pr_url("no url here")


class TestSearchIssueUrl:
    def test_finds_embedded_url(self):
        forge = _make_forge()
        owner, repo, number = forge.search_issue_url(
            "Fixes https://github.com/acme/widget/issues/3"
        )
        assert owner == "acme"
        assert number == "3"

    def test_no_url_raises(self):
        forge = _make_forge()
        with pytest.raises(ValueError):
            forge.search_issue_url("no url here")


# ---------------------------------------------------------------------------
# PR operations
# ---------------------------------------------------------------------------

class TestPrCreate:
    @patch("app.github.subprocess.run")
    def test_delegates_to_github_pr_create(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
        )
        forge = _make_forge()
        url = forge.pr_create(title="My PR", body="body text", draft=True)
        assert "pull" in url

    @patch("app.github.subprocess.run")
    def test_passes_optional_args(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/pull/2\n"
        )
        forge = _make_forge()
        forge.pr_create(
            title="T", body="B", draft=False,
            base="main", repo="owner/repo", head="feature",
        )
        cmd = mock_run.call_args[0][0]
        assert "--base" in cmd
        assert "main" in cmd
        assert "--repo" in cmd
        # draft=False means no --draft flag
        assert "--draft" not in cmd


class TestPrView:
    @patch("app.github.subprocess.run")
    def test_returns_parsed_json(self, mock_run):
        payload = {"number": 42, "title": "My PR", "state": "open"}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(payload) + "\n"
        )
        forge = _make_forge()
        result = forge.pr_view(repo="owner/repo", number=42)
        assert result["number"] == 42
        assert result["title"] == "My PR"

    @patch("app.github.subprocess.run")
    def test_raises_on_json_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not-json")
        forge = _make_forge()
        with pytest.raises(RuntimeError, match="Failed to parse PR view output"):
            forge.pr_view(repo="owner/repo", number=1)


class TestPrDiff:
    @patch("app.github.subprocess.run")
    def test_returns_diff_text(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="diff --git a/foo b/foo\n"
        )
        forge = _make_forge()
        diff = forge.pr_diff(repo="owner/repo", number=5)
        assert "diff" in diff


class TestListMergedPrs:
    @patch("app.github.subprocess.run")
    def test_returns_branch_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="feature/branch-a\nfeature/branch-b\n"
        )
        forge = _make_forge()
        branches = forge.list_merged_prs(repo="owner/repo")
        assert "feature/branch-a" in branches
        assert "feature/branch-b" in branches

    @patch("app.github.subprocess.run")
    def test_empty_output_returns_empty_list(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        forge = _make_forge()
        assert forge.list_merged_prs(repo="owner/repo") == []


# ---------------------------------------------------------------------------
# Issue operations
# ---------------------------------------------------------------------------

class TestIssueCreate:
    @patch("app.github.subprocess.run")
    def test_delegates_to_github_issue_create(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/issues/1\n"
        )
        forge = _make_forge()
        url = forge.issue_create(title="Bug", body="description")
        assert "issues" in url

    @patch("app.github.subprocess.run")
    def test_passes_labels(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/issues/2\n"
        )
        forge = _make_forge()
        forge.issue_create(title="T", body="B", labels=["bug", "help wanted"])
        cmd = mock_run.call_args[0][0]
        assert "--label" in cmd


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------

class TestRunApi:
    @patch("app.github.subprocess.run")
    def test_delegates_to_github_api(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"id": 1}')
        forge = _make_forge()
        result = forge.run_api("repos/owner/repo")
        assert result == '{"id": 1}'


# ---------------------------------------------------------------------------
# CI Status
# ---------------------------------------------------------------------------

class TestGetCiStatus:
    @patch("app.github.subprocess.run")
    def test_returns_status_dict(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"status": "success", "total": 3}'
        )
        forge = _make_forge()
        status = forge.get_ci_status(repo="owner/repo", branch="main")
        assert status["status"] == "success"

    @patch("app.github.subprocess.run")
    def test_returns_unknown_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="not found", stdout=""
        )
        forge = _make_forge()
        status = forge.get_ci_status(repo="owner/repo", branch="missing")
        assert status["status"] == "unknown"


# ---------------------------------------------------------------------------
# Web URL construction
# ---------------------------------------------------------------------------

class TestGetWebUrl:
    def test_pr_url(self):
        forge = _make_forge()
        url = forge.get_web_url(repo="owner/repo", url_type="pull", number=42)
        assert url == "https://github.com/owner/repo/pull/42"

    def test_issue_url(self):
        forge = _make_forge()
        url = forge.get_web_url(repo="owner/repo", url_type="issues", number=5)
        assert url == "https://github.com/owner/repo/issues/5"

    def test_pr_alias(self):
        forge = _make_forge()
        url = forge.get_web_url(repo="owner/repo", url_type="pr", number=1)
        assert url == "https://github.com/owner/repo/pull/1"

    def test_custom_base_url(self):
        forge = GitHubForge(base_url="https://github.example.com")
        url = forge.get_web_url(repo="owner/repo", url_type="pull", number=99)
        assert url == "https://github.example.com/owner/repo/pull/99"


# ---------------------------------------------------------------------------
# Fork detection
# ---------------------------------------------------------------------------

class TestDetectFork:
    @patch("app.github.subprocess.run")
    def test_returns_parent_slug_for_fork(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="upstream/repo\n")
        forge = _make_forge()
        result = forge.detect_fork("/path/to/project")
        assert result == "upstream/repo"

    @patch("app.github.subprocess.run")
    def test_returns_none_when_not_fork(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="null/null\n")
        forge = _make_forge()
        result = forge.detect_fork("/path/to/project")
        assert result is None


# ---------------------------------------------------------------------------
# Feature matrix
# ---------------------------------------------------------------------------

class TestSupports:
    def test_supports_implemented_features(self):
        forge = _make_forge()
        for feature in (FEATURE_PR, FEATURE_ISSUES, FEATURE_CI_STATUS, FEATURE_PR_REVIEW_COMMENTS):
            assert forge.supports(feature) is True, f"Expected supports({feature!r}) to be True"

    def test_does_not_support_unimplemented_features(self):
        forge = _make_forge()
        assert forge.supports("notifications") is False
        assert forge.supports("reactions") is False

    def test_does_not_support_unknown_feature(self):
        forge = _make_forge()
        assert forge.supports("nonexistent") is False
