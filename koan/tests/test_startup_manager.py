"""Tests for app.startup_manager — decomposed startup steps."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def koan_root(tmp_path):
    """Create a minimal koan root with instance directory."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "missions.md").write_text(
        "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
    )
    (instance / "config.yaml").write_text("max_runs: 5\ninterval: 10\n")
    return tmp_path


@pytest.fixture
def instance(koan_root):
    return str(koan_root / "instance")


@pytest.fixture
def projects(tmp_path):
    p1 = tmp_path / "proj1"
    p1.mkdir()
    return [("proj1", str(p1))]


# ---------------------------------------------------------------------------
# Test: recover_crashed_missions
# ---------------------------------------------------------------------------

class TestRecoverCrashedMissions:
    @patch("app.recover.recover_missions")
    def test_calls_recover_missions(self, mock_recover, instance):
        from app.startup_manager import recover_crashed_missions
        recover_crashed_missions(instance)
        mock_recover.assert_called_once_with(instance)

    @patch("app.recover.recover_missions", side_effect=FileNotFoundError("no missions"))
    def test_propagates_exceptions(self, mock_recover, instance):
        from app.startup_manager import recover_crashed_missions
        with pytest.raises(FileNotFoundError):
            recover_crashed_missions(instance)


# ---------------------------------------------------------------------------
# Test: run_migrations
# ---------------------------------------------------------------------------

class TestRunMigrations:
    @patch("app.projects_migration.run_migration", return_value=[])
    def test_no_migrations(self, mock_migrate, capsys):
        from app.startup_manager import run_migrations
        run_migrations("/tmp/koan")
        mock_migrate.assert_called_once_with("/tmp/koan")

    @patch("app.projects_migration.run_migration", return_value=["migrated X", "migrated Y"])
    def test_logs_migration_messages(self, mock_migrate, capsys):
        from app.startup_manager import run_migrations
        run_migrations("/tmp/koan")
        out = capsys.readouterr().out
        assert "[migration] migrated X" in out
        assert "[migration] migrated Y" in out


# ---------------------------------------------------------------------------
# Test: populate_github_urls
# ---------------------------------------------------------------------------

class TestPopulateGithubUrls:
    @patch("app.projects_config.ensure_github_urls", return_value=[])
    def test_no_urls(self, mock_ensure):
        from app.startup_manager import populate_github_urls
        populate_github_urls("/tmp/koan")
        mock_ensure.assert_called_once_with("/tmp/koan")

    @patch("app.projects_config.ensure_github_urls", return_value=["added url for proj1"])
    def test_logs_messages(self, mock_ensure, capsys):
        from app.startup_manager import populate_github_urls
        populate_github_urls("/tmp/koan")
        out = capsys.readouterr().out
        assert "[github-urls] added url for proj1" in out


# ---------------------------------------------------------------------------
# Test: discover_workspace
# ---------------------------------------------------------------------------

class TestDiscoverWorkspace:
    @patch("app.projects_merged.get_warnings", return_value=[])
    @patch("app.projects_merged.populate_workspace_github_urls", return_value=0)
    @patch("app.projects_merged.get_yaml_project_names", return_value={"proj1"})
    @patch("app.projects_merged.refresh_projects", return_value=[("proj1", "/path/proj1")])
    def test_no_workspace_projects(self, mock_refresh, mock_yaml, mock_pop, mock_warn, capsys):
        from app.startup_manager import discover_workspace
        result = discover_workspace("/tmp/koan", [])
        assert result == [("proj1", "/path/proj1")]
        out = capsys.readouterr().out
        assert "[workspace]" not in out  # No workspace projects logged

    @patch("app.projects_merged.get_warnings", return_value=[])
    @patch("app.projects_merged.populate_workspace_github_urls", return_value=2)
    @patch("app.projects_merged.get_yaml_project_names", return_value={"proj1"})
    @patch("app.projects_merged.refresh_projects", return_value=[
        ("proj1", "/p1"), ("ws-proj", "/p2"), ("ws-proj2", "/p3"),
    ])
    def test_workspace_projects_logged(self, mock_refresh, mock_yaml, mock_pop, mock_warn, capsys):
        from app.startup_manager import discover_workspace
        result = discover_workspace("/tmp/koan", [])
        assert len(result) == 3
        out = capsys.readouterr().out
        assert "Discovered 2 project(s) from workspace/" in out
        assert "Cached 2 github_url(s)" in out

    @patch("app.projects_merged.get_warnings", return_value=["path conflict for X"])
    @patch("app.projects_merged.populate_workspace_github_urls", return_value=0)
    @patch("app.projects_merged.get_yaml_project_names", return_value=set())
    @patch("app.projects_merged.refresh_projects", return_value=[])
    def test_warnings_logged(self, mock_refresh, mock_yaml, mock_pop, mock_warn, capsys):
        from app.startup_manager import discover_workspace
        discover_workspace("/tmp/koan", [])
        out = capsys.readouterr().out
        assert "path conflict for X" in out


# ---------------------------------------------------------------------------
# Test: validate_config
# ---------------------------------------------------------------------------

class TestValidateConfig:
    @patch("app.utils.load_config", return_value={"max_runs_per_day": 20})
    def test_valid_config_no_warnings(self, mock_config, capsys):
        from app.startup_manager import validate_config
        validate_config("/tmp/koan")
        out = capsys.readouterr().out
        assert "[config]" not in out

    @patch("app.utils.load_config", return_value={"unknwon_key": 1, "debug": "yes"})
    def test_warns_on_bad_config(self, mock_config, capsys):
        from app.startup_manager import validate_config
        validate_config("/tmp/koan")
        out = capsys.readouterr().out
        assert "unrecognized key" in out
        assert "should be bool" in out


# ---------------------------------------------------------------------------
# Test: run_sanity_checks
# ---------------------------------------------------------------------------

class TestRunSanityChecks:
    @patch("sanity.run_all", return_value=[])
    def test_no_changes(self, mock_run_all, capsys):
        from app.startup_manager import run_sanity_checks
        run_sanity_checks("/tmp/instance")
        out = capsys.readouterr().out
        assert "Running sanity checks" in out

    @patch("sanity.run_all", return_value=[
        ("missions", True, ["Fixed header"]),
        ("config", False, []),
    ])
    def test_logs_modified_checks(self, mock_run_all, capsys):
        from app.startup_manager import run_sanity_checks
        run_sanity_checks("/tmp/instance")
        out = capsys.readouterr().out
        assert "[missions] Fixed header" in out
        assert "[config]" not in out  # Not modified


# ---------------------------------------------------------------------------
# Test: cleanup_memory
# ---------------------------------------------------------------------------

class TestCleanupMemory:
    @patch("app.startup_manager._should_run_cleanup", return_value=True)
    @patch("app.startup_manager._write_cleanup_marker")
    @patch("app.memory_manager.MemoryManager")
    def test_calls_run_cleanup(self, mock_mgr_cls, mock_write, mock_should, capsys):
        from app.startup_manager import cleanup_memory
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.summary_path.exists.return_value = True
        cleanup_memory("/tmp/instance")
        mock_mgr_cls.assert_called_once_with("/tmp/instance")
        mock_mgr.run_cleanup.assert_called_once()
        mock_write.assert_called_once()
        out = capsys.readouterr().out
        assert "Running memory cleanup" in out

    @patch("app.startup_manager._should_run_cleanup", return_value=True)
    @patch("app.startup_manager._write_cleanup_marker")
    @patch("app.memory_manager.MemoryManager")
    def test_hydrates_on_cold_boot(self, mock_mgr_cls, mock_write, mock_should, capsys):
        """When summary.md is missing but SNAPSHOT.md exists, hydrate first."""
        from app.startup_manager import cleanup_memory
        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.summary_path.exists.return_value = False
        snapshot_mock = type("P", (), {"exists": lambda s: True})()
        mock_mgr.memory_dir.__truediv__ = lambda s, x: snapshot_mock
        mock_mgr.instance_dir.__truediv__ = lambda s, x: type("P", (), {"exists": lambda s: False})()
        mock_mgr.hydrate_from_snapshot.return_value = {"memory/summary.md": True}
        cleanup_memory("/tmp/instance")
        mock_mgr.hydrate_from_snapshot.assert_called_once()
        mock_mgr.run_cleanup.assert_called_once()
        out = capsys.readouterr().out
        assert "Cold boot detected" in out

    @patch("app.startup_manager._should_run_cleanup", return_value=False)
    @patch("app.startup_manager._cleanup_marker_path")
    @patch("app.memory_manager.MemoryManager")
    def test_skips_when_recent(self, mock_mgr_cls, mock_marker_path, mock_should, tmp_path, capsys):
        """Cleanup should be skipped if it ran recently."""
        import time
        from app.startup_manager import cleanup_memory
        marker = tmp_path / ".koan-last-cleanup"
        marker.write_text(str(time.time() - 3600))  # 1 hour ago
        mock_marker_path.return_value = marker
        cleanup_memory("/tmp/instance")
        mock_mgr_cls.return_value.run_cleanup.assert_not_called()
        out = capsys.readouterr().out
        assert "cleanup skipped" in out


class TestCleanupThrottle:
    """Test _should_run_cleanup and _write_cleanup_marker."""

    def test_runs_when_no_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        from app.startup_manager import _should_run_cleanup
        assert _should_run_cleanup() is True

    def test_skips_when_recent(self, tmp_path, monkeypatch):
        import time
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        marker = tmp_path / ".koan-last-cleanup"
        marker.write_text(str(time.time()))  # just now
        from app.startup_manager import _should_run_cleanup
        assert _should_run_cleanup() is False

    def test_runs_when_old(self, tmp_path, monkeypatch):
        import time
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        marker = tmp_path / ".koan-last-cleanup"
        marker.write_text(str(time.time() - 25 * 3600))  # 25 hours ago
        from app.startup_manager import _should_run_cleanup
        assert _should_run_cleanup() is True

    def test_runs_on_corrupt_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        marker = tmp_path / ".koan-last-cleanup"
        marker.write_text("not a number")
        from app.startup_manager import _should_run_cleanup
        assert _should_run_cleanup() is True

    def test_write_marker(self, tmp_path, monkeypatch):
        import time
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        from app.startup_manager import _write_cleanup_marker
        _write_cleanup_marker()
        marker = tmp_path / ".koan-last-cleanup"
        assert marker.exists()
        ts = float(marker.read_text().strip())
        assert abs(ts - time.time()) < 5


# ---------------------------------------------------------------------------
# Test: prune_missions_done
# ---------------------------------------------------------------------------

class TestPruneMissionsDone:
    def test_prunes_old_done_items(self, tmp_path):
        from app.startup_manager import prune_missions_done

        missions = tmp_path / "missions.md"
        done_items = "\n".join(f"- Task {i} ✅" for i in range(100))
        missions.write_text(
            f"# Missions\n\n## Pending\n\n- Active\n\n## Done\n{done_items}\n"
        )

        prune_missions_done(str(tmp_path))

        content = missions.read_text()
        from app.missions import parse_sections
        sections = parse_sections(content)
        assert len(sections["done"]) == 50
        assert len(sections["pending"]) == 1

    def test_noop_when_no_missions_file(self, tmp_path):
        from app.startup_manager import prune_missions_done
        prune_missions_done(str(tmp_path))  # should not raise

    def test_noop_when_few_done_items(self, tmp_path):
        from app.startup_manager import prune_missions_done

        missions = tmp_path / "missions.md"
        content = "# Missions\n\n## Pending\n\n## Done\n- Task 1\n- Task 2\n"
        missions.write_text(content)

        prune_missions_done(str(tmp_path))
        assert missions.read_text() == content


# ---------------------------------------------------------------------------
# Test: cleanup_mission_history
# ---------------------------------------------------------------------------

class TestCleanupMissionHistory:
    @patch("app.mission_history.cleanup_old_entries")
    def test_calls_cleanup(self, mock_cleanup):
        from app.startup_manager import cleanup_mission_history
        cleanup_mission_history("/tmp/instance")
        mock_cleanup.assert_called_once_with("/tmp/instance")


# ---------------------------------------------------------------------------
# Test: check_health
# ---------------------------------------------------------------------------

class TestCheckHealth:
    @patch("app.health_check.check_and_alert")
    def test_default_max_age(self, mock_check, capsys):
        from app.startup_manager import check_health
        check_health("/tmp/koan")
        mock_check.assert_called_once_with("/tmp/koan", max_age=120)

    @patch("app.health_check.check_and_alert")
    def test_custom_max_age(self, mock_check):
        from app.startup_manager import check_health
        check_health("/tmp/koan", max_age=60)
        mock_check.assert_called_once_with("/tmp/koan", max_age=60)


# ---------------------------------------------------------------------------
# Test: check_self_reflection
# ---------------------------------------------------------------------------

class TestCheckSelfReflection:
    @patch("app.self_reflection.should_reflect", return_value=False)
    def test_not_due(self, mock_should, capsys):
        from app.startup_manager import check_self_reflection
        check_self_reflection("/tmp/instance")
        mock_should.assert_called_once()

    @patch("app.self_reflection.notify_outbox")
    @patch("app.self_reflection.save_reflection")
    @patch("app.self_reflection.run_reflection", return_value="Some observations")
    @patch("app.self_reflection.should_reflect", return_value=True)
    def test_triggers_reflection(self, mock_should, mock_run, mock_save, mock_notify):
        from app.startup_manager import check_self_reflection
        check_self_reflection("/tmp/instance")
        mock_run.assert_called_once()
        mock_save.assert_called_once()
        mock_notify.assert_called_once()

    @patch("app.self_reflection.run_reflection", return_value="")
    @patch("app.self_reflection.should_reflect", return_value=True)
    def test_empty_observations_skips_save(self, mock_should, mock_run):
        from app.startup_manager import check_self_reflection
        with patch("app.self_reflection.save_reflection") as mock_save:
            check_self_reflection("/tmp/instance")
            mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# Test: handle_start_on_pause
# ---------------------------------------------------------------------------

class TestHandleStartOnPause:
    @patch("app.utils.get_start_on_pause", return_value=False)
    def test_disabled(self, mock_config, koan_root):
        from app.startup_manager import handle_start_on_pause
        handle_start_on_pause(str(koan_root))
        assert not (koan_root / ".koan-pause").exists()

    @patch("app.utils.get_start_on_pause", return_value=True)
    def test_creates_pause_file(self, mock_config, koan_root, capsys):
        from app.startup_manager import handle_start_on_pause
        handle_start_on_pause(str(koan_root))
        assert (koan_root / ".koan-pause").exists()
        out = capsys.readouterr().out
        assert "start_on_pause=true" in out

    @patch("app.utils.get_start_on_pause", return_value=True)
    def test_preserves_manual_pause(self, mock_config, koan_root):
        from app.startup_manager import handle_start_on_pause
        (koan_root / ".koan-pause").write_text("manual\n1234567890\n")
        handle_start_on_pause(str(koan_root))
        # Manual pause preserved — content still starts with "manual"
        content = (koan_root / ".koan-pause").read_text()
        assert content.startswith("manual")

    @patch("app.utils.get_start_on_pause", return_value=True)
    def test_overwrites_quota_reason(self, mock_config, koan_root):
        from app.startup_manager import handle_start_on_pause
        (koan_root / ".koan-pause").write_text("quota\n1234567890\nresets 10am")
        handle_start_on_pause(str(koan_root))
        content = (koan_root / ".koan-pause").read_text()
        assert content.startswith("start_on_pause")

    @patch("app.utils.get_start_on_pause", return_value=True)
    def test_overwrites_max_runs_reason(self, mock_config, koan_root):
        from app.startup_manager import handle_start_on_pause
        (koan_root / ".koan-pause").write_text("max_runs\n1234567890\n")
        handle_start_on_pause(str(koan_root))
        content = (koan_root / ".koan-pause").read_text()
        assert content.startswith("start_on_pause")

    @patch("app.utils.get_start_on_pause", return_value=True)
    def test_noop_if_already_paused(self, mock_config, koan_root, capsys):
        from app.startup_manager import handle_start_on_pause
        (koan_root / ".koan-pause").touch()
        handle_start_on_pause(str(koan_root))
        out = capsys.readouterr().out
        # No log about entering pause (already paused)
        assert "start_on_pause=true" not in out


# ---------------------------------------------------------------------------
# Test: setup_git_identity
# ---------------------------------------------------------------------------

class TestSetupGitIdentity:
    def test_sets_git_env(self, monkeypatch):
        monkeypatch.setenv("KOAN_EMAIL", "koan@example.com")
        from app.startup_manager import setup_git_identity
        setup_git_identity()
        assert os.environ["GIT_AUTHOR_NAME"] == "Kōan"
        assert os.environ["GIT_AUTHOR_EMAIL"] == "koan@example.com"
        assert os.environ["GIT_COMMITTER_NAME"] == "Kōan"
        assert os.environ["GIT_COMMITTER_EMAIL"] == "koan@example.com"

    def test_noop_without_env(self, monkeypatch):
        monkeypatch.delenv("KOAN_EMAIL", raising=False)
        monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
        from app.startup_manager import setup_git_identity
        setup_git_identity()
        assert "GIT_AUTHOR_NAME" not in os.environ


# ---------------------------------------------------------------------------
# Test: setup_github_auth
# ---------------------------------------------------------------------------

class TestSetupGithubAuth:
    def test_noop_without_github_user(self, monkeypatch):
        monkeypatch.delenv("GITHUB_USER", raising=False)
        from app.startup_manager import setup_github_auth
        # Should not raise
        setup_github_auth()

    @patch("app.github_auth.setup_github_auth", return_value=True)
    def test_success(self, mock_auth, monkeypatch, capsys):
        monkeypatch.setenv("GITHUB_USER", "testuser")
        from app.startup_manager import setup_github_auth
        setup_github_auth()
        out = capsys.readouterr().out
        assert "authenticated as testuser" in out

    @patch("app.github_auth.setup_github_auth", return_value=False)
    def test_failure(self, mock_auth, monkeypatch, capsys):
        monkeypatch.setenv("GITHUB_USER", "testuser")
        from app.startup_manager import setup_github_auth
        setup_github_auth()
        out = capsys.readouterr().out
        assert "Warning: GitHub auth failed" in out


# ---------------------------------------------------------------------------
# Test: run_git_sync
# ---------------------------------------------------------------------------

class TestRunGitSync:
    @patch("app.git_sync.GitSync")
    def test_syncs_all_projects(self, mock_gs_cls, capsys):
        from app.startup_manager import run_git_sync
        projects = [("proj1", "/p1"), ("proj2", "/p2")]
        run_git_sync("/tmp/instance", projects)
        assert mock_gs_cls.call_count == 2
        mock_gs_cls.assert_any_call("/tmp/instance", "proj1", "/p1")
        mock_gs_cls.assert_any_call("/tmp/instance", "proj2", "/p2")

    @patch("app.git_sync.GitSync")
    def test_continues_on_error(self, mock_gs_cls, capsys):
        """One project failing should not stop sync of others."""
        from app.startup_manager import run_git_sync
        mock_inst = MagicMock()
        mock_inst.sync_and_report.side_effect = [Exception("fail"), None]
        mock_gs_cls.return_value = mock_inst
        run_git_sync("/tmp/instance", [("proj1", "/p1"), ("proj2", "/p2")])
        out = capsys.readouterr().out
        assert "Git sync failed for proj1" in out
        # Second project still attempted
        assert mock_inst.sync_and_report.call_count == 2


# ---------------------------------------------------------------------------
# Test: run_daily_report
# ---------------------------------------------------------------------------

class TestRunDailyReport:
    @patch("app.daily_report.send_daily_report")
    def test_calls_report(self, mock_report):
        from app.startup_manager import run_daily_report
        run_daily_report()
        mock_report.assert_called_once()


# ---------------------------------------------------------------------------
# Test: run_morning_ritual
# ---------------------------------------------------------------------------

class TestRunMorningRitual:
    @patch("app.rituals.run_ritual")
    def test_calls_morning_ritual(self, mock_ritual, capsys):
        from app.startup_manager import run_morning_ritual
        run_morning_ritual("/tmp/instance")
        mock_ritual.assert_called_once_with("morning", Path("/tmp/instance"))
        out = capsys.readouterr().out
        assert "Running morning ritual" in out


# ---------------------------------------------------------------------------
# Test: _safe_run
# ---------------------------------------------------------------------------

class TestSafeRun:
    def test_returns_result(self):
        from app.startup_manager import _safe_run
        result = _safe_run("test", lambda: 42)
        assert result == 42

    def test_catches_exception(self, capsys):
        from app.startup_manager import _safe_run
        result = _safe_run("step X", lambda: 1 / 0)
        assert result is None
        out = capsys.readouterr().out
        assert "step X failed" in out

    def test_passes_args(self):
        from app.startup_manager import _safe_run
        result = _safe_run("test", lambda x, y: x + y, 3, 4)
        assert result == 7

    def test_passes_kwargs(self):
        from app.startup_manager import _safe_run
        result = _safe_run("test", lambda *, x: x * 2, x=5)
        assert result == 10


# ---------------------------------------------------------------------------
# Test: run_startup (orchestrator)
# ---------------------------------------------------------------------------

class TestRunStartup:
    @patch("app.startup_manager.run_morning_ritual")
    @patch("app.startup_manager.run_daily_report")
    @patch("app.startup_manager.run_git_sync")
    @patch("app.run._notify")
    @patch("app.run._build_startup_status", return_value="Active")
    @patch("app.run.set_status")
    @patch("app.startup_manager.setup_github_auth")
    @patch("app.startup_manager.setup_git_identity")
    @patch("app.startup_manager.handle_start_on_pause")
    @patch("app.startup_manager.check_self_reflection")
    @patch("app.startup_manager.check_health")
    @patch("app.startup_manager.cleanup_mission_history")
    @patch("app.startup_manager.cleanup_memory")
    @patch("app.startup_manager.run_sanity_checks")
    @patch("app.startup_manager.discover_workspace", return_value=[("proj1", "/p1")])
    @patch("app.startup_manager.populate_github_urls")
    @patch("app.startup_manager.run_migrations")
    @patch("app.startup_manager.recover_crashed_missions")
    @patch("app.banners.print_agent_banner")
    @patch("app.utils.get_branch_prefix", return_value="koan/")
    @patch("app.utils.get_cli_binary_for_shell", return_value="claude")
    @patch("app.utils.get_interval_seconds", return_value=60)
    @patch("app.utils.get_max_runs", return_value=10)
    def test_returns_config(
        self,
        mock_max_runs, mock_interval, mock_cli, mock_prefix,
        mock_banner,
        mock_recover, mock_migrate, mock_gh_urls, mock_workspace,
        mock_sanity, mock_memory, mock_history, mock_health,
        mock_reflection, mock_pause, mock_git_id, mock_gh_auth,
        mock_set_status, mock_build_status, mock_notify,
        mock_git_sync, mock_daily, mock_ritual,
    ):
        from app.startup_manager import run_startup
        result = run_startup("/tmp/koan", "/tmp/koan/instance", [("proj1", "/p1")])
        assert result == (10, 60, "koan/")

    @patch("app.startup_manager.run_morning_ritual")
    @patch("app.startup_manager.run_daily_report")
    @patch("app.startup_manager.run_git_sync")
    @patch("app.run._notify")
    @patch("app.run._build_startup_status", return_value="Active")
    @patch("app.run.set_status")
    @patch("app.startup_manager.setup_github_auth")
    @patch("app.startup_manager.setup_git_identity")
    @patch("app.startup_manager.handle_start_on_pause")
    @patch("app.startup_manager.check_self_reflection")
    @patch("app.startup_manager.check_health")
    @patch("app.startup_manager.cleanup_mission_history")
    @patch("app.startup_manager.cleanup_memory")
    @patch("app.startup_manager.run_sanity_checks")
    @patch("app.startup_manager.discover_workspace", return_value=[("proj1", "/p1")])
    @patch("app.startup_manager.populate_github_urls")
    @patch("app.startup_manager.run_migrations")
    @patch("app.startup_manager.recover_crashed_missions")
    @patch("app.banners.print_agent_banner")
    @patch("app.utils.get_branch_prefix", return_value="koan/")
    @patch("app.utils.get_cli_binary_for_shell", return_value="claude")
    @patch("app.utils.get_interval_seconds", return_value=60)
    @patch("app.utils.get_max_runs", return_value=10)
    def test_calls_all_steps(
        self,
        mock_max_runs, mock_interval, mock_cli, mock_prefix,
        mock_banner,
        mock_recover, mock_migrate, mock_gh_urls, mock_workspace,
        mock_sanity, mock_memory, mock_history, mock_health,
        mock_reflection, mock_pause, mock_git_id, mock_gh_auth,
        mock_set_status, mock_build_status, mock_notify,
        mock_git_sync, mock_daily, mock_ritual,
    ):
        from app.startup_manager import run_startup
        run_startup("/tmp/koan", "/tmp/koan/instance", [("proj1", "/p1")])

        # Verify all steps were called
        mock_recover.assert_called_once()
        mock_migrate.assert_called_once()
        mock_gh_urls.assert_called_once()
        mock_workspace.assert_called_once()
        mock_sanity.assert_called_once()
        mock_memory.assert_called_once()
        mock_history.assert_called_once()
        mock_health.assert_called_once()
        mock_reflection.assert_called_once()
        mock_pause.assert_called_once()
        mock_git_id.assert_called_once()
        mock_gh_auth.assert_called_once()
        mock_git_sync.assert_called_once()
        mock_daily.assert_called_once()
        mock_ritual.assert_called_once()

    @patch("app.startup_manager.run_morning_ritual")
    @patch("app.startup_manager.run_daily_report")
    @patch("app.startup_manager.run_git_sync")
    @patch("app.run._notify")
    @patch("app.run._build_startup_status", return_value="Active")
    @patch("app.run.set_status")
    @patch("app.startup_manager.setup_github_auth")
    @patch("app.startup_manager.setup_git_identity")
    @patch("app.startup_manager.handle_start_on_pause")
    @patch("app.startup_manager.check_self_reflection")
    @patch("app.startup_manager.check_health")
    @patch("app.startup_manager.cleanup_mission_history")
    @patch("app.startup_manager.cleanup_memory")
    @patch("app.startup_manager.run_sanity_checks", side_effect=Exception("sanity boom"))
    @patch("app.startup_manager.discover_workspace", return_value=[("proj1", "/p1")])
    @patch("app.startup_manager.populate_github_urls")
    @patch("app.startup_manager.run_migrations")
    @patch("app.startup_manager.recover_crashed_missions")
    @patch("app.banners.print_agent_banner")
    @patch("app.utils.get_branch_prefix", return_value="koan/")
    @patch("app.utils.get_cli_binary_for_shell", return_value="claude")
    @patch("app.utils.get_interval_seconds", return_value=60)
    @patch("app.utils.get_max_runs", return_value=10)
    def test_survives_step_failure(
        self,
        mock_max_runs, mock_interval, mock_cli, mock_prefix,
        mock_banner,
        mock_recover, mock_migrate, mock_gh_urls, mock_workspace,
        mock_sanity, mock_memory, mock_history, mock_health,
        mock_reflection, mock_pause, mock_git_id, mock_gh_auth,
        mock_set_status, mock_build_status, mock_notify,
        mock_git_sync, mock_daily, mock_ritual,
        capsys,
    ):
        """A failing step should not prevent other steps from running."""
        from app.startup_manager import run_startup
        result = run_startup("/tmp/koan", "/tmp/koan/instance", [("proj1", "/p1")])
        assert result == (10, 60, "koan/")
        # Sanity failed but memory cleanup still ran
        mock_memory.assert_called_once()
        out = capsys.readouterr().out
        assert "Sanity checks failed" in out

    @patch("app.startup_manager.run_morning_ritual")
    @patch("app.startup_manager.run_daily_report")
    @patch("app.startup_manager.run_git_sync")
    @patch("app.run._notify")
    @patch("app.run._build_startup_status", return_value="Active")
    @patch("app.run.set_status")
    @patch("app.startup_manager.setup_github_auth")
    @patch("app.startup_manager.setup_git_identity")
    @patch("app.startup_manager.handle_start_on_pause")
    @patch("app.startup_manager.check_self_reflection")
    @patch("app.startup_manager.check_health")
    @patch("app.startup_manager.cleanup_mission_history")
    @patch("app.startup_manager.cleanup_memory")
    @patch("app.startup_manager.run_sanity_checks")
    @patch("app.startup_manager.discover_workspace", return_value=[])
    @patch("app.startup_manager.populate_github_urls")
    @patch("app.startup_manager.run_migrations")
    @patch("app.startup_manager.recover_crashed_missions")
    @patch("app.banners.print_agent_banner")
    @patch("app.utils.get_branch_prefix", return_value="koan/")
    @patch("app.utils.get_cli_binary_for_shell", return_value="claude")
    @patch("app.utils.get_interval_seconds", return_value=60)
    @patch("app.utils.get_max_runs", return_value=10)
    def test_handles_empty_projects_after_workspace_discovery(
        self,
        mock_max_runs, mock_interval, mock_cli, mock_prefix,
        mock_banner,
        mock_recover, mock_migrate, mock_gh_urls, mock_workspace,
        mock_sanity, mock_memory, mock_history, mock_health,
        mock_reflection, mock_pause, mock_git_id, mock_gh_auth,
        mock_set_status, mock_build_status, mock_notify,
        mock_git_sync, mock_daily, mock_ritual,
    ):
        """If workspace discovery empties the project list, startup should
        not crash with IndexError on projects[0][0]."""
        from app.startup_manager import run_startup
        # Should not raise IndexError
        result = run_startup("/tmp/koan", "/tmp/koan/instance", [("proj1", "/p1")])
        assert result == (10, 60, "koan/")
        # Verify notification was sent with "none" as current project
        call_args = mock_notify.call_args[0]
        assert "Current: none" in call_args[1]

    @patch("app.startup_manager.run_morning_ritual")
    @patch("app.startup_manager.run_daily_report")
    @patch("app.startup_manager.run_git_sync")
    @patch("app.run._notify")
    @patch("app.run._build_startup_status", return_value="Active")
    @patch("app.run.set_status")
    @patch("app.startup_manager.setup_github_auth", side_effect=Exception("auth boom"))
    @patch("app.startup_manager.setup_git_identity", side_effect=Exception("git id boom"))
    @patch("app.startup_manager.handle_start_on_pause", side_effect=Exception("pause boom"))
    @patch("app.startup_manager.check_self_reflection")
    @patch("app.startup_manager.check_health")
    @patch("app.startup_manager.cleanup_mission_history")
    @patch("app.startup_manager.cleanup_memory")
    @patch("app.startup_manager.run_sanity_checks")
    @patch("app.startup_manager.discover_workspace", return_value=[("proj1", "/p1")])
    @patch("app.startup_manager.populate_github_urls")
    @patch("app.startup_manager.run_migrations")
    @patch("app.startup_manager.recover_crashed_missions")
    @patch("app.banners.print_agent_banner")
    @patch("app.utils.get_branch_prefix", return_value="koan/")
    @patch("app.utils.get_cli_binary_for_shell", return_value="claude")
    @patch("app.utils.get_interval_seconds", return_value=60)
    @patch("app.utils.get_max_runs", return_value=10)
    def test_pause_git_auth_failures_dont_crash(
        self,
        mock_max_runs, mock_interval, mock_cli, mock_prefix,
        mock_banner,
        mock_recover, mock_migrate, mock_gh_urls, mock_workspace,
        mock_sanity, mock_memory, mock_history, mock_health,
        mock_reflection, mock_pause, mock_git_id, mock_gh_auth,
        mock_set_status, mock_build_status, mock_notify,
        mock_git_sync, mock_daily, mock_ritual,
        capsys,
    ):
        """handle_start_on_pause, setup_git_identity, and setup_github_auth
        failures must not crash startup — they are wrapped in _safe_run."""
        from app.startup_manager import run_startup
        result = run_startup("/tmp/koan", "/tmp/koan/instance", [("proj1", "/p1")])
        assert result == (10, 60, "koan/")
        # All three failed but startup still completed
        out = capsys.readouterr().out
        assert "Start on pause failed" in out
        assert "Git identity failed" in out
        assert "GitHub auth failed" in out
        # Later steps still ran
        mock_git_sync.assert_called_once()
        mock_notify.assert_called_once()
