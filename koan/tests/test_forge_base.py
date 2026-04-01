"""Tests for forge/base.py — ForgeProvider ABC."""

import pytest

from app.forge.base import (
    ALL_FEATURES,
    FEATURE_CI_STATUS,
    FEATURE_ISSUES,
    FEATURE_NOTIFICATIONS,
    FEATURE_PR,
    FEATURE_PR_REVIEW_COMMENTS,
    FEATURE_REACTIONS,
    ForgeProvider,
)


class ConcreteForge(ForgeProvider):
    """Minimal concrete subclass — overrides nothing beyond what's needed."""
    name = "test"


class TestForgeProviderABC:
    def test_all_features_constant_contains_expected_values(self):
        assert FEATURE_PR in ALL_FEATURES
        assert FEATURE_ISSUES in ALL_FEATURES
        assert FEATURE_NOTIFICATIONS in ALL_FEATURES
        assert FEATURE_CI_STATUS in ALL_FEATURES
        assert FEATURE_REACTIONS in ALL_FEATURES
        assert FEATURE_PR_REVIEW_COMMENTS in ALL_FEATURES

    def test_cli_name_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.cli_name()

    def test_is_cli_available_returns_false_when_cli_name_raises(self):
        forge = ConcreteForge()
        assert forge.is_cli_available() is False

    def test_auth_env_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.auth_env()

    def test_parse_pr_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.parse_pr_url("https://example.com/owner/repo/pull/1")

    def test_parse_issue_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.parse_issue_url("https://example.com/owner/repo/issues/1")

    def test_search_pr_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.search_pr_url("some text")

    def test_search_issue_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.search_issue_url("some text")

    def test_pr_create_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.pr_create(title="t", body="b")

    def test_pr_view_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.pr_view(repo="owner/repo", number=1)

    def test_pr_diff_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.pr_diff(repo="owner/repo", number=1)

    def test_list_merged_prs_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.list_merged_prs(repo="owner/repo")

    def test_issue_create_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.issue_create(title="t", body="b")

    def test_run_api_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.run_api(endpoint="/repos/o/r")

    def test_get_ci_status_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.get_ci_status(repo="owner/repo", branch="main")

    def test_get_web_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.get_web_url(repo="owner/repo", url_type="pull", number=1)

    def test_detect_fork_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.detect_fork(project_path="/some/path")

    def test_supports_returns_false_by_default(self):
        forge = ConcreteForge()
        for feature in ALL_FEATURES:
            assert forge.supports(feature) is False

    def test_supports_unknown_feature_returns_false(self):
        forge = ConcreteForge()
        assert forge.supports("nonexistent_feature") is False
