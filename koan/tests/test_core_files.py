"""Tests for core_files — unversioned file integrity checker."""

import os
import pytest
from pathlib import Path

from app.core_files import (
    CORE_PATHS,
    PROJECT_CORE_PATHS,
    snapshot_core_files,
    check_core_files,
    log_integrity_warnings,
)


@pytest.fixture
def fake_koan_root(tmp_path):
    """Create a minimal koan root with all core files present."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "missions.md").write_text("# Missions\n")
    (instance / "config.yaml").write_text("enabled: true\n")
    (instance / "soul.md").write_text("# Soul\n")
    (instance / "memory").mkdir()
    (tmp_path / "projects.yaml").write_text("projects: []\n")
    return tmp_path


@pytest.fixture
def fake_project(tmp_path):
    """Create a minimal project directory with .env."""
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".env").write_text("SECRET=xxx\n")
    return proj


class TestSnapshotCoreFiles:
    def test_all_present(self, fake_koan_root):
        snap = snapshot_core_files(str(fake_koan_root))
        assert "instance/" in snap
        assert "instance/missions.md" in snap
        assert "instance/config.yaml" in snap
        assert "instance/soul.md" in snap
        assert "instance/memory/" in snap
        assert "projects.yaml" in snap

    def test_missing_file(self, fake_koan_root):
        (fake_koan_root / "projects.yaml").unlink()
        snap = snapshot_core_files(str(fake_koan_root))
        assert "projects.yaml" not in snap
        assert "instance/" in snap  # other files still present

    def test_missing_directory(self, fake_koan_root):
        import shutil
        shutil.rmtree(fake_koan_root / "instance" / "memory")
        snap = snapshot_core_files(str(fake_koan_root))
        assert "instance/memory/" not in snap
        assert "instance/" in snap

    def test_with_project_path(self, fake_koan_root, fake_project):
        snap = snapshot_core_files(str(fake_koan_root), str(fake_project))
        assert "project:.env" in snap

    def test_project_env_missing(self, fake_koan_root, tmp_path):
        proj = tmp_path / "noproj"
        proj.mkdir()
        snap = snapshot_core_files(str(fake_koan_root), str(proj))
        assert "project:.env" not in snap

    def test_no_project_path(self, fake_koan_root):
        snap = snapshot_core_files(str(fake_koan_root), None)
        # Should only contain koan root paths
        assert all(not p.startswith("project:") for p in snap)


class TestCheckCoreFiles:
    def test_no_changes(self, fake_koan_root):
        before = snapshot_core_files(str(fake_koan_root))
        warnings = check_core_files(str(fake_koan_root), before)
        assert warnings == []

    def test_file_removed(self, fake_koan_root):
        before = snapshot_core_files(str(fake_koan_root))
        (fake_koan_root / "projects.yaml").unlink()
        warnings = check_core_files(str(fake_koan_root), before)
        assert len(warnings) == 1
        assert "projects.yaml" in warnings[0]

    def test_directory_removed(self, fake_koan_root):
        before = snapshot_core_files(str(fake_koan_root))
        import shutil
        shutil.rmtree(fake_koan_root / "instance" / "memory")
        warnings = check_core_files(str(fake_koan_root), before)
        assert any("instance/memory/" in w for w in warnings)

    def test_multiple_removals(self, fake_koan_root):
        before = snapshot_core_files(str(fake_koan_root))
        (fake_koan_root / "projects.yaml").unlink()
        (fake_koan_root / "instance" / "soul.md").unlink()
        warnings = check_core_files(str(fake_koan_root), before)
        assert len(warnings) == 2

    def test_project_env_removed(self, fake_koan_root, fake_project):
        before = snapshot_core_files(str(fake_koan_root), str(fake_project))
        (fake_project / ".env").unlink()
        warnings = check_core_files(str(fake_koan_root), before, str(fake_project))
        assert len(warnings) == 1
        assert "Project file disappeared: .env" in warnings[0]

    def test_file_added_no_warning(self, fake_koan_root):
        """Adding new files should not trigger warnings."""
        # Snapshot without projects.yaml
        (fake_koan_root / "projects.yaml").unlink()
        before = snapshot_core_files(str(fake_koan_root))
        # Recreate it
        (fake_koan_root / "projects.yaml").write_text("projects: []\n")
        warnings = check_core_files(str(fake_koan_root), before)
        assert warnings == []

    def test_empty_snapshot_no_warnings(self, tmp_path):
        """If nothing existed before, nothing can disappear."""
        before = snapshot_core_files(str(tmp_path))
        warnings = check_core_files(str(tmp_path), before)
        assert warnings == []


class TestLogIntegrityWarnings:
    def test_no_warnings(self, capsys):
        log_integrity_warnings([])
        assert capsys.readouterr().err == ""

    def test_with_warnings(self, capsys):
        log_integrity_warnings(["Core file disappeared: projects.yaml"])
        err = capsys.readouterr().err
        assert "INTEGRITY CHECK FAILED" in err
        assert "projects.yaml" in err
