"""Tests for app.forge.base — ForgeProvider ABC."""

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
    """Minimal concrete subclass for instantiation tests."""
    name = "concrete"

    def cli_name(self):
        return "fake-cli"


class TestForgeProviderABC:
    def test_can_be_instantiated_via_subclass(self):
        forge = ConcreteForge()
        assert forge.name == "concrete"

    def test_base_url_default_empty(self):
        forge = ConcreteForge()
        assert forge.base_url == ""

    def test_base_url_stored(self):
        forge = ConcreteForge(base_url="https://example.com")
        assert forge.base_url == "https://example.com"

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
            forge.search_pr_url("text with https://example.com/owner/repo/pull/1")

    def test_search_issue_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.search_issue_url("text")

    def test_pr_create_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.pr_create(title="t", body="b")

    def test_pr_view_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.pr_view(repo="owner/repo", number="1")

    def test_pr_diff_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.pr_diff(repo="owner/repo", number="1")

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
            forge.run_api(endpoint="repos/owner/repo/issues")

    def test_get_ci_status_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.get_ci_status(repo="owner/repo", branch="main")

    def test_get_web_url_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.get_web_url(repo="owner/repo")

    def test_detect_fork_raises(self):
        forge = ConcreteForge()
        with pytest.raises(NotImplementedError):
            forge.detect_fork(project_path="/path/to/repo")

    def test_supports_returns_false_for_all_features(self):
        forge = ConcreteForge()
        for feature in ALL_FEATURES:
            assert forge.supports(feature) is False

    def test_supports_unknown_feature_returns_false(self):
        forge = ConcreteForge()
        assert forge.supports("nonexistent_feature") is False


class TestFeatureConstants:
    def test_all_features_tuple_contains_expected_values(self):
        assert FEATURE_PR in ALL_FEATURES
        assert FEATURE_ISSUES in ALL_FEATURES
        assert FEATURE_NOTIFICATIONS in ALL_FEATURES
        assert FEATURE_CI_STATUS in ALL_FEATURES
        assert FEATURE_REACTIONS in ALL_FEATURES
        assert FEATURE_PR_REVIEW_COMMENTS in ALL_FEATURES

    def test_all_features_are_strings(self):
        for f in ALL_FEATURES:
            assert isinstance(f, str)

    def test_all_features_are_unique(self):
        assert len(ALL_FEATURES) == len(set(ALL_FEATURES))


class TestIsCliAvailable:
    def test_returns_false_when_cli_not_on_path(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)
        forge = ConcreteForge()
        assert forge.is_cli_available() is False

    def test_returns_true_when_cli_found(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/fake-cli")
        forge = ConcreteForge()
        assert forge.is_cli_available() is True
