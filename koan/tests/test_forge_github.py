"""Tests for app.forge.github — GitHubForge thin wrapper."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.forge.base import ALL_FEATURES
from app.forge.github import GitHubForge


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


class TestGitHubForgeInit:
    def test_name(self):
        assert GitHubForge.name == "github"

    def test_default_base_url(self):
        forge = GitHubForge()
        assert forge.base_url == "https://github.com"

    def test_custom_base_url_github_enterprise(self):
        forge = GitHubForge(base_url="https://github.company.com")
        assert forge.base_url == "https://github.company.com"

    def test_cli_name(self):
        assert GitHubForge().cli_name() == "gh"

    def test_supports_all_features(self):
        forge = GitHubForge()
        for feature in ALL_FEATURES:
            assert forge.supports(feature) is True, f"Expected {feature!r} to be supported"

    def test_supports_unknown_feature_false(self):
        assert GitHubForge().supports("nonexistent") is False


# ---------------------------------------------------------------------------
# auth_env — delegates to github_auth.get_gh_env
# ---------------------------------------------------------------------------


class TestAuthEnv:
    def test_delegates_to_get_gh_env(self):
        with patch("app.forge.github.GitHubForge.auth_env") as mock_auth:
            mock_auth.return_value = {"GH_TOKEN": "ghp_test"}
            forge = GitHubForge()
            result = forge.auth_env()
        assert result == {"GH_TOKEN": "ghp_test"}

    def test_returns_empty_dict_when_no_user(self, monkeypatch):
        monkeypatch.delenv("GITHUB_USER", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        forge = GitHubForge()
        result = forge.auth_env()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# URL parsing — delegates to github_url_parser
# ---------------------------------------------------------------------------


class TestParsePrUrl:
    def test_valid_pr_url(self):
        forge = GitHubForge()
        owner, repo, number = forge.parse_pr_url("https://github.com/owner/repo/pull/42")
        assert owner == "owner"
        assert repo == "repo"
        assert number == "42"

    def test_invalid_url_raises(self):
        forge = GitHubForge()
        with pytest.raises(ValueError):
            forge.parse_pr_url("https://github.com/owner/repo/issues/42")

    def test_pr_url_with_fragment(self):
        forge = GitHubForge()
        owner, repo, number = forge.parse_pr_url(
            "https://github.com/owner/repo/pull/1#issuecomment-123"
        )
        assert number == "1"


class TestParseIssueUrl:
    def test_valid_issue_url(self):
        forge = GitHubForge()
        owner, repo, number = forge.parse_issue_url(
            "https://github.com/owner/repo/issues/99"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert number == "99"

    def test_invalid_url_raises(self):
        forge = GitHubForge()
        with pytest.raises(ValueError):
            forge.parse_issue_url("https://github.com/owner/repo/pull/99")


class TestSearchPrUrl:
    def test_finds_url_in_text(self):
        forge = GitHubForge()
        text = "See PR https://github.com/owner/repo/pull/7 for details."
        owner, repo, number = forge.search_pr_url(text)
        assert number == "7"

    def test_raises_when_not_found(self):
        forge = GitHubForge()
        with pytest.raises(ValueError):
            forge.search_pr_url("no url here")


class TestSearchIssueUrl:
    def test_finds_url_in_text(self):
        forge = GitHubForge()
        text = "Fixes https://github.com/owner/repo/issues/12"
        owner, repo, number = forge.search_issue_url(text)
        assert number == "12"

    def test_raises_when_not_found(self):
        forge = GitHubForge()
        with pytest.raises(ValueError):
            forge.search_issue_url("no url here")


# ---------------------------------------------------------------------------
# pr_create — delegates to app.github.pr_create
# ---------------------------------------------------------------------------


class TestPrCreate:
    @patch("app.github.subprocess.run")
    def test_delegates_and_returns_url(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
        )
        forge = GitHubForge()
        url = forge.pr_create(
            title="Test PR",
            body="body text",
            draft=True,
            repo="owner/repo",
        )
        assert "github.com" in url
        assert "pull" in url

    @patch("app.github.subprocess.run")
    def test_passes_draft_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/o/r/pull/2\n")
        forge = GitHubForge()
        forge.pr_create(title="T", body="B", draft=True, repo="o/r")
        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd

    @patch("app.github.subprocess.run")
    def test_no_draft_flag_when_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/o/r/pull/3\n")
        forge = GitHubForge()
        forge.pr_create(title="T", body="B", draft=False, repo="o/r")
        cmd = mock_run.call_args[0][0]
        assert "--draft" not in cmd


# ---------------------------------------------------------------------------
# issue_create — delegates to app.github.issue_create
# ---------------------------------------------------------------------------


class TestIssueCreate:
    @patch("app.github.subprocess.run")
    def test_delegates_and_returns_url(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/owner/repo/issues/5\n"
        )
        forge = GitHubForge()
        url = forge.issue_create(title="Bug", body="description")
        assert "issues" in url


# ---------------------------------------------------------------------------
# pr_view — wraps run_gh and parses JSON
# ---------------------------------------------------------------------------


class TestPrView:
    @patch("app.github.subprocess.run")
    def test_returns_parsed_dict(self, mock_run):
        pr_data = {"title": "My PR", "state": "OPEN", "number": 42}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(pr_data) + "\n"
        )
        forge = GitHubForge()
        result = forge.pr_view(repo="owner/repo", number="42")
        assert result["title"] == "My PR"
        assert result["state"] == "OPEN"

    @patch("app.github.subprocess.run")
    def test_returns_raw_on_json_parse_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not-json\n")
        forge = GitHubForge()
        result = forge.pr_view(repo="owner/repo", number="1")
        assert "raw" in result


# ---------------------------------------------------------------------------
# get_web_url — URL construction
# ---------------------------------------------------------------------------


class TestGetWebUrl:
    def test_repo_root(self):
        forge = GitHubForge()
        url = forge.get_web_url("owner/repo")
        assert url == "https://github.com/owner/repo"

    def test_pr_url(self):
        forge = GitHubForge()
        url = forge.get_web_url("owner/repo", url_type="pull", number="42")
        assert url == "https://github.com/owner/repo/pull/42"

    def test_issue_url(self):
        forge = GitHubForge()
        url = forge.get_web_url("owner/repo", url_type="issues", number="7")
        assert url == "https://github.com/owner/repo/issues/7"

    def test_github_enterprise_base_url(self):
        forge = GitHubForge(base_url="https://github.company.com")
        url = forge.get_web_url("myorg/myrepo", url_type="pull", number="1")
        assert url == "https://github.company.com/myorg/myrepo/pull/1"

    def test_trailing_slash_stripped(self):
        forge = GitHubForge(base_url="https://github.com/")
        url = forge.get_web_url("owner/repo")
        assert url == "https://github.com/owner/repo"

    def test_unknown_type_returns_repo_root(self):
        forge = GitHubForge()
        url = forge.get_web_url("owner/repo", url_type="discussion", number="5")
        assert url == "https://github.com/owner/repo"


# ---------------------------------------------------------------------------
# detect_fork — delegates to app.github.detect_parent_repo
# ---------------------------------------------------------------------------


class TestDetectFork:
    @patch("app.github.subprocess.run")
    def test_returns_parent_when_fork(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="upstream/repo\n")
        forge = GitHubForge()
        result = forge.detect_fork("/path/to/repo")
        assert result == "upstream/repo"

    @patch("app.github.subprocess.run")
    def test_returns_none_when_not_fork(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="\n")
        forge = GitHubForge()
        result = forge.detect_fork("/path/to/repo")
        assert result is None


# ---------------------------------------------------------------------------
# get_ci_status
# ---------------------------------------------------------------------------


class TestGetCiStatus:
    @patch("app.github.subprocess.run")
    def test_returns_status_dict(self, mock_run):
        status_data = {"status": "success", "total": 3}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(status_data) + "\n"
        )
        forge = GitHubForge()
        result = forge.get_ci_status(repo="owner/repo", branch="main")
        assert result["status"] == "success"

    @patch("app.github.subprocess.run")
    def test_returns_unknown_on_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        forge = GitHubForge()
        result = forge.get_ci_status(repo="owner/repo", branch="main")
        assert result == {"status": "unknown"}
