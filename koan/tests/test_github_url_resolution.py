"""Tests for GitHub URL project resolution feature.

Covers:
- get_github_remote() — git remote URL extraction
- save_projects_config() / ensure_github_urls() — auto-population
- resolve_project_path() with owner parameter
- Skill handler owner passthrough
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure test env vars don't leak (preserves KOAN_ROOT)."""
    for key in list(os.environ):
        if key.startswith("KOAN_") and key != "KOAN_ROOT":
            monkeypatch.delenv(key, raising=False)


# ─────────────────────────────────────────────────────
# Phase 1: get_github_remote()
# ─────────────────────────────────────────────────────


class TestGetGithubRemote:
    """Tests for get_github_remote() — extracting owner/repo from git remote."""

    def test_https_url(self, tmp_path):
        """Parses HTTPS GitHub remote."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/sukria/koan.git\n"
        )
        with patch("app.utils.subprocess.run", return_value=result):
            assert get_github_remote(str(tmp_path)) == "sukria/koan"

    def test_ssh_url(self, tmp_path):
        """Parses SSH GitHub remote."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="git@github.com:atoomic/Clone.git\n"
        )
        with patch("app.utils.subprocess.run", return_value=result):
            assert get_github_remote(str(tmp_path)) == "atoomic/clone"

    def test_https_without_dot_git(self, tmp_path):
        """Parses HTTPS URL without .git suffix."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/garu/Clone\n"
        )
        with patch("app.utils.subprocess.run", return_value=result):
            assert get_github_remote(str(tmp_path)) == "garu/clone"

    def test_non_github_remote(self, tmp_path):
        """Returns None for non-GitHub remotes."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://gitlab.com/user/repo.git\n"
        )
        # Both origin and upstream fail
        with patch("app.utils.subprocess.run", return_value=result):
            assert get_github_remote(str(tmp_path)) is None

    def test_no_remote(self, tmp_path):
        """Returns None when git remote fails."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal")
        with patch("app.utils.subprocess.run", return_value=result):
            assert get_github_remote(str(tmp_path)) is None

    def test_upstream_fallback(self, tmp_path):
        """Falls back to upstream when origin is not GitHub."""
        from app.utils import get_github_remote

        origin = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://gitlab.com/user/repo.git\n"
        )
        upstream = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/org/repo.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd[2] == "origin":
                return origin
            return upstream

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            assert get_github_remote(str(tmp_path)) == "org/repo"

    def test_timeout_handled(self, tmp_path):
        """Handles subprocess timeout gracefully."""
        from app.utils import get_github_remote

        with patch("app.utils.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            assert get_github_remote(str(tmp_path)) is None

    def test_git_not_found(self, tmp_path):
        """Handles missing git binary gracefully."""
        from app.utils import get_github_remote

        with patch("app.utils.subprocess.run", side_effect=FileNotFoundError):
            assert get_github_remote(str(tmp_path)) is None

    def test_case_normalization(self, tmp_path):
        """Owner/repo are normalized to lowercase."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/Sukria/Koan.git\n"
        )
        with patch("app.utils.subprocess.run", return_value=result):
            assert get_github_remote(str(tmp_path)) == "sukria/koan"

    def test_upstream_when_origin_absent(self, tmp_path):
        """Uses upstream when origin doesn't exist."""
        from app.utils import get_github_remote

        origin_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal")
        upstream_ok = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/sukria/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd[2] == "origin":
                return origin_fail
            return upstream_ok

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            assert get_github_remote(str(tmp_path)) == "sukria/koan"


# ─────────────────────────────────────────────────────
# Phase 1b: get_all_github_remotes()
# ─────────────────────────────────────────────────────


class TestGetAllGithubRemotes:
    """Tests for get_all_github_remotes() — extracting ALL owner/repo from all git remotes."""

    def test_single_origin(self, tmp_path):
        """Returns single remote when only origin exists."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\n")
        url_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/atoomic/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            return url_result

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            result = get_all_github_remotes(str(tmp_path))

        assert result == ["atoomic/koan"]

    def test_origin_and_upstream(self, tmp_path):
        """Returns both origin and upstream remotes."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\nupstream\n")
        origin_url = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="git@github.com:atoomic/koan.git\n"
        )
        upstream_url = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/sukria/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            if "origin" in cmd:
                return origin_url
            return upstream_url

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            result = get_all_github_remotes(str(tmp_path))

        assert "atoomic/koan" in result
        assert "sukria/koan" in result
        assert len(result) == 2

    def test_deduplicates(self, tmp_path):
        """Does not return duplicate entries for same owner/repo."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\nbackup\n")
        url_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/atoomic/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            return url_result

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            result = get_all_github_remotes(str(tmp_path))

        assert result == ["atoomic/koan"]

    def test_non_github_remotes_skipped(self, tmp_path):
        """Skips remotes that don't point to GitHub."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\ngitlab\n")
        origin_url = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/atoomic/koan.git\n"
        )
        gitlab_url = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://gitlab.com/atoomic/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            if "origin" in cmd:
                return origin_url
            return gitlab_url

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            result = get_all_github_remotes(str(tmp_path))

        assert result == ["atoomic/koan"]

    def test_no_remotes(self, tmp_path):
        """Returns empty list when no remotes exist."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="")

        with patch("app.utils.subprocess.run", return_value=list_result):
            result = get_all_github_remotes(str(tmp_path))

        assert result == []

    def test_git_remote_fails(self, tmp_path):
        """Returns empty list when git remote command fails."""
        from app.utils import get_all_github_remotes

        fail_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal")

        with patch("app.utils.subprocess.run", return_value=fail_result):
            result = get_all_github_remotes(str(tmp_path))

        assert result == []

    def test_timeout_handled(self, tmp_path):
        """Handles subprocess timeout gracefully."""
        from app.utils import get_all_github_remotes

        with patch("app.utils.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            result = get_all_github_remotes(str(tmp_path))

        assert result == []

    def test_case_normalization(self, tmp_path):
        """Owner/repo are normalized to lowercase."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\n")
        url_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/Sukria/Koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            return url_result

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            result = get_all_github_remotes(str(tmp_path))

        assert result == ["sukria/koan"]

    def test_individual_url_fetch_failure(self, tmp_path):
        """Continues past individual remote URL fetch failures."""
        from app.utils import get_all_github_remotes

        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\nupstream\n")
        origin_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal")
        upstream_ok = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/sukria/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            if "origin" in cmd:
                return origin_fail
            return upstream_ok

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            result = get_all_github_remotes(str(tmp_path))

        assert result == ["sukria/koan"]


# ─────────────────────────────────────────────────────
# Phase 2: save_projects_config() + ensure_github_urls()
# ─────────────────────────────────────────────────────


class TestSaveProjectsConfig:
    """Tests for save_projects_config() — atomic YAML writes."""

    def test_writes_valid_yaml(self, tmp_path):
        from app.projects_config import save_projects_config, load_projects_config

        config = {
            "projects": {
                "koan": {"path": str(tmp_path / "koan"), "github_url": "sukria/koan"}
            }
        }
        (tmp_path / "koan").mkdir()
        save_projects_config(str(tmp_path), config)

        result = load_projects_config(str(tmp_path))
        assert result["projects"]["koan"]["github_url"] == "sukria/koan"

    def test_preserves_existing_fields(self, tmp_path):
        from app.projects_config import save_projects_config, load_projects_config

        config = {
            "defaults": {"git_auto_merge": {"enabled": False}},
            "projects": {
                "koan": {
                    "path": str(tmp_path / "koan"),
                    "github_url": "sukria/koan",
                    "cli_provider": "claude",
                }
            }
        }
        (tmp_path / "koan").mkdir()
        save_projects_config(str(tmp_path), config)

        result = load_projects_config(str(tmp_path))
        assert result["projects"]["koan"]["cli_provider"] == "claude"
        assert result["defaults"]["git_auto_merge"]["enabled"] is False

    def test_atomic_write_has_header(self, tmp_path):
        from app.projects_config import save_projects_config

        config = {"projects": {"myapp": {"path": "/tmp/myapp"}}}
        save_projects_config(str(tmp_path), config)

        content = (tmp_path / "projects.yaml").read_text()
        assert "Kōan" in content
        assert "projects:" in content

    def test_handles_write_error(self, tmp_path):
        from app.projects_config import save_projects_config

        config = {"projects": {"myapp": {"path": "/tmp/myapp"}}}
        (tmp_path / "projects.yaml").write_text("old")
        with patch("app.utils.tempfile.mkstemp", side_effect=OSError("permission denied")):
            with pytest.raises(OSError):
                save_projects_config(str(tmp_path), config)


class TestSaveProjectsConfigComments:
    """Tests for comment preservation in save_projects_config()."""

    def test_preserves_inline_comments(self, tmp_path):
        from app.projects_config import save_projects_config, load_projects_config

        yaml_with_comments = (
            "# Main config header\n"
            "# Second header line\n\n"
            "projects:\n"
            "  myapp:\n"
            "    path: /tmp/myapp  # project root\n"
            "    exploration: true  # enable autonomous work\n"
        )
        (tmp_path / "projects.yaml").write_text(yaml_with_comments)

        config = load_projects_config(str(tmp_path))
        config["projects"]["myapp"]["exploration"] = False
        save_projects_config(str(tmp_path), config)

        saved = (tmp_path / "projects.yaml").read_text()
        assert "# Main config header" in saved
        assert "# Second header line" in saved
        assert "# project root" in saved
        assert "exploration: false" in saved

    def test_preserves_block_comments(self, tmp_path):
        from app.projects_config import save_projects_config, load_projects_config

        yaml_with_comments = (
            "# projects.yaml — my custom header\n"
            "\n"
            "# Default settings for all projects\n"
            "defaults:\n"
            "  exploration: true\n"
            "\n"
            "# Project list\n"
            "projects:\n"
            "  # My main app\n"
            "  webapp:\n"
            "    path: /tmp/webapp\n"
        )
        (tmp_path / "projects.yaml").write_text(yaml_with_comments)

        config = load_projects_config(str(tmp_path))
        config["projects"]["webapp"]["github_url"] = "user/webapp"
        save_projects_config(str(tmp_path), config)

        saved = (tmp_path / "projects.yaml").read_text()
        assert "# projects.yaml — my custom header" in saved
        assert "# Default settings for all projects" in saved
        assert "# Project list" in saved
        assert "# My main app" in saved
        assert "github_url: user/webapp" in saved

    def test_preserves_comments_on_new_field_addition(self, tmp_path):
        from app.projects_config import save_projects_config, load_projects_config

        yaml_with_comments = (
            "# Config with comments\n"
            "projects:\n"
            "  app1:\n"
            "    path: /tmp/app1  # first project\n"
            "  app2:\n"
            "    path: /tmp/app2  # second project\n"
        )
        (tmp_path / "projects.yaml").write_text(yaml_with_comments)

        config = load_projects_config(str(tmp_path))
        config["projects"]["app1"]["exploration"] = False
        save_projects_config(str(tmp_path), config)

        saved = (tmp_path / "projects.yaml").read_text()
        assert "# first project" in saved
        assert "# second project" in saved
        assert "exploration: false" in saved

    def test_new_file_gets_header(self, tmp_path):
        from app.projects_config import save_projects_config

        config = {"projects": {"newapp": {"path": "/tmp/newapp"}}}
        save_projects_config(str(tmp_path), config)

        saved = (tmp_path / "projects.yaml").read_text()
        assert "Kōan" in saved
        assert "projects:" in saved

    def test_roundtrip_preserves_full_structure(self, tmp_path):
        from app.projects_config import save_projects_config, load_projects_config

        yaml_content = (
            "# ===== Kōan projects =====\n"
            "# Edit this file to configure your projects.\n"
            "# Comments like this one will be preserved!\n"
            "\n"
            "defaults:\n"
            "  # Global settings\n"
            "  exploration: true  # allow autonomous exploration\n"
            "  git_auto_merge:\n"
            "    enabled: false  # manual merge only\n"
            "\n"
            "projects:\n"
            "  # ---- Production apps ----\n"
            "  webapp:\n"
            "    path: /tmp/webapp\n"
            "    exploration: false  # too risky for autonomous\n"
            "\n"
            "  # ---- Internal tools ----\n"
            "  toolbox:\n"
            "    path: /tmp/toolbox\n"
        )
        (tmp_path / "projects.yaml").write_text(yaml_content)

        config = load_projects_config(str(tmp_path))
        config["projects"]["webapp"]["exploration"] = True
        save_projects_config(str(tmp_path), config)

        saved = (tmp_path / "projects.yaml").read_text()
        assert "# ===== Kōan projects =====" in saved
        assert "# Edit this file to configure your projects." in saved
        assert "# Comments like this one will be preserved!" in saved
        assert "# Global settings" in saved
        assert "# allow autonomous exploration" in saved
        assert "# manual merge only" in saved
        assert "# ---- Production apps ----" in saved
        assert "# ---- Internal tools ----" in saved
        assert "exploration: true" in saved  # updated value


class TestEnsureGithubUrls:
    """Tests for ensure_github_urls() — auto-populating github_url."""

    def test_populates_missing_github_urls(self, tmp_path):
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {"path": str(koan_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_github_remote", return_value="sukria/koan"):
            msgs = ensure_github_urls(str(tmp_path))

        assert len(msgs) == 1
        assert "sukria/koan" in msgs[0]

        # Verify it was saved
        saved = yaml.safe_load((tmp_path / "projects.yaml").read_text())
        assert saved["projects"]["koan"]["github_url"] == "sukria/koan"

    def test_skips_existing_github_urls(self, tmp_path):
        from app.projects_config import ensure_github_urls

        config = {
            "projects": {
                "koan": {"path": "/tmp/koan", "github_url": "custom/koan"}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_github_remote") as mock_remote:
            msgs = ensure_github_urls(str(tmp_path))

        assert len(msgs) == 0
        mock_remote.assert_not_called()

    def test_skips_non_git_projects(self, tmp_path):
        from app.projects_config import ensure_github_urls

        config = {
            "projects": {
                "notes": {"path": str(tmp_path / "notes")}
            }
        }
        (tmp_path / "notes").mkdir()
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_github_remote", return_value=None):
            msgs = ensure_github_urls(str(tmp_path))

        assert len(msgs) == 0

    def test_idempotent(self, tmp_path):
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {"path": str(koan_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_github_remote", return_value="sukria/koan"):
            msgs1 = ensure_github_urls(str(tmp_path))
            msgs2 = ensure_github_urls(str(tmp_path))

        assert len(msgs1) == 1
        assert len(msgs2) == 0  # second run is a no-op

    def test_no_projects_yaml(self, tmp_path):
        from app.projects_config import ensure_github_urls
        # No projects.yaml file
        msgs = ensure_github_urls(str(tmp_path))
        assert msgs == []

    def test_skips_missing_paths(self, tmp_path):
        from app.projects_config import ensure_github_urls

        config = {
            "projects": {
                "ghost": {"path": "/nonexistent/path/ghost"}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        msgs = ensure_github_urls(str(tmp_path))
        assert len(msgs) == 0

    def test_handles_write_error_gracefully(self, tmp_path):
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {"path": str(koan_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_github_remote", return_value="sukria/koan"), \
             patch("app.projects_config.save_projects_config", side_effect=OSError("disk full")):
            msgs = ensure_github_urls(str(tmp_path))

        assert any("could not save" in m.lower() for m in msgs)


# ─────────────────────────────────────────────────────
# Phase 3: resolve_project_path() with owner
# ─────────────────────────────────────────────────────


class TestResolveProjectPathWithOwner:
    """Tests for enhanced resolve_project_path() with owner parameter."""

    def test_without_owner_unchanged(self, monkeypatch):
        """Without owner, behavior is identical to before."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", Path("/tmp/test"))
        monkeypatch.setenv("KOAN_PROJECTS", "koan:/home/koan;web:/home/web")

        from app.utils import resolve_project_path
        assert resolve_project_path("koan") == "/home/koan"

    def test_exact_name_match(self, monkeypatch):
        """Exact project name match still works."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", Path("/tmp/test"))
        monkeypatch.setenv("KOAN_PROJECTS", "koan:/home/koan")

        from app.utils import resolve_project_path
        assert resolve_project_path("koan", owner="sukria") == "/home/koan"

    def test_github_url_match(self, tmp_path, monkeypatch):
        """Matches via github_url in projects.yaml when name doesn't match."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-koan": {
                    "path": "/home/my-koan",
                    "github_url": "sukria/koan"
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        # repo name "koan" doesn't match project name "my-koan"
        # but github_url matches
        assert resolve_project_path("koan", owner="sukria") == "/home/my-koan"

    def test_github_url_case_insensitive(self, tmp_path, monkeypatch):
        """GitHub URL matching is case-insensitive."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "myapp": {
                    "path": "/home/myapp",
                    "github_url": "Sukria/Koan"
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        assert resolve_project_path("koan", owner="sukria") == "/home/myapp"

    def test_auto_discovery(self, tmp_path, monkeypatch):
        """Auto-discovers github_url from git remote when no match found."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "my-koan"
        project_dir.mkdir()
        config = {
            "projects": {
                "my-koan": {"path": str(project_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        # get_all_github_remotes calls: git remote (list), git remote get-url <name>
        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="origin\n")
        url_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/sukria/koan.git\n"
        )

        def side_effect(cmd, **kwargs):
            if cmd == ["git", "remote"]:
                return list_result
            return url_result

        with patch("app.utils.subprocess.run", side_effect=side_effect):
            from app.utils import resolve_project_path
            path = resolve_project_path("koan", owner="sukria")

        assert path == str(project_dir)

        # Verify auto-discovery saved to projects.yaml
        saved = yaml.safe_load((tmp_path / "projects.yaml").read_text())
        assert saved["projects"]["my-koan"]["github_url"] == "sukria/koan"

    def test_single_project_fallback(self, monkeypatch):
        """Falls back to single project without owner."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", Path("/tmp/test"))
        monkeypatch.setenv("KOAN_PROJECTS", "only:/home/only")

        from app.utils import resolve_project_path
        assert resolve_project_path("unknown") == "/home/only"

    def test_no_match_returns_none(self, monkeypatch):
        """Returns None when nothing matches with multiple projects."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", Path("/tmp/test"))
        monkeypatch.setenv("KOAN_PROJECTS", "koan:/home/koan;web:/home/web")

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            from app.utils import resolve_project_path
            assert resolve_project_path("unknown", owner="somebody") is None

    def test_basename_match(self, monkeypatch):
        """Directory basename match still works."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", Path("/tmp/test"))
        monkeypatch.setenv("KOAN_PROJECTS", "myproject:/home/workspace/koan")

        from app.utils import resolve_project_path
        assert resolve_project_path("koan") == "/home/workspace/koan"

    def test_fork_scenario(self, tmp_path, monkeypatch):
        """Fork workflow: owner in URL differs from owner in github_url."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        # Project has github_url pointing to the upstream owner
        config = {
            "projects": {
                "koan": {
                    "path": "/home/koan",
                    "github_url": "sukria/koan"
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        # URL is from fork owner — doesn't match github_url
        # Should still match on project name
        assert resolve_project_path("koan", owner="atoomic") == "/home/koan"

    def test_cross_owner_via_upstream_remote(self, tmp_path, monkeypatch):
        """Cross-owner PR: URL owner is upstream, local project is fork.

        This is the key bug fix: /recreate https://github.com/sukria/koan/pull/171
        should find the local project even when github_url is atoomic/koan (fork)
        because the upstream git remote points to sukria/koan.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "koan"
        project_dir.mkdir()
        config = {
            "projects": {
                "koan": {
                    "path": str(project_dir),
                    "github_url": "atoomic/koan"  # Fork!
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        # get_all_github_remotes returns both origin (fork) and upstream (original)
        with patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan", "sukria/koan"]), \
             patch("app.utils.get_github_remote", return_value="atoomic/koan"):
            from app.utils import resolve_project_path
            # URL points to sukria/koan (upstream owner) — name "koan" matches
            # so it resolves via step 2 (exact name match)
            path = resolve_project_path("koan", owner="sukria")

        assert path == str(project_dir)

    def test_cross_owner_different_project_name(self, tmp_path, monkeypatch):
        """Cross-owner PR with non-matching project name.

        Project is named "my-fork" locally, github_url is "atoomic/koan" (fork),
        upstream remote points to "sukria/koan". URL: sukria/koan/pull/X.
        Steps 1-3 all fail — only step 4 (all remotes) catches it.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "my-fork"
        project_dir.mkdir()
        config = {
            "projects": {
                "my-fork": {
                    "path": str(project_dir),
                    "github_url": "atoomic/koan"  # Fork origin
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        # get_all_github_remotes returns both fork and upstream
        with patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan", "sukria/koan"]), \
             patch("app.utils.get_github_remote", return_value="atoomic/koan"):
            from app.utils import resolve_project_path
            path = resolve_project_path("koan", owner="sukria")

        assert path == str(project_dir)

    def test_cross_owner_no_upstream_remote(self, tmp_path, monkeypatch):
        """When project only has origin (no upstream), cross-owner matches via step 6.

        Step 6 (repo-name match): github_url "atoomic/koan" has repo "koan",
        which matches the requested repo "koan" — different owner, same repo.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "my-fork"
        project_dir.mkdir()
        config = {
            "projects": {
                "my-fork": {
                    "path": str(project_dir),
                    "github_url": "atoomic/koan"
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        # Only origin remote, no upstream — steps 1-4 fail,
        # but step 6 matches on repo name "koan" from github_url
        with patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan"]):
            from app.utils import resolve_project_path
            path = resolve_project_path("koan", owner="sukria")

        assert path == str(project_dir)

    def test_partial_name_match_aliased_clone(self, tmp_path, monkeypatch):
        """Aliased clone: repo 'perl-Convert-ASN1' cloned as 'Convert-ASN1'.

        Steps 1-3 all fail (name mismatch), but step 3b catches it via
        partial name matching + remote validation.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "Convert-ASN1"
        project_dir.mkdir()
        config = {
            "projects": {
                "Convert-ASN1": {"path": str(project_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes",
                   return_value=["cpan-authors/perl-convert-asn1"]):
            from app.utils import resolve_project_path
            path = resolve_project_path(
                "perl-Convert-ASN1", owner="cpan-authors"
            )

        assert path == str(project_dir)

    def test_partial_name_match_reverse(self, tmp_path, monkeypatch):
        """Reverse alias: local name is longer than repo name.

        E.g. local 'perl-Convert-ASN1' for repo 'Convert-ASN1'.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "perl-Convert-ASN1"
        project_dir.mkdir()
        config = {
            "projects": {
                "perl-Convert-ASN1": {"path": str(project_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes",
                   return_value=["cpan-authors/convert-asn1"]):
            from app.utils import resolve_project_path
            path = resolve_project_path(
                "Convert-ASN1", owner="cpan-authors"
            )

        assert path == str(project_dir)

    def test_partial_name_no_false_positive(self, tmp_path, monkeypatch):
        """Partial name match doesn't fire when remote doesn't confirm.

        Project 'ASN1' is a suffix of 'perl-Convert-ASN1' but remote
        doesn't match — should return None.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "ASN1"
        project_dir.mkdir()
        config = {
            "projects": {
                "ASN1": {"path": str(project_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes",
                   return_value=["other-org/totally-different"]):
            from app.utils import resolve_project_path
            path = resolve_project_path(
                "perl-Convert-ASN1", owner="cpan-authors"
            )

        assert path is None

    def test_all_urls_cache_match(self, tmp_path, monkeypatch):
        """In-memory all-URLs cache (workspace projects with fork remotes)."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "myfork:/home/myfork")

        with patch("app.projects_merged.get_all_projects",
                   return_value=[("myfork", "/home/myfork")]), \
             patch("app.projects_merged.get_github_url_cache",
                   return_value={"myfork": "atoomic/koan"}), \
             patch("app.projects_merged.get_all_github_urls_cache",
                   return_value={"myfork": ["atoomic/koan", "sukria/koan"]}):
            from app.utils import resolve_project_path
            path = resolve_project_path("koan", owner="sukria")

        assert path == "/home/myfork"


# ─────────────────────────────────────────────────────
# Phase 4b: Partial name candidate helper
# ─────────────────────────────────────────────────────


class TestFindPartialNameCandidates:
    """Tests for _find_partial_name_candidates() helper."""

    def test_suffix_match_dash(self):
        from app.utils import _find_partial_name_candidates
        projects = [("Convert-ASN1", "/path/Convert-ASN1")]
        result = _find_partial_name_candidates("perl-convert-asn1", projects)
        assert len(result) == 1
        assert result[0] == ("Convert-ASN1", "/path/Convert-ASN1")

    def test_suffix_match_underscore(self):
        from app.utils import _find_partial_name_candidates
        projects = [("Convert_ASN1", "/path/Convert_ASN1")]
        result = _find_partial_name_candidates("perl_convert_asn1", projects)
        assert len(result) == 1

    def test_reverse_suffix(self):
        from app.utils import _find_partial_name_candidates
        projects = [("perl-Convert-ASN1", "/path/perl-Convert-ASN1")]
        result = _find_partial_name_candidates("convert-asn1", projects)
        assert len(result) == 1

    def test_no_match(self):
        from app.utils import _find_partial_name_candidates
        projects = [("totally-different", "/path/a")]
        result = _find_partial_name_candidates("perl-convert-asn1", projects)
        assert len(result) == 0

    def test_exact_match_excluded(self):
        """Exact matches are excluded (handled by earlier steps)."""
        from app.utils import _find_partial_name_candidates
        projects = [("convert-asn1", "/path/a")]
        result = _find_partial_name_candidates("convert-asn1", projects)
        assert len(result) == 0

    def test_basename_match(self):
        """Matches on directory basename, not just project name."""
        from app.utils import _find_partial_name_candidates
        projects = [("myproject", "/path/to/Convert-ASN1")]
        result = _find_partial_name_candidates("perl-convert-asn1", projects)
        assert len(result) == 1

    def test_no_mid_word_match(self):
        """Doesn't match if the suffix isn't at a word boundary."""
        from app.utils import _find_partial_name_candidates
        projects = [("onvert-ASN1", "/path/a")]  # no dash before
        result = _find_partial_name_candidates("perl-convert-asn1", projects)
        assert len(result) == 0


# ─────────────────────────────────────────────────────
# Phase 4: Skill handler owner passthrough
# ─────────────────────────────────────────────────────


class TestRebaseHandlerOwner:
    """Tests that /rebase handler passes owner to resolve_project_path."""

    def test_passes_owner(self, tmp_path):
        from skills.core.rebase.handler import handle

        ctx = MagicMock()
        ctx.args = "https://github.com/sukria/koan/pull/42"
        ctx.instance_dir = tmp_path

        with patch("app.utils.resolve_project_path", return_value="/home/koan") as mock_resolve, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission"):
            handle(ctx)

        mock_resolve.assert_called_once_with("koan", owner="sukria")


class TestRecreateHandlerOwner:
    """Tests that /recreate handler passes owner to resolve_project_path."""

    def test_passes_owner(self, tmp_path):
        from skills.core.recreate.handler import handle

        ctx = MagicMock()
        ctx.args = "https://github.com/garu/Clone/pull/10"
        ctx.instance_dir = tmp_path

        with patch("app.utils.resolve_project_path", return_value="/home/clone") as mock_resolve, \
             patch("app.utils.get_known_projects", return_value=[("clone", "/home/clone")]), \
             patch("app.utils.insert_pending_mission"):
            handle(ctx)

        mock_resolve.assert_called_once_with("Clone", owner="garu")


class TestPrHandlerOwner:
    """Tests that /pr handler passes owner to resolve_project_path."""

    def test_passes_owner(self, tmp_path):
        from skills.core.pr.handler import handle

        ctx = MagicMock()
        ctx.args = "https://github.com/sukria/koan/pull/99"
        ctx.send_message = None

        with patch("app.utils.resolve_project_path", return_value="/home/koan") as mock_resolve, \
             patch("app.pr_review.run_pr_review", return_value=(True, "ok")):
            handle(ctx)

        mock_resolve.assert_called_once_with("koan", owner="sukria")


class TestCheckHandlerOwner:
    """Tests that /check handler passes owner for project resolution."""

    def test_passes_owner_for_pr(self, tmp_path):
        from skills.core.check.handler import handle

        ctx = MagicMock()
        ctx.args = "https://github.com/sukria/koan/pull/85"
        ctx.instance_dir = tmp_path

        with patch("app.utils.resolve_project_path", return_value="/home/koan") as mock_resolve, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission"):
            handle(ctx)

        mock_resolve.assert_called_once_with("koan", owner="sukria")

    def test_passes_owner_for_issue(self, tmp_path):
        from skills.core.check.handler import handle

        ctx = MagicMock()
        ctx.args = "https://github.com/garu/Clone/issues/18"
        ctx.instance_dir = tmp_path

        with patch("app.utils.resolve_project_path", return_value="/home/clone") as mock_resolve, \
             patch("app.utils.get_known_projects", return_value=[("clone", "/home/clone")]), \
             patch("app.utils.insert_pending_mission"):
            handle(ctx)

        mock_resolve.assert_called_once_with("Clone", owner="garu")


class TestPlanHandlerOwner:
    """Tests that /plan handler passes owner for project resolution."""

    def test_passes_owner_for_issue_url(self, tmp_path):
        from skills.core.plan.handler import handle

        ctx = MagicMock()
        ctx.args = "https://github.com/sukria/koan/issues/230"
        ctx.instance_dir = tmp_path

        with patch("app.utils.resolve_project_path", return_value="/home/koan") as mock_resolve, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission"):
            handle(ctx)

        mock_resolve.assert_called_once_with("koan", owner="sukria")


# ─────────────────────────────────────────────────────
# Phase 5: Startup integration
# ─────────────────────────────────────────────────────


class TestStartupEnsureGithubUrls:
    """Tests that run_startup calls ensure_github_urls."""

    def test_ensure_called_in_startup(self):
        """Verify the ensure_github_urls call exists in startup source."""
        import inspect
        from app import startup_manager

        # run_startup delegates to startup_manager — check canonical source
        source = inspect.getsource(startup_manager.populate_github_urls)
        assert "ensure_github_urls" in source
        assert "github-urls" in source


# ─────────────────────────────────────────────────────
# Integration / edge cases
# ─────────────────────────────────────────────────────


class TestEnsureGithubUrlsList:
    """Tests for github_urls (plural) population in ensure_github_urls()."""

    def test_populates_github_urls_list(self, tmp_path):
        """ensure_github_urls stores all remotes in github_urls list."""
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {"path": str(koan_dir)}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_github_remote", return_value="atoomic/koan"), \
             patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan", "sukria/koan"]):
            ensure_github_urls(str(tmp_path))

        saved = yaml.safe_load((tmp_path / "projects.yaml").read_text())
        assert saved["projects"]["koan"]["github_url"] == "atoomic/koan"
        assert "atoomic/koan" in saved["projects"]["koan"]["github_urls"]
        assert "sukria/koan" in saved["projects"]["koan"]["github_urls"]

    def test_refreshes_github_urls_when_changed(self, tmp_path):
        """github_urls is refreshed when remotes change."""
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {
                    "path": str(koan_dir),
                    "github_url": "atoomic/koan",
                    "github_urls": ["atoomic/koan"],
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        # A new upstream remote was added
        with patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan", "sukria/koan"]):
            ensure_github_urls(str(tmp_path))

        saved = yaml.safe_load((tmp_path / "projects.yaml").read_text())
        assert len(saved["projects"]["koan"]["github_urls"]) == 2
        assert "sukria/koan" in saved["projects"]["koan"]["github_urls"]

    def test_skips_github_urls_when_unchanged(self, tmp_path):
        """github_urls is NOT re-saved when already correct."""
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {
                    "path": str(koan_dir),
                    "github_url": "atoomic/koan",
                    "github_urls": ["atoomic/koan", "sukria/koan"],
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan", "sukria/koan"]), \
             patch("app.projects_config.save_projects_config") as mock_save:
            ensure_github_urls(str(tmp_path))

        mock_save.assert_not_called()

    def test_empty_all_remotes_skips_github_urls(self, tmp_path):
        """github_urls is NOT set when get_all_github_remotes returns empty."""
        from app.projects_config import ensure_github_urls

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        config = {
            "projects": {
                "koan": {"path": str(koan_dir), "github_url": "atoomic/koan"}
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            ensure_github_urls(str(tmp_path))

        saved = yaml.safe_load((tmp_path / "projects.yaml").read_text())
        assert "github_urls" not in saved["projects"]["koan"]


class TestResolveViaGithubUrlsList:
    """Tests for resolve_project_path using github_urls (plural) in step 1."""

    def test_cross_owner_match_via_github_urls(self, tmp_path, monkeypatch):
        """Step 1 matches cross-owner PR via github_urls list.

        This is the core improvement: when github_url is "atoomic/koan" (fork)
        but github_urls includes "sukria/koan" (upstream), step 1 resolves
        without falling through to the expensive step 4 subprocess calls.
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "atoomic/koan",
                    "github_urls": ["atoomic/koan", "sukria/koan"],
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        # target is "sukria/koan" — doesn't match github_url but IS in github_urls
        assert resolve_project_path("koan", owner="sukria") == "/home/my-fork"

    def test_github_urls_case_insensitive(self, tmp_path, monkeypatch):
        """github_urls matching is case-insensitive."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "myapp": {
                    "path": "/home/myapp",
                    "github_url": "Atoomic/Koan",
                    "github_urls": ["Atoomic/Koan", "Sukria/Koan"],
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        assert resolve_project_path("koan", owner="sukria") == "/home/myapp"

    def test_github_urls_not_present_uses_step6(self, tmp_path, monkeypatch):
        """When github_urls is absent, step 6 matches on repo name from github_url.

        Previously returned None — now step 6 (cross-owner repo-name match)
        finds the project because "atoomic/koan" has repo part "koan" matching
        the requested repo "koan".
        """
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "atoomic/koan",
                    # No github_urls field
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            from app.utils import resolve_project_path
            path = resolve_project_path("koan", owner="sukria")

        assert path == "/home/my-fork"

    def test_step4_persists_github_urls(self, tmp_path, monkeypatch):
        """Step 4 auto-discovery also persists github_urls list."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        project_dir = tmp_path / "my-fork"
        project_dir.mkdir()
        config = {
            "projects": {
                "my-fork": {
                    "path": str(project_dir),
                    "github_url": "atoomic/koan",
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes",
                   return_value=["atoomic/koan", "sukria/koan"]), \
             patch("app.utils.get_github_remote", return_value="atoomic/koan"):
            from app.utils import resolve_project_path
            path = resolve_project_path("koan", owner="sukria")

        assert path == str(project_dir)

        # Verify github_urls was persisted
        saved = yaml.safe_load((tmp_path / "projects.yaml").read_text())
        assert "github_urls" in saved["projects"]["my-fork"]
        assert "sukria/koan" in saved["projects"]["my-fork"]["github_urls"]

    def test_primary_github_url_still_fast(self, tmp_path, monkeypatch):
        """Primary github_url match (step 1 first check) still works."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "myapp": {
                    "path": "/home/myapp",
                    "github_url": "sukria/koan",
                    "github_urls": ["sukria/koan", "atoomic/koan"],
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        # Matches on primary github_url — fastest path
        assert resolve_project_path("koan", owner="sukria") == "/home/myapp"


class TestCrossOwnerRepoNameMatch:
    """Tests for Step 6: cross-owner repo-name match.

    When steps 1-5 all fail, step 6 matches the requested repo name
    against the repo component of configured github_url/github_urls.
    This handles the case where a user provides a PR URL from a
    different owner (upstream vs fork) and the project is named
    differently from the repo.
    """

    def test_matches_on_repo_name_only(self, tmp_path, monkeypatch):
        """Step 6 matches repo name from github_url when owner differs."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "atoomic/koan",
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=["atoomic/koan"]):
            from app.utils import resolve_project_path
            # "sukria/koan" doesn't match github_url "atoomic/koan" exactly
            # But repo "koan" matches the repo part of "atoomic/koan"
            assert resolve_project_path("koan", owner="sukria") == "/home/my-fork"

    def test_matches_via_github_urls_list(self, tmp_path, monkeypatch):
        """Step 6 also checks github_urls entries, not just github_url."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-project": {
                    "path": "/home/my-project",
                    "github_urls": ["alice/myrepo"],
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            from app.utils import resolve_project_path
            assert resolve_project_path("myrepo", owner="bob") == "/home/my-project"

    def test_case_insensitive(self, tmp_path, monkeypatch):
        """Step 6 repo-name matching is case-insensitive."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "Atoomic/KOAN",
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            from app.utils import resolve_project_path
            assert resolve_project_path("koan", owner="Sukria") == "/home/my-fork"

    def test_ambiguous_multiple_matches(self, tmp_path, monkeypatch):
        """Step 6 returns None when multiple projects match same repo name."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "fork-a": {
                    "path": "/home/fork-a",
                    "github_url": "alice/myrepo",
                },
                "fork-b": {
                    "path": "/home/fork-b",
                    "github_url": "bob/myrepo",
                },
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            from app.utils import resolve_project_path
            # Two projects have repo "myrepo" — ambiguous
            assert resolve_project_path("myrepo", owner="charlie") is None

    def test_no_match_when_repo_differs(self, tmp_path, monkeypatch):
        """Step 6 doesn't match when repo names differ."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "atoomic/other-repo",
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        with patch("app.utils.get_all_github_remotes", return_value=[]):
            from app.utils import resolve_project_path
            assert resolve_project_path("koan", owner="sukria") is None

    def test_skipped_without_owner(self, tmp_path, monkeypatch):
        """Step 6 is only used when owner is provided."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "atoomic/koan",
                },
                "my-other": {
                    "path": "/home/my-other",
                    "github_url": "bob/something",
                },
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.utils import resolve_project_path
        # Without owner, step 6 doesn't run (falls through to None for multiple projects)
        assert resolve_project_path("koan") is None

    def test_handles_projects_yaml_error(self, tmp_path, monkeypatch):
        """Step 6 handles errors gracefully."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "a:/a;b:/b")

        with patch("app.utils.get_all_github_remotes", return_value=[]), \
             patch("app.projects_config.load_projects_config", side_effect=OSError("disk")):
            from app.utils import resolve_project_path
            assert resolve_project_path("koan", owner="sukria") is None


class TestErrorMessageSuggestions:
    """Tests for improved error messages with closest match suggestions."""

    def test_suggests_matching_repo(self, tmp_path, monkeypatch):
        """Error message suggests projects with matching repo name."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "my-fork:/home/my-fork")

        config = {
            "projects": {
                "my-fork": {
                    "path": "/home/my-fork",
                    "github_url": "atoomic/koan",
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.github_skill_helpers import format_project_not_found_error
        msg = format_project_not_found_error("koan", owner="unknown-user")
        assert "atoomic/koan" in msg
        assert "Possible match" in msg

    def test_no_suggestion_without_owner(self):
        """No suggestion when no owner is provided."""
        from app.github_skill_helpers import format_project_not_found_error

        with patch("app.utils.get_known_projects", return_value=[("test", "/test")]):
            msg = format_project_not_found_error("koan")
        assert "Possible match" not in msg

    def test_no_suggestion_when_no_match(self, tmp_path, monkeypatch):
        """No suggestion when repo name doesn't match any project."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "myapp:/home/myapp")

        config = {
            "projects": {
                "myapp": {
                    "path": "/home/myapp",
                    "github_url": "alice/myapp",
                }
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.github_skill_helpers import format_project_not_found_error
        msg = format_project_not_found_error("koan", owner="sukria")
        assert "Possible match" not in msg

    def test_suggestion_with_multiple_matches(self, tmp_path, monkeypatch):
        """Shows all matching repos when multiple projects have same repo name."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "fork-a:/a;fork-b:/b")

        config = {
            "projects": {
                "fork-a": {
                    "path": "/a",
                    "github_url": "alice/koan",
                },
                "fork-b": {
                    "path": "/b",
                    "github_url": "bob/koan",
                },
            }
        }
        (tmp_path / "projects.yaml").write_text(yaml.dump(config))

        from app.github_skill_helpers import format_project_not_found_error
        msg = format_project_not_found_error("koan", owner="charlie")
        assert "alice/koan" in msg
        assert "bob/koan" in msg


class TestGithubUrlEdgeCases:
    """Edge cases for the full resolution pipeline."""

    def test_url_with_trailing_slash(self, tmp_path):
        """Trailing slash in URL doesn't break parsing."""
        from app.utils import get_github_remote

        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/sukria/koan/\n"
        )
        # The regex requires no trailing slash after repo name
        # but get_github_remote strips whitespace
        with patch("app.utils.subprocess.run", return_value=result):
            # Should handle or return None gracefully
            remote = get_github_remote(str(tmp_path))
            # URL has trailing slash — regex may or may not match,
            # but it should not crash
            assert remote is None or isinstance(remote, str)
