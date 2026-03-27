"""Tests for startup_info module."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.startup_info import (
    _get_file_size,
    _get_messaging_provider,
    _get_projects_summary,
    _get_provider,
    _get_skills_summary,
    gather_startup_info,
)


class TestGetProvider:
    def test_reads_from_koan_cli_provider_env(self, monkeypatch):
        monkeypatch.setenv("KOAN_CLI_PROVIDER", "copilot")
        assert _get_provider(Path("/tmp")) == "copilot"

    def test_falls_back_to_cli_provider(self, monkeypatch):
        monkeypatch.delenv("KOAN_CLI_PROVIDER", raising=False)
        monkeypatch.setenv("CLI_PROVIDER", "local")
        assert _get_provider(Path("/tmp")) == "local"

    def test_falls_back_to_config(self, monkeypatch):
        monkeypatch.delenv("KOAN_CLI_PROVIDER", raising=False)
        monkeypatch.delenv("CLI_PROVIDER", raising=False)
        with patch("app.utils.load_config", return_value={"cli_provider": "ollama"}):
            assert _get_provider(Path("/tmp")) == "ollama"

    def test_defaults_to_claude(self, monkeypatch):
        monkeypatch.delenv("KOAN_CLI_PROVIDER", raising=False)
        monkeypatch.delenv("CLI_PROVIDER", raising=False)
        with patch("app.utils.load_config", side_effect=OSError("no config")):
            assert _get_provider(Path("/tmp")) == "claude"


class TestGetProjectsSummary:
    def test_formats_project_list(self):
        projects = [("koan", "/a"), ("webapp", "/b")]
        with patch("app.utils.get_known_projects", return_value=projects):
            result = _get_projects_summary(Path("/tmp"))
            assert "2" in result
            assert "koan" in result
            assert "webapp" in result

    def test_truncates_long_list(self):
        projects = [(f"p{i}", f"/p{i}") for i in range(5)]
        with patch("app.utils.get_known_projects", return_value=projects):
            result = _get_projects_summary(Path("/tmp"))
            assert "5" in result
            assert "+2 more" in result

    def test_empty_projects(self):
        with patch("app.utils.get_known_projects", return_value=[]):
            assert _get_projects_summary(Path("/tmp")) == "none configured"

    def test_handles_exception(self):
        with patch("app.utils.get_known_projects", side_effect=OSError):
            assert _get_projects_summary(Path("/tmp")) == "unavailable"


class TestGetSkillsSummary:
    def test_core_only(self):
        with patch("app.skills.build_registry") as mock_reg:
            registry = mock_reg.return_value
            registry.list_by_scope.return_value = [None] * 29
            registry.all_skills.return_value = [None] * 29
            result = _get_skills_summary(Path("/tmp"), Path("/tmp/instance"))
            assert result == "29 core"

    def test_core_plus_extra(self):
        with patch("app.skills.build_registry") as mock_reg:
            registry = mock_reg.return_value
            registry.list_by_scope.return_value = [None] * 29
            registry.list_all.return_value = [None] * 32
            result = _get_skills_summary(Path("/tmp"), Path("/tmp/instance"))
            assert "29 core + 3 extra" in result

    def test_handles_exception(self):
        with patch("app.skills.build_registry", side_effect=ImportError):
            assert _get_skills_summary(Path("/tmp"), Path("/tmp/instance")) == "unavailable"


class TestGetFileSize:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("x" * 5000)
        assert _get_file_size(f) == "5k chars"

    def test_small_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello")
        assert _get_file_size(f) == "5 chars"

    def test_missing_file(self, tmp_path):
        assert _get_file_size(tmp_path / "missing.md") == "not found"


class TestGetMessagingProvider:
    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("KOAN_MESSAGING_PROVIDER", "slack")
        assert _get_messaging_provider() == "slack"

    def test_falls_back_to_config(self, monkeypatch):
        monkeypatch.delenv("KOAN_MESSAGING_PROVIDER", raising=False)
        with patch("app.utils.load_config", return_value={"messaging_provider": "slack"}):
            assert _get_messaging_provider() == "slack"

    def test_defaults_to_telegram(self, monkeypatch):
        monkeypatch.delenv("KOAN_MESSAGING_PROVIDER", raising=False)
        with patch("app.utils.load_config", return_value={}):
            assert _get_messaging_provider() == "telegram"


class TestGatherStartupInfo:
    def test_returns_all_keys(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "soul.md").write_text("test soul")
        with patch("app.startup_info._get_provider", return_value="claude"), \
             patch("app.startup_info._get_projects_summary", return_value="1 (koan)"), \
             patch("app.startup_info._get_skills_summary", return_value="29 core"), \
             patch("app.startup_info._get_messaging_provider", return_value="telegram"):
            info = gather_startup_info(tmp_path)
            assert "provider" in info
            assert "projects" in info
            assert "skills" in info
            assert "soul" in info
            assert "messaging" in info
