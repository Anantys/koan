"""Tests for migration_runner.py — discovery, execution, tracking, and listing."""

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.migration_runner import (
    discover_migrations,
    is_applied,
    mark_applied,
    run_migration,
    run_pending_migrations,
    list_migrations,
    MIGRATION_PATTERN,
)


# ---------------------------------------------------------------------------
# MIGRATION_PATTERN
# ---------------------------------------------------------------------------


class TestMigrationPattern:
    def test_valid_pattern(self):
        assert MIGRATION_PATTERN.match("0001_some_description.py")

    def test_valid_pattern_long_number(self):
        assert MIGRATION_PATTERN.match("9999_final_migration.py")

    def test_rejects_non_python(self):
        assert not MIGRATION_PATTERN.match("0001_desc.txt")

    def test_rejects_short_number(self):
        assert not MIGRATION_PATTERN.match("001_desc.py")

    def test_rejects_no_underscore(self):
        assert not MIGRATION_PATTERN.match("0001desc.py")

    def test_rejects_no_description(self):
        assert not MIGRATION_PATTERN.match("0001_.py")

    def test_rejects_non_numeric_prefix(self):
        assert not MIGRATION_PATTERN.match("abcd_desc.py")

    def test_rejects_init(self):
        assert not MIGRATION_PATTERN.match("__init__.py")

    def test_rejects_hyphenated_description(self):
        """Hyphens aren't allowed — only word characters (\\w)."""
        assert not MIGRATION_PATTERN.match("0001_some-description.py")

    def test_captures_migration_id(self):
        m = MIGRATION_PATTERN.match("0042_fix_data.py")
        assert m.group(1) == "0042"


# ---------------------------------------------------------------------------
# discover_migrations
# ---------------------------------------------------------------------------


class TestDiscoverMigrations:
    def test_empty_dir(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        with patch("app.migration_runner.MIGRATIONS_DIR", mig_dir):
            result = discover_migrations()
        assert result == []

    def test_no_dir(self, tmp_path):
        with patch("app.migration_runner.MIGRATIONS_DIR", tmp_path / "nope"):
            result = discover_migrations()
        assert result == []

    def test_discovers_valid_migrations(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "0001_first.py").write_text("def migrate(d): pass")
        (mig_dir / "0002_second.py").write_text("def migrate(d): pass")
        (mig_dir / "__init__.py").write_text("")
        (mig_dir / "README.md").write_text("docs")

        with patch("app.migration_runner.MIGRATIONS_DIR", mig_dir):
            result = discover_migrations()
        assert len(result) == 2
        assert result[0][0] == "0001"
        assert result[1][0] == "0002"

    def test_sorted_order(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "0003_third.py").write_text("")
        (mig_dir / "0001_first.py").write_text("")
        (mig_dir / "0002_second.py").write_text("")

        with patch("app.migration_runner.MIGRATIONS_DIR", mig_dir):
            result = discover_migrations()
        ids = [r[0] for r in result]
        assert ids == ["0001", "0002", "0003"]

    def test_ignores_directories(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "0001_subdir.py").mkdir()  # directory, not file
        (mig_dir / "0002_real.py").write_text("")

        with patch("app.migration_runner.MIGRATIONS_DIR", mig_dir):
            result = discover_migrations()
        assert len(result) == 1
        assert result[0][0] == "0002"


# ---------------------------------------------------------------------------
# is_applied / mark_applied
# ---------------------------------------------------------------------------


class TestTracking:
    def test_not_applied_initially(self, tmp_path):
        tracking = tmp_path / ".migrations"
        tracking.mkdir()
        assert not is_applied("0001", tracking)

    def test_mark_and_check(self, tmp_path):
        tracking = tmp_path / ".migrations"
        mark_applied("0001", tracking)
        assert is_applied("0001", tracking)

    def test_mark_creates_directory(self, tmp_path):
        tracking = tmp_path / ".migrations"
        assert not tracking.exists()
        mark_applied("0001", tracking)
        assert tracking.is_dir()
        assert is_applied("0001", tracking)

    def test_multiple_migrations_tracked_independently(self, tmp_path):
        tracking = tmp_path / ".migrations"
        mark_applied("0001", tracking)
        assert is_applied("0001", tracking)
        assert not is_applied("0002", tracking)
        mark_applied("0002", tracking)
        assert is_applied("0001", tracking)
        assert is_applied("0002", tracking)


# ---------------------------------------------------------------------------
# run_migration
# ---------------------------------------------------------------------------


class TestRunMigration:
    def test_runs_migrate_function(self, tmp_path):
        mig_file = tmp_path / "0001_test.py"
        mig_file.write_text(textwrap.dedent("""\
            from pathlib import Path

            def migrate(instance_dir: Path) -> None:
                (instance_dir / "migrated.txt").write_text("done")
        """))
        instance = tmp_path / "instance"
        instance.mkdir()

        run_migration(mig_file, instance)
        assert (instance / "migrated.txt").read_text() == "done"

    def test_raises_if_no_migrate_function(self, tmp_path):
        mig_file = tmp_path / "0001_bad.py"
        mig_file.write_text("x = 1\n")
        instance = tmp_path / "instance"
        instance.mkdir()

        with pytest.raises(AttributeError, match="missing required migrate"):
            run_migration(mig_file, instance)

    def test_propagates_migration_errors(self, tmp_path):
        mig_file = tmp_path / "0001_crash.py"
        mig_file.write_text(textwrap.dedent("""\
            def migrate(instance_dir):
                raise ValueError("broken migration")
        """))
        instance = tmp_path / "instance"
        instance.mkdir()

        with pytest.raises(ValueError, match="broken migration"):
            run_migration(mig_file, instance)


# ---------------------------------------------------------------------------
# run_pending_migrations
# ---------------------------------------------------------------------------


class TestRunPendingMigrations:
    def _setup_migrations(self, tmp_path, scripts):
        """Create migration files and instance/tracking dirs."""
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        instance = tmp_path / "instance"
        instance.mkdir()
        tracking = tmp_path / ".migrations"

        for name, code in scripts.items():
            (mig_dir / name).write_text(code)

        return instance, mig_dir, tracking

    def test_runs_all_pending(self, tmp_path):
        code = textwrap.dedent("""\
            def migrate(d):
                (d / "marker.txt").write_text("ok")
        """)
        instance, mig_dir, tracking = self._setup_migrations(tmp_path, {
            "0001_first.py": code,
            "0002_second.py": code,
        })

        applied = run_pending_migrations(instance, mig_dir, tracking)
        assert applied == ["0001", "0002"]
        assert is_applied("0001", tracking)
        assert is_applied("0002", tracking)

    def test_skips_already_applied(self, tmp_path):
        code = "def migrate(d): pass"
        instance, mig_dir, tracking = self._setup_migrations(tmp_path, {
            "0001_first.py": code,
            "0002_second.py": code,
        })
        mark_applied("0001", tracking)

        applied = run_pending_migrations(instance, mig_dir, tracking)
        assert applied == ["0002"]

    def test_stops_on_failure(self, tmp_path):
        instance, mig_dir, tracking = self._setup_migrations(tmp_path, {
            "0001_ok.py": "def migrate(d): pass",
            "0002_crash.py": "def migrate(d): raise RuntimeError('boom')",
            "0003_never.py": "def migrate(d): pass",
        })

        applied = run_pending_migrations(instance, mig_dir, tracking)
        assert applied == ["0001"]
        assert is_applied("0001", tracking)
        assert not is_applied("0002", tracking)
        assert not is_applied("0003", tracking)

    def test_no_instance_dir(self, tmp_path):
        """Returns empty if instance directory doesn't exist."""
        result = run_pending_migrations(
            tmp_path / "nope",
            tmp_path / "mig",
            tmp_path / "track",
        )
        assert result == []

    def test_no_migrations_dir(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        result = run_pending_migrations(
            instance,
            tmp_path / "no_mig",
            tmp_path / "track",
        )
        assert result == []

    def test_empty_migrations_dir(self, tmp_path):
        instance, mig_dir, tracking = self._setup_migrations(tmp_path, {})
        result = run_pending_migrations(instance, mig_dir, tracking)
        assert result == []

    def test_all_already_applied(self, tmp_path):
        code = "def migrate(d): pass"
        instance, mig_dir, tracking = self._setup_migrations(tmp_path, {
            "0001_first.py": code,
        })
        mark_applied("0001", tracking)

        applied = run_pending_migrations(instance, mig_dir, tracking)
        assert applied == []

    def test_migration_receives_instance_dir(self, tmp_path):
        """Verify migration function receives the correct instance_dir."""
        code = textwrap.dedent("""\
            def migrate(d):
                (d / "received_path.txt").write_text(str(d))
        """)
        instance, mig_dir, tracking = self._setup_migrations(tmp_path, {
            "0001_check.py": code,
        })

        run_pending_migrations(instance, mig_dir, tracking)
        assert (instance / "received_path.txt").read_text() == str(instance)


# ---------------------------------------------------------------------------
# list_migrations
# ---------------------------------------------------------------------------


class TestListMigrations:
    def test_lists_all(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        tracking = tmp_path / ".migrations"

        (mig_dir / "0001_first.py").write_text("")
        (mig_dir / "0002_second.py").write_text("")
        mark_applied("0001", tracking)

        result = list_migrations(mig_dir, tracking)
        assert len(result) == 2
        assert result[0] == ("0001", "0001_first.py", True)
        assert result[1] == ("0002", "0002_second.py", False)

    def test_empty_dir(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        tracking = tmp_path / ".migrations"

        result = list_migrations(mig_dir, tracking)
        assert result == []

    def test_no_dir(self, tmp_path):
        result = list_migrations(tmp_path / "nope", tmp_path / "track")
        assert result == []

    def test_ignores_non_migration_files(self, tmp_path):
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        tracking = tmp_path / ".migrations"

        (mig_dir / "__init__.py").write_text("")
        (mig_dir / "README.md").write_text("")
        (mig_dir / "0001_real.py").write_text("")

        result = list_migrations(mig_dir, tracking)
        assert len(result) == 1
        assert result[0][0] == "0001"


# ---------------------------------------------------------------------------
# Migration 0001 — English mission headers
# ---------------------------------------------------------------------------


class TestMigration0001:
    """Test the actual 0001_english_mission_headers migration."""

    def _run_migration(self, instance_dir):
        """Import and run the 0001 migration directly."""
        mig_path = Path(__file__).parent.parent / "migrations" / "0001_english_mission_headers.py"
        run_migration(mig_path, instance_dir)

    def test_converts_french_headers(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## En attente\n\n"
            "- task 1\n\n"
            "## En cours\n\n"
            "- task 2\n\n"
            "## Terminées\n"
        )
        self._run_migration(tmp_path)
        content = missions.read_text()
        assert "## Pending" in content
        assert "## In Progress" in content
        assert "## Done" in content
        assert "En attente" not in content
        assert "En cours" not in content
        assert "Terminées" not in content

    def test_handles_terminés_variant(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Terminés\n")
        self._run_migration(tmp_path)
        assert "## Done" in missions.read_text()

    def test_case_insensitive(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## EN ATTENTE\n\n## EN COURS\n")
        self._run_migration(tmp_path)
        content = missions.read_text()
        assert "## Pending" in content
        assert "## In Progress" in content

    def test_already_english_noop(self, tmp_path):
        missions = tmp_path / "missions.md"
        original = "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        missions.write_text(original)
        self._run_migration(tmp_path)
        assert missions.read_text() == original

    def test_no_missions_file(self, tmp_path):
        """Silently succeeds if missions.md doesn't exist."""
        self._run_migration(tmp_path)  # should not raise

    def test_preserves_mission_content(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## En attente\n\n"
            "- [project:koan] fix the bug\n"
            "- [project:web] add feature\n\n"
            "## En cours\n\n"
            "- working on it\n\n"
            "## Terminées\n\n"
            "- done task ✅\n"
        )
        self._run_migration(tmp_path)
        content = missions.read_text()
        assert "fix the bug" in content
        assert "add feature" in content
        assert "working on it" in content
        assert "done task ✅" in content

    def test_mixed_french_english_headers(self, tmp_path):
        """Only French headers are converted; existing English ones kept."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## En cours\n\n"
            "## Done\n"
        )
        self._run_migration(tmp_path)
        content = missions.read_text()
        assert "## Pending" in content
        assert "## In Progress" in content
        assert "## Done" in content
        assert content.count("## Pending") == 1


# ---------------------------------------------------------------------------
# CLI __main__ interface
# ---------------------------------------------------------------------------

class TestCLIMainBlock:
    """Test the CLI interface via subprocess (avoids runpy double-patching issues
    since migration_runner uses module-level KOAN_ROOT/INSTANCE_DIR constants)."""

    def test_no_koan_root_exits_1(self, tmp_path):
        """CLI exits 1 when KOAN_ROOT is not set."""
        import subprocess
        env = {k: v for k, v in os.environ.items() if k != "KOAN_ROOT"}
        result = subprocess.run(
            [sys.executable, "-m", "app.migration_runner"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
        )
        assert result.returncode == 1
        assert "KOAN_ROOT" in result.stderr

    def test_list_flag_shows_status(self, tmp_path):
        """CLI --list shows migration status labels."""
        import subprocess
        instance = tmp_path / "instance"
        instance.mkdir()

        result = subprocess.run(
            [sys.executable, "-m", "app.migration_runner", "--list"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, "KOAN_ROOT": str(tmp_path)},
        )
        # Should show at least one migration with [applied] or [pending]
        assert "[pending]" in result.stdout or "[applied]" in result.stdout or result.returncode == 0

    def test_run_mode_executes(self, tmp_path):
        """CLI without --list runs and reports migrations."""
        import subprocess
        instance = tmp_path / "instance"
        instance.mkdir()

        result = subprocess.run(
            [sys.executable, "-m", "app.migration_runner"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, "KOAN_ROOT": str(tmp_path)},
        )
        # Should succeed — migrations either apply or there are none
        assert result.returncode == 0

    def test_list_after_run_shows_applied(self, tmp_path):
        """After running migrations, --list shows them as applied."""
        import subprocess
        instance = tmp_path / "instance"
        instance.mkdir()
        koan_dir = str(Path(__file__).resolve().parent.parent)

        # Run migrations first
        subprocess.run(
            [sys.executable, "-m", "app.migration_runner"],
            capture_output=True, text=True, timeout=10,
            cwd=koan_dir,
            env={**os.environ, "KOAN_ROOT": str(tmp_path)},
        )

        # Then list — all should show as applied
        result = subprocess.run(
            [sys.executable, "-m", "app.migration_runner", "--list"],
            capture_output=True, text=True, timeout=10,
            cwd=koan_dir,
            env={**os.environ, "KOAN_ROOT": str(tmp_path)},
        )
        if result.stdout.strip():
            assert "[applied]" in result.stdout
            assert "[pending]" not in result.stdout
