"""Tests for forge/registry.py — forge type string → provider class mapping."""

import pytest

from app.forge.github import GitHubForge
from app.forge.registry import DEFAULT_FORGE, FORGE_TYPES, get_forge_class


class TestGetForgeClass:
    def test_github_returns_github_forge_class(self):
        cls = get_forge_class("github")
        assert cls is GitHubForge

    def test_returns_a_class_not_an_instance(self):
        cls = get_forge_class("github")
        assert isinstance(cls, type)

    def test_unknown_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown forge type"):
            get_forge_class("bitbucket")

    def test_error_message_lists_supported_types(self):
        with pytest.raises(ValueError, match="github"):
            get_forge_class("unknown")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            get_forge_class("")

    def test_case_sensitive_mismatch_raises(self):
        with pytest.raises(ValueError):
            get_forge_class("GitHub")


class TestForgeTypesRegistry:
    def test_default_forge_is_github(self):
        assert DEFAULT_FORGE == "github"

    def test_forge_types_contains_github(self):
        assert "github" in FORGE_TYPES

    def test_all_registry_values_are_forge_provider_subclasses(self):
        from app.forge.base import ForgeProvider
        for name, cls in FORGE_TYPES.items():
            assert issubclass(cls, ForgeProvider), (
                f"FORGE_TYPES[{name!r}] = {cls!r} is not a ForgeProvider subclass"
            )


class TestGetForge:
    """Tests for the get_forge() factory in forge/__init__.py."""

    def test_returns_github_forge_by_default(self):
        from app.forge import get_forge
        forge = get_forge(None)
        assert isinstance(forge, GitHubForge)

    def test_returns_github_forge_for_no_project(self):
        from app.forge import get_forge
        forge = get_forge()
        assert isinstance(forge, GitHubForge)

    def test_returns_github_forge_for_unconfigured_project(self):
        from app.forge import get_forge
        # Projects not in projects.yaml fall back to GitHub.
        forge = get_forge("project-that-does-not-exist")
        assert isinstance(forge, GitHubForge)


class TestDetectForgeFromUrl:
    def test_github_url_returns_github_forge(self):
        from app.forge import detect_forge_from_url
        forge = detect_forge_from_url("https://github.com/owner/repo/pull/1")
        assert isinstance(forge, GitHubForge)

    def test_unknown_domain_defaults_to_github(self):
        from app.forge import detect_forge_from_url
        forge = detect_forge_from_url("https://bitbucket.org/owner/repo/pulls/1")
        assert isinstance(forge, GitHubForge)

    def test_empty_string_defaults_to_github(self):
        from app.forge import detect_forge_from_url
        forge = detect_forge_from_url("")
        assert isinstance(forge, GitHubForge)
