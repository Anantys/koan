"""Tests for sanity/project_paths.py — project path validation at startup."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


class TestIsGitRepo:
    """Tests for _is_git_repo() helper."""

    def test_valid_git_repo(self, tmp_path):
        """A directory with `git init` is recognized as a repo."""
        from sanity.project_paths import _is_git_repo

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        assert _is_git_repo(str(tmp_path)) is True

    def test_plain_directory(self, tmp_path):
        """A plain directory (no .git) is not a repo."""
        from sanity.project_paths import _is_git_repo

        assert _is_git_repo(str(tmp_path)) is False

    def test_nonexistent_path(self):
        """A nonexistent path is not a repo."""
        from sanity.project_paths import _is_git_repo

        assert _is_git_repo("/nonexistent/path/xyz") is False

    def test_timeout_returns_false(self, tmp_path):
        """Subprocess timeout returns False gracefully."""
        from sanity.project_paths import _is_git_repo

        with patch("sanity.project_paths.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("git", 5)):
            assert _is_git_repo(str(tmp_path)) is False

    def test_oserror_returns_false(self, tmp_path):
        """OSError (e.g. git not installed) returns False."""
        from sanity.project_paths import _is_git_repo

        with patch("sanity.project_paths.subprocess.run",
                   side_effect=OSError("git not found")):
            assert _is_git_repo(str(tmp_path)) is False


class TestRun:
    """Tests for the run() sanity check entry point."""

    def test_no_koan_root(self, monkeypatch):
        """Missing KOAN_ROOT env var returns empty result."""
        from sanity.project_paths import run

        monkeypatch.delenv("KOAN_ROOT", raising=False)
        modified, warnings = run("/some/instance")
        assert modified is False
        assert warnings == []

    def test_no_projects_yaml(self, tmp_path, monkeypatch):
        """No projects.yaml file returns empty result."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        modified, warnings = run(str(tmp_path / "instance"))
        assert modified is False
        assert warnings == []

    def test_all_paths_valid(self, tmp_path, monkeypatch):
        """All project paths exist and are git repos — no warnings."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        subprocess.run(["git", "init", str(project_dir)], capture_output=True)

        config = {
            "projects": {
                "myproject": {"path": str(project_dir)},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert warnings == []

    def test_missing_path_warns(self, tmp_path, monkeypatch):
        """A project with a nonexistent path produces a warning."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        config = {
            "projects": {
                "ghost": {"path": "/nonexistent/path/xyz"},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert len(warnings) == 1
        assert "ghost" in warnings[0]
        assert "does not exist" in warnings[0]

    def test_not_git_repo_warns(self, tmp_path, monkeypatch):
        """A project path that exists but isn't a git repo produces a warning."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()

        config = {
            "projects": {
                "plain": {"path": str(plain_dir)},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert len(warnings) == 1
        assert "plain" in warnings[0]
        assert "not a git repository" in warnings[0]

    def test_mixed_valid_and_invalid(self, tmp_path, monkeypatch):
        """Mix of valid and invalid paths only warns about invalid ones."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        good_dir = tmp_path / "good"
        good_dir.mkdir()
        subprocess.run(["git", "init", str(good_dir)], capture_output=True)

        config = {
            "projects": {
                "good": {"path": str(good_dir)},
                "missing": {"path": "/nonexistent/xyz"},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert len(warnings) == 1
        assert "missing" in warnings[0]

    def test_workspace_project_without_path_skipped(self, tmp_path, monkeypatch):
        """Projects without a path field (workspace overrides) are skipped."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        config = {
            "projects": {
                "override-only": {"cli_provider": "claude"},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert warnings == []

    def test_none_project_entry_skipped(self, tmp_path, monkeypatch):
        """None project entries (e.g. placeholder) are skipped."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        config = {
            "projects": {
                "placeholder": None,
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert warnings == []

    def test_empty_path_skipped(self, tmp_path, monkeypatch):
        """Empty path string is treated as workspace-only override."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        config = {
            "projects": {
                "empty": {"path": ""},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert warnings == []

    def test_load_config_exception_handled(self, tmp_path, monkeypatch):
        """If load_projects_config raises, we return empty gracefully."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        with patch("app.projects_config.load_projects_config",
                   side_effect=ValueError("bad yaml")):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert warnings == []

    def test_never_modifies_files(self, tmp_path, monkeypatch):
        """The check is read-only: modified is always False."""
        from sanity.project_paths import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        config = {
            "projects": {
                "bad1": {"path": "/nonexistent/a"},
                "bad2": {"path": "/nonexistent/b"},
            }
        }
        with patch("app.projects_config.load_projects_config", return_value=config):
            modified, warnings = run(str(tmp_path / "instance"))

        assert modified is False
        assert len(warnings) == 2
