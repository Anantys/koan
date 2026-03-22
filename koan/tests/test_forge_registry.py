"""Tests for app.forge.registry and app.forge.get_forge factory."""

import os

import pytest

from app.forge import GitHubForge, detect_forge_from_url, get_forge
from app.forge.github import GitHubForge
from app.forge.registry import DEFAULT_FORGE, FORGE_TYPES, get_forge_class


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestForgeRegistry:
    def test_github_type_maps_to_github_forge(self):
        cls = get_forge_class("github")
        assert cls is GitHubForge

    def test_unknown_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown forge type"):
            get_forge_class("bitbucket")

    def test_error_message_lists_known_types(self):
        with pytest.raises(ValueError, match="github"):
            get_forge_class("nonexistent")

    def test_forge_types_dict_not_empty(self):
        assert len(FORGE_TYPES) >= 1

    def test_default_forge_is_github(self):
        assert DEFAULT_FORGE == "github"

    def test_forge_types_values_are_forge_provider_subclasses(self):
        from app.forge.base import ForgeProvider
        for name, cls in FORGE_TYPES.items():
            assert issubclass(cls, ForgeProvider), f"{name} is not a ForgeProvider subclass"


# ---------------------------------------------------------------------------
# get_forge factory
# ---------------------------------------------------------------------------


class TestGetForge:
    def test_no_project_name_returns_github_forge(self, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", "")
        forge = get_forge()
        assert isinstance(forge, GitHubForge)

    def test_none_project_name_returns_github_forge(self, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", "")
        forge = get_forge(None)
        assert isinstance(forge, GitHubForge)

    def test_unknown_project_defaults_to_github(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text(
            "projects:\n  myproject:\n    path: /tmp\n"
        )
        forge = get_forge("nonexistent_project")
        assert isinstance(forge, GitHubForge)

    def test_project_with_no_forge_field_defaults_to_github(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text(
            "projects:\n  myproject:\n    path: /tmp\n"
        )
        forge = get_forge("myproject")
        assert isinstance(forge, GitHubForge)

    def test_project_with_explicit_github_forge(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text(
            "projects:\n  myproject:\n    path: /tmp\n    forge: github\n"
        )
        forge = get_forge("myproject")
        assert isinstance(forge, GitHubForge)

    def test_project_with_forge_url_passed_to_instance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text(
            "projects:\n  myproject:\n    path: /tmp\n"
            "    forge: github\n    forge_url: https://github.company.com\n"
        )
        forge = get_forge("myproject")
        assert isinstance(forge, GitHubForge)
        assert forge.base_url == "https://github.company.com"

    def test_github_url_used_as_forge_url_fallback(self, tmp_path, monkeypatch):
        """github_url is accepted as a backward-compatible alias for forge_url."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text(
            "projects:\n  myproject:\n    path: /tmp\n"
            "    github_url: https://github.com/owner/repo\n"
        )
        forge = get_forge("myproject")
        assert isinstance(forge, GitHubForge)

    def test_returns_github_when_koan_root_not_set(self, monkeypatch):
        monkeypatch.delenv("KOAN_ROOT", raising=False)
        forge = get_forge("anyproject")
        assert isinstance(forge, GitHubForge)

    def test_returns_github_on_config_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("invalid: [yaml: {broken")
        forge = get_forge("myproject")
        assert isinstance(forge, GitHubForge)

    def test_unknown_forge_type_in_config_defaults_to_github(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text(
            "projects:\n  myproject:\n    path: /tmp\n    forge: unsupported_forge\n"
        )
        forge = get_forge("myproject")
        assert isinstance(forge, GitHubForge)


# ---------------------------------------------------------------------------
# detect_forge_from_url
# ---------------------------------------------------------------------------


class TestDetectForgeFromUrl:
    def test_github_url_returns_github_forge(self):
        forge = detect_forge_from_url("https://github.com/owner/repo/pull/1")
        assert isinstance(forge, GitHubForge)

    def test_gitlab_url_returns_forge(self):
        # Phase 2a not yet implemented — currently returns GitHubForge as placeholder
        forge = detect_forge_from_url("https://gitlab.com/owner/repo/-/merge_requests/1")
        assert forge is not None

    def test_codeberg_url_returns_forge(self):
        # Phase 2b not yet implemented — currently returns GitHubForge as placeholder
        forge = detect_forge_from_url("https://codeberg.org/owner/repo/pulls/1")
        assert forge is not None

    def test_unknown_domain_returns_github_forge(self):
        forge = detect_forge_from_url("https://bitbucket.org/owner/repo/pull-requests/1")
        assert isinstance(forge, GitHubForge)

    def test_empty_url_returns_github_forge(self):
        forge = detect_forge_from_url("")
        assert isinstance(forge, GitHubForge)
