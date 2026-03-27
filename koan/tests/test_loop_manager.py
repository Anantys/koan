"""Tests for loop_manager.py — loop management utilities for the agent loop."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.slow


# --- Test resolve_focus_area ---


class TestResolveFocusArea:
    """Test focus area resolution from autonomous mode."""

    def test_review_mode(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("review")
        assert "READ-ONLY" in result
        assert "review" in result.lower()

    def test_implement_mode(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("implement")
        assert "implementation" in result.lower()

    def test_deep_mode(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("deep")
        assert "deep work" in result.lower()

    def test_unknown_mode_fallback(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("unknown")
        assert result == "General autonomous work"

    def test_has_mission_overrides_mode(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("deep", has_mission=True)
        assert result == "Execute assigned mission"

    def test_has_mission_with_review(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("review", has_mission=True)
        assert result == "Execute assigned mission"

    def test_wait_mode(self):
        from app.loop_manager import resolve_focus_area

        result = resolve_focus_area("wait")
        assert "pause" in result.lower() or "exhausted" in result.lower()


# --- Test validate_projects ---


class TestValidateProjects:
    """Test project configuration validation."""

    def test_valid_projects(self, tmp_path):
        from app.loop_manager import validate_projects

        p1 = tmp_path / "proj1"
        p2 = tmp_path / "proj2"
        p1.mkdir()
        p2.mkdir()
        # Initialize as git repos
        subprocess.run(["git", "init"], cwd=p1, capture_output=True)
        subprocess.run(["git", "init"], cwd=p2, capture_output=True)

        result = validate_projects([("proj1", str(p1)), ("proj2", str(p2))])
        assert result is None

    def test_empty_projects(self):
        from app.loop_manager import validate_projects

        result = validate_projects([])
        assert result is not None
        assert "No projects" in result

    def test_too_many_projects(self, tmp_path):
        from app.loop_manager import validate_projects

        projects = [(f"p{i}", str(tmp_path)) for i in range(51)]

        result = validate_projects(projects)
        assert result is not None
        assert "Max 50" in result

    def test_custom_max_projects(self, tmp_path):
        from app.loop_manager import validate_projects

        projects = [(f"p{i}", str(tmp_path)) for i in range(3)]

        # With max_projects=2, 3 projects should fail
        result = validate_projects(projects, max_projects=2)
        assert result is not None
        assert "Max 2" in result

    def test_missing_path(self, tmp_path):
        from app.loop_manager import validate_projects

        result = validate_projects([("proj1", "/nonexistent/path/xyz")])
        assert result is not None
        assert "does not exist" in result
        assert "proj1" in result

    def test_single_valid_project(self, tmp_path):
        from app.loop_manager import validate_projects

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        result = validate_projects([("koan", str(tmp_path))])
        assert result is None

    def test_non_git_directory(self, tmp_path):
        """A valid directory that is not a git repo should be rejected."""
        from app.loop_manager import validate_projects

        proj = tmp_path / "not-a-repo"
        proj.mkdir()

        result = validate_projects([("myproj", str(proj))])
        assert result is not None
        assert "not a git repository" in result
        assert "myproj" in result

    def test_mixed_git_and_non_git(self, tmp_path):
        """First project is a git repo, second is not — should catch the second."""
        from app.loop_manager import validate_projects

        p1 = tmp_path / "repo"
        p2 = tmp_path / "plain"
        p1.mkdir()
        p2.mkdir()
        subprocess.run(["git", "init"], cwd=p1, capture_output=True)

        result = validate_projects([("repo", str(p1)), ("plain", str(p2))])
        assert result is not None
        assert "plain" in result
        assert "not a git repository" in result


# --- Test lookup_project ---


class TestLookupProject:
    """Test project name to path lookup."""

    def test_found(self):
        from app.loop_manager import lookup_project

        result = lookup_project("web-app", [("koan", "/a"), ("web-app", "/b")])
        assert result == "/b"

    def test_not_found(self):
        from app.loop_manager import lookup_project

        result = lookup_project("unknown", [("koan", "/a"), ("web-app", "/b")])
        assert result is None

    def test_first_match(self):
        from app.loop_manager import lookup_project

        result = lookup_project("koan", [("koan", "/first"), ("koan", "/second")])
        assert result == "/first"

    def test_empty_list(self):
        from app.loop_manager import lookup_project

        result = lookup_project("anything", [])
        assert result is None


# --- Test format_project_list ---


class TestFormatProjectList:
    """Test project list formatting."""

    def test_sorted_output(self):
        from app.loop_manager import format_project_list

        result = format_project_list([("web-app", "/c"), ("backend", "/a"), ("koan", "/b")])
        lines = result.strip().split("\n")
        assert len(lines) == 3
        assert "backend" in lines[0]
        assert "koan" in lines[1]
        assert "web-app" in lines[2]

    def test_bullet_points(self):
        from app.loop_manager import format_project_list

        result = format_project_list([("proj", "/p")])
        assert "\u2022" in result  # bullet character

    def test_empty_list(self):
        from app.loop_manager import format_project_list

        result = format_project_list([])
        assert result == ""


# --- Test create_pending_file ---


class TestCreatePendingFile:
    """Test pending.md file creation."""

    def test_mission_pending(self, tmp_path):
        from app.loop_manager import create_pending_file

        instance = str(tmp_path / "instance")
        os.makedirs(os.path.join(instance, "journal"), exist_ok=True)

        path = create_pending_file(
            instance_dir=instance,
            project_name="koan",
            run_num=3,
            max_runs=20,
            autonomous_mode="implement",
            mission_title="Fix the bug",
        )

        content = Path(path).read_text()
        assert "# Mission: Fix the bug" in content
        assert "Project: koan" in content
        assert "Run: 3/20" in content
        assert "Mode: implement" in content
        assert "---" in content

    def test_autonomous_pending(self, tmp_path):
        from app.loop_manager import create_pending_file

        instance = str(tmp_path / "instance")
        os.makedirs(os.path.join(instance, "journal"), exist_ok=True)

        path = create_pending_file(
            instance_dir=instance,
            project_name="web-app",
            run_num=1,
            max_runs=25,
            autonomous_mode="deep",
        )

        content = Path(path).read_text()
        assert "# Autonomous run" in content
        assert "Project: web-app" in content
        assert "Run: 1/25" in content
        assert "Mode: deep" in content

    def test_creates_journal_directory(self, tmp_path):
        from app.loop_manager import create_pending_file

        instance = str(tmp_path / "instance")
        os.makedirs(os.path.join(instance, "journal"), exist_ok=True)

        create_pending_file(
            instance_dir=instance,
            project_name="koan",
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
        )

        # Should have created today's journal directory
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        assert os.path.isdir(os.path.join(instance, "journal", today))

    def test_returns_path(self, tmp_path):
        from app.loop_manager import create_pending_file

        instance = str(tmp_path / "instance")
        os.makedirs(os.path.join(instance, "journal"), exist_ok=True)

        path = create_pending_file(
            instance_dir=instance,
            project_name="koan",
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
        )

        assert path.endswith("pending.md")
        assert os.path.isfile(path)

    def test_mission_mode_fallback(self, tmp_path):
        from app.loop_manager import create_pending_file

        instance = str(tmp_path / "instance")
        os.makedirs(os.path.join(instance, "journal"), exist_ok=True)

        # Empty autonomous mode with mission title should show "mission"
        path = create_pending_file(
            instance_dir=instance,
            project_name="koan",
            run_num=1,
            max_runs=20,
            autonomous_mode="",
            mission_title="Do stuff",
        )

        content = Path(path).read_text()
        assert "Mode: mission" in content

    def test_uses_atomic_write(self, tmp_path):
        """pending.md must be written atomically to prevent corruption on crash."""
        from unittest.mock import patch

        from app.loop_manager import create_pending_file

        instance = str(tmp_path / "instance")
        os.makedirs(os.path.join(instance, "journal"), exist_ok=True)

        with patch("app.loop_manager.atomic_write") as mock_atomic:
            create_pending_file(
                instance_dir=instance,
                project_name="koan",
                run_num=1,
                max_runs=20,
                autonomous_mode="deep",
            )
            mock_atomic.assert_called_once()
            # Verify it was called with the pending.md path and content
            args = mock_atomic.call_args[0]
            assert str(args[0]).endswith("pending.md")
            assert "# Autonomous run" in args[1]


# --- Test interruptible_sleep ---


class TestInterruptibleSleep:
    """Test interruptible sleep with wake-on-event."""

    def test_timeout(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Very short interval + check interval — should timeout immediately
        result = interruptible_sleep(
            interval=1,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "timeout"

    def test_stop_file_wakes(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Pre-create stop file
        Path(os.path.join(koan_root, ".koan-stop")).touch()

        result = interruptible_sleep(
            interval=60,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "stop"

    def test_pause_file_wakes(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Pre-create pause file
        Path(os.path.join(koan_root, ".koan-pause")).touch()

        result = interruptible_sleep(
            interval=60,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "pause"

    def test_restart_file_wakes(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Pre-create restart file
        Path(os.path.join(koan_root, ".koan-restart")).touch()

        result = interruptible_sleep(
            interval=60,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "restart"

    def test_shutdown_file_wakes(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Pre-create shutdown file
        Path(os.path.join(koan_root, ".koan-shutdown")).touch()

        result = interruptible_sleep(
            interval=60,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "shutdown"

    def test_mission_wakes(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Create a missions.md with a pending mission
        missions_md = Path(instance) / "missions.md"
        missions_md.write_text("## Pending\n\n- Fix the bug\n\n## Done\n")

        result = interruptible_sleep(
            interval=60,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "mission"

    def test_priority_stop_over_pause(self, tmp_path):
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Both stop and pause — stop is checked first (after mission)
        Path(os.path.join(koan_root, ".koan-stop")).touch()
        Path(os.path.join(koan_root, ".koan-pause")).touch()

        result = interruptible_sleep(
            interval=60,
            koan_root=koan_root,
            instance_dir=instance,
            check_interval=1,
        )
        assert result == "stop"

    def test_detects_signal_before_sleeping(self, tmp_path):
        """Pre-existing signals must be detected immediately, without sleeping first."""
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Pre-create stop file — should be detected before any sleep
        Path(os.path.join(koan_root, ".koan-stop")).touch()

        import time as _time
        real_sleep = _time.sleep
        sleep_calls = []

        def tracking_sleep(secs):
            sleep_calls.append(secs)
            real_sleep(secs)

        with patch("app.loop_manager.time.sleep", side_effect=tracking_sleep):
            result = interruptible_sleep(
                interval=60,
                koan_root=koan_root,
                instance_dir=instance,
                check_interval=10,
            )

        assert result == "stop"
        # No sleep should have happened — signal detected on first check
        assert len(sleep_calls) == 0

    def test_does_not_overshoot_interval(self, tmp_path):
        """Sleep should not exceed the requested interval."""
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        import time as _time
        real_sleep = _time.sleep
        total_slept = [0.0]

        def tracking_sleep(secs):
            total_slept[0] += secs
            # Don't actually sleep — just track

        with patch("app.loop_manager.time.sleep", side_effect=tracking_sleep), \
             patch("app.loop_manager.process_github_notifications", return_value=0):
            result = interruptible_sleep(
                interval=25,
                koan_root=koan_root,
                instance_dir=instance,
                check_interval=10,
            )

        assert result == "timeout"
        # Total sleep should not exceed requested interval
        assert total_slept[0] <= 25.0

    def test_mission_detected_immediately_without_sleep(self, tmp_path):
        """A pending mission should be detected before any sleep call."""
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        # Pre-create mission
        missions_md = Path(instance) / "missions.md"
        missions_md.write_text("## Pending\n\n- Fix the bug\n\n## Done\n")

        sleep_calls = []

        def tracking_sleep(secs):
            sleep_calls.append(secs)

        with patch("app.loop_manager.time.sleep", side_effect=tracking_sleep):
            result = interruptible_sleep(
                interval=60,
                koan_root=koan_root,
                instance_dir=instance,
                check_interval=10,
            )

        assert result == "mission"
        assert len(sleep_calls) == 0

    def test_github_check_time_counts_toward_elapsed(self, tmp_path):
        """Slow GitHub API calls should count toward elapsed time,
        preventing the sleep loop from running longer than the interval."""
        from app.loop_manager import interruptible_sleep

        koan_root = str(tmp_path / "root")
        instance = str(tmp_path / "instance")
        os.makedirs(koan_root, exist_ok=True)
        os.makedirs(instance, exist_ok=True)

        call_count = [0]
        clock = [100.0]

        def mock_monotonic():
            return clock[0]

        def slow_github_check(*args, **kwargs):
            """Simulate a GitHub API call that takes 5s of wall time."""
            call_count[0] += 1
            clock[0] += 5.0  # Advance clock by 5s during the call
            return 0

        def tracking_sleep(secs):
            clock[0] += secs  # Advance clock during sleep too

        with patch("app.loop_manager.time.sleep", side_effect=tracking_sleep), \
             patch("app.loop_manager.time.monotonic", side_effect=mock_monotonic), \
             patch("app.loop_manager.process_github_notifications", side_effect=slow_github_check):
            result = interruptible_sleep(
                interval=10,
                koan_root=koan_root,
                instance_dir=instance,
                check_interval=3,
            )

        assert result == "timeout"
        # With 10s interval and 5s per GitHub check + 3s check_interval:
        # Iteration 1: github=5s elapsed, sleep=3s → elapsed=8s
        # Iteration 2: github=5s elapsed → elapsed=13s, breaks (remaining ≤ 0)
        # So github should be called exactly 2 times, not more.
        assert call_count[0] == 2, (
            f"GitHub check called {call_count[0]} times — "
            "elapsed should include API call time"
        )


# --- Test internal helpers ---


class TestCheckHelpers:
    """Test file-checking helper functions."""

    def test_check_signal_file_stop_exists(self, tmp_path):
        from app.loop_manager import _check_signal_file

        Path(tmp_path / ".koan-stop").touch()
        assert _check_signal_file(str(tmp_path), ".koan-stop") is True

    def test_check_signal_file_stop_missing(self, tmp_path):
        from app.loop_manager import _check_signal_file

        assert _check_signal_file(str(tmp_path), ".koan-stop") is False

    def test_check_signal_file_pause_exists(self, tmp_path):
        from app.loop_manager import _check_signal_file

        Path(tmp_path / ".koan-pause").touch()
        assert _check_signal_file(str(tmp_path), ".koan-pause") is True

    def test_check_signal_file_pause_missing(self, tmp_path):
        from app.loop_manager import _check_signal_file

        assert _check_signal_file(str(tmp_path), ".koan-pause") is False

    def test_check_signal_file_shutdown_exists(self, tmp_path):
        from app.loop_manager import _check_signal_file

        Path(tmp_path / ".koan-shutdown").touch()
        assert _check_signal_file(str(tmp_path), ".koan-shutdown") is True

    def test_check_signal_file_shutdown_missing(self, tmp_path):
        from app.loop_manager import _check_signal_file

        assert _check_signal_file(str(tmp_path), ".koan-shutdown") is False

    def test_check_signal_file_restart_exists(self, tmp_path):
        from app.loop_manager import _check_signal_file

        Path(tmp_path / ".koan-restart").touch()
        assert _check_signal_file(str(tmp_path), ".koan-restart") is True

    def test_check_signal_file_restart_missing(self, tmp_path):
        from app.loop_manager import _check_signal_file

        assert _check_signal_file(str(tmp_path), ".koan-restart") is False

    def test_check_pending_missions_with_missions(self, tmp_path):
        from app.loop_manager import check_pending_missions

        missions = tmp_path / "missions.md"
        missions.write_text("## Pending\n\n- Do something\n\n## Done\n")

        assert check_pending_missions(str(tmp_path)) is True

    def test_check_pending_missions_empty(self, tmp_path):
        from app.loop_manager import check_pending_missions

        missions = tmp_path / "missions.md"
        missions.write_text("## Pending\n\n## Done\n")

        assert check_pending_missions(str(tmp_path)) is False

    def test_check_pending_missions_no_file(self, tmp_path):
        from app.loop_manager import check_pending_missions

        assert check_pending_missions(str(tmp_path)) is False


# --- Test GitHub notification backoff ---


class TestGitHubNotificationBackoff:
    """Test exponential backoff for GitHub notification polling."""

    def setup_method(self):
        """Reset backoff state before each test."""
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_effective_interval_starts_at_base(self):
        from app.loop_manager import _get_effective_check_interval, _GITHUB_CHECK_INTERVAL
        assert _get_effective_check_interval() == _GITHUB_CHECK_INTERVAL

    def test_effective_interval_doubles_on_empty(self):
        import app.loop_manager as lm
        lm._consecutive_empty_checks = 1
        assert lm._get_effective_check_interval() == 120
        lm._consecutive_empty_checks = 2
        assert lm._get_effective_check_interval() == 180  # capped at default max (180)

    def test_effective_interval_capped_at_max(self):
        import app.loop_manager as lm
        lm._consecutive_empty_checks = 10
        assert lm._get_effective_check_interval() == lm._GITHUB_MAX_CHECK_INTERVAL

    def test_reset_clears_state(self):
        import app.loop_manager as lm
        lm._consecutive_empty_checks = 5
        lm._last_github_check = 999.0
        lm._last_github_check_iso = "2026-01-01T00:00:00Z"
        lm.reset_github_backoff()
        assert lm._consecutive_empty_checks == 0
        assert lm._last_github_check == 0
        assert lm._last_github_check_iso == ""

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_empty_notifications_increments_backoff(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 0
        assert lm._consecutive_empty_checks == 1

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_found_notifications_resets_backoff(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        lm._consecutive_empty_checks = 3  # simulate previous backoff

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        fake_notif = {"id": "1", "subject": {"url": "https://api.github.com/repos/o/r/issues/1"}}
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([fake_notif], [])), \
             patch("app.github_command_handler.process_single_notification", return_value=(True, None)):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 1
        assert lm._consecutive_empty_checks == 0

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_backoff_throttles_subsequent_checks(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        # First call: succeeds, sets backoff
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
            process_github_notifications(str(tmp_path), str(tmp_path))

        assert lm._consecutive_empty_checks == 1
        # Effective interval is now 120s

        # Second call immediately after: should be throttled (last check was just now)
        result = process_github_notifications(str(tmp_path), str(tmp_path))
        assert result == 0
        # Counter stays at 1 (throttled, didn't actually check)
        assert lm._consecutive_empty_checks == 1

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_consecutive_empty_checks_accumulate(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        # Simulate multiple checks by resetting last_check each time
        for i in range(4):
            lm._last_github_check = 0  # force past throttle
            with patch("app.projects_config.load_projects_config", return_value={}), \
                 patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
                process_github_notifications(str(tmp_path), str(tmp_path))

        assert lm._consecutive_empty_checks == 4
        # After 4 empty: 60 * 2^4 = 960 → capped at 180
        assert lm._get_effective_check_interval() == 180

    def test_config_disabled_does_not_affect_backoff(self, tmp_path):
        import app.loop_manager as lm
        from app.loop_manager import process_github_notifications

        lm._consecutive_empty_checks = 2

        with patch("app.utils.load_config", return_value={}), \
             patch("app.loop_manager._load_github_config", return_value=None):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 0
        # Config disabled = early return, backoff unchanged
        assert lm._consecutive_empty_checks == 2

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_notifications_with_no_missions_still_resets(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """Notifications present but all fail to create missions — still resets backoff."""
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        lm._consecutive_empty_checks = 5

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        fake_notif = {"id": "1", "subject": {"url": "https://api.github.com/repos/o/r/issues/1"}}
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([fake_notif], [])), \
             patch("app.github_command_handler.process_single_notification", return_value=(False, "error")), \
             patch("app.loop_manager._post_error_for_notification"):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 0
        # Notifications were present (non-empty list), so backoff resets
        assert lm._consecutive_empty_checks == 0

    def test_exception_does_not_reset_backoff(self, tmp_path):
        """Exception during check should not touch backoff state."""
        import app.loop_manager as lm
        from app.loop_manager import process_github_notifications

        lm._consecutive_empty_checks = 3

        with patch("app.utils.load_config", side_effect=OSError("boom")):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 0
        assert lm._consecutive_empty_checks == 3

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_since_timestamp_passed_to_fetch(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """process_github_notifications passes _last_github_check_iso as since."""
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        # First call: since should be seeded from max_age (cold start lookback)
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])) as mock_fetch:
            process_github_notifications(str(tmp_path), str(tmp_path))

        # Cold start: since is auto-seeded from max_age_hours, not None
        mock_fetch.assert_called_once()
        _, kwargs = mock_fetch.call_args
        assert kwargs.get("since") is not None, "Cold start should seed since from max_age"

        # After first call, _last_github_check_iso should be set
        assert lm._last_github_check_iso != ""

        # Second call: should pass the timestamp
        saved_iso = lm._last_github_check_iso
        lm._last_github_check = 0  # force past throttle
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])) as mock_fetch:
            process_github_notifications(str(tmp_path), str(tmp_path))

        mock_fetch.assert_called_once()
        _, kwargs = mock_fetch.call_args
        assert kwargs.get("since") == saved_iso

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_cold_start_seeds_since_from_max_age(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """Cold start uses max_age_hours to seed the since parameter."""
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications, reset_github_backoff
        from datetime import datetime, timedelta, timezone

        reset_github_backoff()
        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 2}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])) as mock_fetch:
            process_github_notifications(str(tmp_path), str(tmp_path))

        mock_fetch.assert_called_once()
        _, kwargs = mock_fetch.call_args
        since_str = kwargs.get("since")
        assert since_str is not None

        # Verify the since timestamp is approximately max_age hours ago
        since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        expected = datetime.now(timezone.utc) - timedelta(hours=2)
        # Allow 10 seconds of drift
        assert abs((since_dt - expected).total_seconds()) < 10


# --- Test _drain_notifications ---


class TestDrainNotifications:
    """Test _drain_notifications marks non-actionable notifications as read."""

    @patch("app.github_notifications.mark_notification_read")
    def test_drains_notifications_with_ids(self, mock_mark):
        from app.loop_manager import _drain_notifications

        notifications = [
            {"id": "100", "reason": "ci_activity"},
            {"id": "101", "reason": "review_requested"},
            {"id": "102", "reason": "assign"},
        ]
        result = _drain_notifications(notifications)
        assert result == 3
        assert mock_mark.call_count == 3
        mock_mark.assert_any_call("100")
        mock_mark.assert_any_call("101")
        mock_mark.assert_any_call("102")

    @patch("app.github_notifications.mark_notification_read")
    def test_empty_list_drains_nothing(self, mock_mark):
        from app.loop_manager import _drain_notifications

        result = _drain_notifications([])
        assert result == 0
        mock_mark.assert_not_called()

    @patch("app.github_notifications.mark_notification_read")
    def test_skips_notifications_without_id(self, mock_mark):
        from app.loop_manager import _drain_notifications

        notifications = [
            {"reason": "ci_activity"},  # no id key
            {"id": "", "reason": "assign"},  # empty id
            {"id": "200", "reason": "review_requested"},
        ]
        result = _drain_notifications(notifications)
        # Only the one with a non-empty id should be drained
        assert result == 1
        mock_mark.assert_called_once_with("200")

    @patch("app.github_notifications.mark_notification_read")
    def test_respects_max_drain_per_cycle(self, mock_mark):
        from app.loop_manager import _drain_notifications, _MAX_DRAIN_PER_CYCLE

        # Create more notifications than the max
        notifications = [{"id": str(i), "reason": "ci_activity"} for i in range(50)]
        result = _drain_notifications(notifications)
        assert result == _MAX_DRAIN_PER_CYCLE
        assert mock_mark.call_count == _MAX_DRAIN_PER_CYCLE

    @patch("app.github_notifications.mark_notification_read")
    def test_continues_after_api_failure(self, mock_mark):
        from app.loop_manager import _drain_notifications

        mock_mark.side_effect = [None, Exception("API error"), None]
        notifications = [
            {"id": "100", "reason": "ci_activity"},
            {"id": "101", "reason": "review_requested"},
            {"id": "102", "reason": "assign"},
        ]
        result = _drain_notifications(notifications)
        # Should drain 2 (first and third succeed), skip the failed one
        assert result == 2
        assert mock_mark.call_count == 3

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    @patch("app.github_notifications.mark_notification_read")
    def test_drain_called_in_process_github_notifications(
        self, mock_mark, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """process_github_notifications drains non-actionable notifications."""
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications, reset_github_backoff

        reset_github_backoff()

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        drain_notifs = [
            {"id": "300", "reason": "ci_activity"},
            {"id": "301", "reason": "review_requested"},
        ]
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications",
                   return_value=FetchResult([], drain_notifs)):
            process_github_notifications(str(tmp_path), str(tmp_path))

        # Should have called mark_notification_read for drain notifications
        assert mock_mark.call_count == 2
        mock_mark.assert_any_call("300")
        mock_mark.assert_any_call("301")

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    @patch("app.github_notifications.mark_notification_read")
    def test_drain_happens_alongside_actionable_processing(
        self, mock_mark, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """Both actionable and drain notifications are processed in a single cycle."""
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications, reset_github_backoff

        reset_github_backoff()

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        actionable = [{"id": "1", "subject": {"url": ""}}]
        drain = [{"id": "400", "reason": "ci_activity"}]

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications",
                   return_value=FetchResult(actionable, drain)), \
             patch("app.github_command_handler.process_single_notification", return_value=(True, None)), \
             patch("app.loop_manager._notify_mission_from_mention"):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 1  # 1 actionable processed
        # Drain notification should also be marked as read
        mock_mark.assert_called_once_with("400")


# --- Test _normalize_github_url ---


class TestNormalizeGithubUrl:
    """Test URL normalization for known_repos matching."""

    def test_owner_repo_passthrough(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("sukria/koan") == "sukria/koan"

    def test_full_https_url(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("https://github.com/sukria/koan") == "sukria/koan"

    def test_full_url_with_trailing_slash(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("https://github.com/sukria/koan/") == "sukria/koan"

    def test_full_url_with_git_suffix(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("https://github.com/sukria/koan.git") == "sukria/koan"

    def test_http_url(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("http://github.com/sukria/koan") == "sukria/koan"

    def test_case_insensitive(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("Sukria/Koan") == "sukria/koan"

    def test_full_url_case_insensitive(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("https://github.com/Sukria/Koan") == "sukria/koan"

    def test_owner_repo_with_git_suffix(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("sukria/koan.git") == "sukria/koan"

    def test_whitespace_trimmed(self):
        from app.loop_manager import _normalize_github_url
        assert _normalize_github_url("  sukria/koan  ") == "sukria/koan"


# --- Test _get_known_repos_from_projects ---


class TestGetKnownReposFromProjects:
    """Test known_repos extraction with URL normalization."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    @patch("app.projects_merged.get_all_github_urls_cache", return_value={})
    @patch("app.projects_merged.get_github_url_cache", return_value={})
    @patch("app.projects_config.load_projects_config")
    def test_returns_none_when_no_config(self, mock_load, _url, _all):
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = None
        assert _get_known_repos_from_projects("/tmp") is None

    @patch("app.projects_merged.get_all_github_urls_cache", return_value={})
    @patch("app.projects_merged.get_github_url_cache", return_value={})
    @patch("app.projects_config.load_projects_config")
    def test_returns_none_when_no_github_urls(self, mock_load, _url, _all):
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = {"projects": {"myapp": {"path": "/tmp/myapp"}}}
        assert _get_known_repos_from_projects("/tmp") is None

    @patch("app.projects_merged.get_all_github_urls_cache", return_value={})
    @patch("app.projects_merged.get_github_url_cache", return_value={})
    @patch("app.projects_config.load_projects_config")
    def test_normalizes_owner_repo_format(self, mock_load, _url, _all):
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = {
            "projects": {"koan": {"path": "/tmp/koan", "github_url": "sukria/koan"}}
        }
        repos = _get_known_repos_from_projects("/tmp")
        assert repos == {"sukria/koan"}

    @patch("app.projects_merged.get_all_github_urls_cache", return_value={})
    @patch("app.projects_merged.get_github_url_cache", return_value={})
    @patch("app.projects_config.load_projects_config")
    def test_normalizes_full_url_format(self, mock_load, _url, _all):
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = {
            "projects": {"koan": {"path": "/tmp/koan", "github_url": "https://github.com/sukria/koan"}}
        }
        repos = _get_known_repos_from_projects("/tmp")
        assert repos == {"sukria/koan"}

    @patch("app.projects_merged.get_all_github_urls_cache", return_value={})
    @patch("app.projects_merged.get_github_url_cache", return_value={})
    @patch("app.projects_config.load_projects_config")
    def test_normalizes_mixed_formats(self, mock_load, _url, _all):
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = {
            "projects": {
                "koan": {"path": "/tmp/koan", "github_url": "https://github.com/sukria/koan"},
                "myapp": {"path": "/tmp/myapp", "github_url": "alice/myapp"},
            }
        }
        repos = _get_known_repos_from_projects("/tmp")
        assert repos == {"sukria/koan", "alice/myapp"}

    @patch("app.projects_merged.get_all_github_urls_cache", return_value={})
    @patch("app.projects_merged.get_github_url_cache", return_value={})
    @patch("app.projects_config.load_projects_config")
    def test_lowercase_normalization(self, mock_load, _url, _all):
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = {
            "projects": {"koan": {"path": "/tmp/koan", "github_url": "Sukria/Koan"}}
        }
        repos = _get_known_repos_from_projects("/tmp")
        assert repos == {"sukria/koan"}

    @patch("app.projects_config.load_projects_config")
    @patch("app.projects_merged.get_github_url_cache")
    @patch("app.projects_merged.get_all_github_urls_cache")
    def test_includes_workspace_primary_urls(self, mock_all_cache, mock_url_cache, mock_load):
        """Workspace projects' primary URLs are included in known_repos."""
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = None  # No projects.yaml
        mock_url_cache.return_value = {"rsa": "atoomic/crypt-openssl-rsa"}
        mock_all_cache.return_value = {}
        repos = _get_known_repos_from_projects("/tmp")
        assert repos == {"atoomic/crypt-openssl-rsa"}

    @patch("app.projects_config.load_projects_config")
    @patch("app.projects_merged.get_github_url_cache")
    @patch("app.projects_merged.get_all_github_urls_cache")
    def test_includes_workspace_all_remotes(self, mock_all_cache, mock_url_cache, mock_load):
        """Workspace projects' ALL remote URLs are included (fork + upstream)."""
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = None  # No projects.yaml
        mock_url_cache.return_value = {"rsa": "atoomic/crypt-openssl-rsa"}
        mock_all_cache.return_value = {
            "rsa": ["atoomic/crypt-openssl-rsa", "cpan-authors/crypt-openssl-rsa"]
        }
        repos = _get_known_repos_from_projects("/tmp")
        assert "atoomic/crypt-openssl-rsa" in repos
        assert "cpan-authors/crypt-openssl-rsa" in repos

    @patch("app.projects_config.load_projects_config")
    @patch("app.projects_merged.get_github_url_cache")
    @patch("app.projects_merged.get_all_github_urls_cache")
    def test_merges_yaml_and_workspace_repos(self, mock_all_cache, mock_url_cache, mock_load):
        """Known repos from yaml AND workspace are merged."""
        from app.loop_manager import _get_known_repos_from_projects
        mock_load.return_value = {
            "projects": {"koan": {"path": "/tmp/koan", "github_url": "sukria/koan"}}
        }
        mock_url_cache.return_value = {"rsa": "atoomic/crypt-openssl-rsa"}
        mock_all_cache.return_value = {
            "rsa": ["atoomic/crypt-openssl-rsa", "cpan-authors/crypt-openssl-rsa"]
        }
        repos = _get_known_repos_from_projects("/tmp")
        assert "sukria/koan" in repos
        assert "cpan-authors/crypt-openssl-rsa" in repos


# --- Test _github_log ---


class TestGithubLog:
    """Test console-visible logging helper."""

    def test_prints_with_github_prefix(self, capsys):
        from app.loop_manager import _github_log
        _github_log("test message")
        captured = capsys.readouterr()
        assert "[github] test message" in captured.out

    def test_prints_debug_messages(self, capsys):
        from app.loop_manager import _github_log
        _github_log("debug msg", "debug")
        captured = capsys.readouterr()
        assert "[github] debug msg" in captured.out

    def test_prints_warning_messages(self, capsys):
        from app.loop_manager import _github_log
        _github_log("warn msg", "warning")
        captured = capsys.readouterr()
        assert "[github] warn msg" in captured.out


# --- Test _load_github_config logging ---


class TestLoadGithubConfigLogging:
    """Test that _load_github_config logs configuration state."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_logs_when_commands_disabled(self, capsys):
        from app.loop_manager import _load_github_config
        result = _load_github_config({}, "/tmp", "/tmp/instance")
        assert result is None
        captured = capsys.readouterr()
        assert "[github]" in captured.out
        assert "disabled" in captured.out.lower() or "not set" in captured.out.lower()

    def test_logs_when_nickname_missing(self, capsys):
        from app.loop_manager import _load_github_config
        config = {"github": {"commands_enabled": True}}
        result = _load_github_config(config, "/tmp", "/tmp/instance")
        assert result is None
        captured = capsys.readouterr()
        assert "[github]" in captured.out
        assert "nickname" in captured.out.lower()

    def test_logs_monitoring_on_success(self, capsys):
        from app.loop_manager import _load_github_config
        config = {"github": {"commands_enabled": True, "nickname": "koan-bot"}}
        result = _load_github_config(config, "/tmp", "/tmp/instance")
        assert result is not None
        assert result["nickname"] == "koan-bot"
        captured = capsys.readouterr()
        assert "[github]" in captured.out
        assert "koan-bot" in captured.out

    def test_logs_only_once(self, capsys):
        from app.loop_manager import _load_github_config
        config = {"github": {"commands_enabled": True, "nickname": "koan-bot"}}
        _load_github_config(config, "/tmp", "/tmp/instance")
        _load_github_config(config, "/tmp", "/tmp/instance")
        captured = capsys.readouterr()
        # Count occurrences of the monitoring message
        lines = [l for l in captured.out.strip().split("\n") if "[github]" in l]
        assert len(lines) == 1, f"Expected 1 log line, got {len(lines)}: {lines}"


# --- Test _load_github_config mtime caching ---


class TestLoadGithubConfigCaching:
    """Test that _load_github_config caches results with mtime invalidation."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_returns_cached_result_on_same_mtime(self):
        """Second call with unchanged config.yaml returns cached result."""
        from app.loop_manager import _load_github_config

        config = {"github": {"commands_enabled": True, "nickname": "koan-bot"}}
        # /tmp has no instance/config.yaml so mtime=0 both times
        result1 = _load_github_config(config, "/tmp", "/tmp/instance")
        result2 = _load_github_config(config, "/tmp", "/tmp/instance")
        assert result1 == result2
        assert result1 is result2  # same object = cache hit

    def test_caches_none_when_disabled(self):
        """Disabled config is cached as None (not re-evaluated)."""
        from app.loop_manager import _load_github_config, _GITHUB_CONFIG_UNSET
        import app.loop_manager as lm

        result = _load_github_config({}, "/tmp", "/tmp/instance")
        assert result is None
        assert lm._github_config_cache is None  # None cached, not sentinel
        assert lm._github_config_cache is not _GITHUB_CONFIG_UNSET

    def test_invalidates_cache_on_mtime_change(self, tmp_path):
        """Cache is invalidated when config.yaml mtime changes."""
        from app.loop_manager import _load_github_config
        import app.loop_manager as lm

        # Create config.yaml so we get a real mtime
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        config_file = instance_dir / "config.yaml"
        config_file.write_text("github:\n  commands_enabled: true\n  nickname: bot-v1\n")

        config1 = {"github": {"commands_enabled": True, "nickname": "bot-v1"}}
        result1 = _load_github_config(config1, str(tmp_path), str(instance_dir))
        assert result1["nickname"] == "bot-v1"
        old_mtime = lm._github_config_cache_mtime

        # Touch the file to change mtime
        import time
        time.sleep(0.05)
        config_file.write_text("github:\n  commands_enabled: true\n  nickname: bot-v2\n")

        config2 = {"github": {"commands_enabled": True, "nickname": "bot-v2"}}
        result2 = _load_github_config(config2, str(tmp_path), str(instance_dir))
        assert result2["nickname"] == "bot-v2"
        assert result2 is not result1  # new object = cache miss
        assert lm._github_config_cache_mtime != old_mtime

    def test_reset_clears_cache(self):
        """reset_github_backoff() clears the config cache."""
        from app.loop_manager import _load_github_config, reset_github_backoff, _GITHUB_CONFIG_UNSET
        import app.loop_manager as lm

        config = {"github": {"commands_enabled": True, "nickname": "koan-bot"}}
        _load_github_config(config, "/tmp", "/tmp/instance")
        assert lm._github_config_cache is not _GITHUB_CONFIG_UNSET

        reset_github_backoff()
        assert lm._github_config_cache is _GITHUB_CONFIG_UNSET
        assert lm._github_config_cache_mtime == 0

    def test_cache_survives_across_calls_without_file(self):
        """When config.yaml doesn't exist (mtime=0), cache still works."""
        from app.loop_manager import _load_github_config
        import app.loop_manager as lm

        config = {"github": {"commands_enabled": True, "nickname": "koan-bot"}}
        result1 = _load_github_config(config, "/nonexistent", "/nonexistent/instance")
        result2 = _load_github_config(config, "/nonexistent", "/nonexistent/instance")
        assert result1 is result2


# --- Test process_github_notifications with console output ---


class TestProcessNotificationsConsoleOutput:
    """Test that process_github_notifications produces console-visible output."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_logs_fetched_notifications(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path, capsys
    ):
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"nickname": "bot", "bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        fake_notif = {
            "id": "1",
            "repository": {"full_name": "sukria/koan"},
            "subject": {"url": "https://api.github.com/repos/sukria/koan/issues/1",
                        "title": "Test issue", "type": "Issue"},
            "updated_at": "2026-02-19T12:00:00Z",
        }
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([fake_notif], [])), \
             patch("app.github_command_handler.process_single_notification", return_value=(True, None)), \
             patch("app.loop_manager._notify_mission_from_mention"):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 1
        captured = capsys.readouterr()
        assert "[github]" in captured.out
        assert "Fetched 1" in captured.out
        assert "Mission queued" in captured.out

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_logs_error_notifications(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path, capsys
    ):
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"nickname": "bot", "bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        fake_notif = {
            "id": "2",
            "repository": {"full_name": "sukria/koan"},
            "subject": {"url": "https://api.github.com/repos/sukria/koan/issues/2",
                        "title": "Error issue", "type": "Issue"},
            "updated_at": "2026-02-19T12:00:00Z",
        }
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([fake_notif], [])), \
             patch("app.github_command_handler.process_single_notification", return_value=(False, "Permission denied")), \
             patch("app.loop_manager._post_error_for_notification"):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 0
        captured = capsys.readouterr()
        assert "[github]" in captured.out
        assert "Permission denied" in captured.out


# --- Test fetch_unread_notifications enhanced logging ---


class TestFetchNotificationsLogging:
    """Test improved logging in fetch_unread_notifications."""

    @patch("app.github_notifications.api")
    def test_logs_skipped_reason_summary(self, mock_api, caplog):
        import json
        import logging
        from app.github_notifications import fetch_unread_notifications

        mock_api.return_value = json.dumps([
            {"reason": "ci_activity", "repository": {"full_name": "a/b"}},
            {"reason": "ci_activity", "repository": {"full_name": "c/d"}},
            {"reason": "assign", "repository": {"full_name": "e/f"}},
            {"reason": "mention", "repository": {"full_name": "sukria/koan"}},
        ])

        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            result = fetch_unread_notifications()

        assert len(result.actionable) == 2
        assert "drain-only" in caplog.text
        assert "ci_activity=2" in caplog.text

    @patch("app.github_notifications.api")
    def test_logs_skipped_unknown_repos(self, mock_api, caplog):
        import json
        import logging
        from app.github_notifications import fetch_unread_notifications

        # Use a non-mention reason — mentions bypass the repo filter
        mock_api.return_value = json.dumps([
            {"reason": "comment", "repository": {"full_name": "unknown/repo"}},
        ])

        known = {"sukria/koan"}
        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            result = fetch_unread_notifications(known)

        assert len(result.actionable) == 0
        assert "unknown repos" in caplog.text
        assert "unknown/repo" in caplog.text

    @patch("app.github_notifications.api")
    def test_known_repos_case_insensitive(self, mock_api):
        import json
        from app.github_notifications import fetch_unread_notifications

        mock_api.return_value = json.dumps([
            {"reason": "mention", "repository": {"full_name": "Sukria/Koan"}},
        ])

        known = {"sukria/koan"}  # lowercase
        result = fetch_unread_notifications(known)
        assert len(result.actionable) == 1  # Should match despite case difference

    @patch("app.github_notifications.api")
    def test_logs_api_error(self, mock_api, caplog):
        import logging
        from app.github_notifications import FetchResult, fetch_unread_notifications

        mock_api.side_effect = RuntimeError("connection refused")
        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            result = fetch_unread_notifications()

        assert isinstance(result, FetchResult)
        assert result.actionable == []
        assert result.drain == []
        assert "failed to fetch" in caplog.text

    @patch("app.github_notifications.api")
    def test_logs_empty_response(self, mock_api, caplog):
        import logging
        from app.github_notifications import FetchResult, fetch_unread_notifications

        mock_api.return_value = ""
        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            result = fetch_unread_notifications()

        assert isinstance(result, FetchResult)
        assert result.actionable == []
        assert result.drain == []
        assert "empty response" in caplog.text


# --- Test CLI interface ---


class TestCLI:
    """Test CLI interface."""

    def test_resolve_focus_cli(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "resolve-focus",
             "--mode", "deep"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "deep work" in result.stdout.lower()

    def test_resolve_focus_with_mission(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "resolve-focus",
             "--mode", "review", "--has-mission"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "Execute assigned mission" in result.stdout

    def test_create_pending_cli(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "journal").mkdir()

        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "create-pending",
             "--instance", str(instance),
             "--project-name", "koan",
             "--run-num", "5",
             "--max-runs", "20",
             "--autonomous-mode", "deep",
             "--mission-title", "Test mission"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "pending.md" in result.stdout

        # Verify the file was created
        pending = instance / "journal" / "pending.md"
        assert pending.exists()
        content = pending.read_text()
        assert "# Mission: Test mission" in content

    def test_unknown_subcommand(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "unknown-cmd"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode != 0

    def test_no_subcommand(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode != 0

    def test_interruptible_sleep_cli(self, tmp_path):
        """Test CLI with very short interval — should timeout quickly."""
        koan_root = tmp_path / "root"
        instance = tmp_path / "instance"
        koan_root.mkdir()
        instance.mkdir()

        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "interruptible-sleep",
             "--interval", "1",
             "--koan-root", str(koan_root),
             "--instance", str(instance),
             "--check-interval", "1"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=10,
        )
        assert result.returncode == 0
        assert "timeout" in result.stdout.strip()

    def test_validate_projects_cli(self, tmp_path, monkeypatch):
        """Test validate-projects CLI."""
        proj = tmp_path / "myproj"
        proj.mkdir()
        subprocess.run(["git", "init"], cwd=proj, capture_output=True)
        monkeypatch.setenv("KOAN_PROJECTS", f"myproj:{proj}")

        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "validate-projects"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            env={**os.environ, "KOAN_PROJECTS": f"myproj:{proj}"},
        )
        assert result.returncode == 0
        assert "myproj" in result.stdout

    def test_lookup_project_cli(self, tmp_path):
        """Test lookup-project CLI."""
        proj = tmp_path / "koan"
        proj.mkdir()

        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "lookup-project",
             "--name", "koan"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            env={**os.environ, "KOAN_PROJECTS": f"koan:{proj}"},
        )
        assert result.returncode == 0
        assert str(proj) in result.stdout

    def test_lookup_project_not_found(self, tmp_path):
        """Test lookup-project CLI with unknown project."""
        proj = tmp_path / "koan"
        proj.mkdir()

        result = subprocess.run(
            [sys.executable, "-m", "app.loop_manager", "lookup-project",
             "--name", "unknown"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            env={**os.environ, "KOAN_PROJECTS": f"koan:{proj}"},
        )
        assert result.returncode != 0
        assert "Unknown project" in result.stderr


class TestNotifyMissionFromMention:
    """Tests for _notify_mission_from_mention Telegram notification."""

    @patch("app.notify.send_telegram", return_value=True)
    def test_uses_mailbox_emoji(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        notif = {
            "repository": {"full_name": "sukria/koan"},
            "subject": {"title": "Fix auth bug", "type": "PullRequest"},
        }
        _notify_mission_from_mention(notif)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "📬" in msg
        assert "sukria/koan" in msg
        assert "Fix auth bug" in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_includes_thread_url(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        notif = {
            "repository": {"full_name": "sukria/koan"},
            "subject": {
                "title": "Fix auth bug",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
            },
        }
        _notify_mission_from_mention(notif)
        msg = mock_send.call_args[0][0]
        assert "https://github.com/sukria/koan/pull/42" in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_no_url_when_subject_url_missing(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        notif = {
            "repository": {"full_name": "sukria/koan"},
            "subject": {"title": "Fix auth bug", "type": "PullRequest"},
        }
        _notify_mission_from_mention(notif)
        msg = mock_send.call_args[0][0]
        assert "https://" not in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_includes_command_and_author(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        notif = {
            "repository": {"full_name": "sukria/koan"},
            "subject": {"title": "Fix auth bug", "type": "PullRequest"},
            "_koan_command": "rebase",
            "_koan_author": "atoomic",
        }
        _notify_mission_from_mention(notif)
        msg = mock_send.call_args[0][0]
        assert "@atoomic" in msg
        assert "/rebase" in msg
        assert "mission queued" in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_fallback_when_no_command_or_author(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        notif = {
            "repository": {"full_name": "sukria/koan"},
            "subject": {"title": "Fix auth bug", "type": "PullRequest"},
        }
        _notify_mission_from_mention(notif)
        msg = mock_send.call_args[0][0]
        assert "@mention" in msg
        assert "mission queued" in msg


# --- Test configurable check interval ---


class TestConfigurableCheckInterval:
    """Test that process_github_notifications uses github.check_interval_seconds from config."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_loads_interval_from_config(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """On first call, loads check_interval_seconds from config."""
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        config_with_interval = {"github": {"check_interval_seconds": 90}}
        mock_config.return_value = config_with_interval
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
            process_github_notifications(str(tmp_path), str(tmp_path))

        assert lm._GITHUB_CHECK_INTERVAL == 90
        assert lm._github_interval_loaded is True

    def test_interval_floor_enforced(self):
        """The check interval has a floor of 10 seconds."""
        from app.github_config import get_github_check_interval
        assert get_github_check_interval({"github": {"check_interval_seconds": 3}}) == 10

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_interval_only_loaded_once(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """The interval is loaded from config only on the first call."""
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {"github": {"check_interval_seconds": 120}}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
            process_github_notifications(str(tmp_path), str(tmp_path))

        assert lm._GITHUB_CHECK_INTERVAL == 120

        # Change config — second call should NOT reload
        mock_config.return_value = {"github": {"check_interval_seconds": 30}}
        lm._last_github_check = 0  # force past throttle

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
            process_github_notifications(str(tmp_path), str(tmp_path))

        # Still 120, not 30
        assert lm._GITHUB_CHECK_INTERVAL == 120

    def test_reset_clears_interval_loaded_flag(self):
        """reset_github_backoff clears the interval loaded flag."""
        import app.loop_manager as lm
        lm._github_interval_loaded = True
        lm.reset_github_backoff()
        assert lm._github_interval_loaded is False


class TestConfigurableMaxCheckInterval:
    """Test that max_check_interval_seconds is configurable and loaded from config."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_get_github_max_check_interval_default(self):
        from app.github_config import get_github_max_check_interval
        assert get_github_max_check_interval({}) == 180

    def test_get_github_max_check_interval_custom(self):
        from app.github_config import get_github_max_check_interval
        assert get_github_max_check_interval({"github": {"max_check_interval_seconds": 600}}) == 600

    def test_get_github_max_check_interval_floor(self):
        from app.github_config import get_github_max_check_interval
        assert get_github_max_check_interval({"github": {"max_check_interval_seconds": 5}}) == 30

    def test_get_github_max_check_interval_invalid(self):
        from app.github_config import get_github_max_check_interval
        assert get_github_max_check_interval({"github": {"max_check_interval_seconds": "bad"}}) == 180

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_max_interval_loaded_from_config(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """On first call, loads max_check_interval_seconds from config."""
        import app.loop_manager as lm
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications

        config = {"github": {"max_check_interval_seconds": 600}}
        mock_config.return_value = config
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 24}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=FetchResult([], [])):
            process_github_notifications(str(tmp_path), str(tmp_path))

        assert lm._GITHUB_MAX_CHECK_INTERVAL == 600

    def test_custom_max_caps_backoff(self):
        """A custom max_check_interval_seconds caps the backoff correctly."""
        import app.loop_manager as lm
        lm._GITHUB_MAX_CHECK_INTERVAL = 120
        lm._consecutive_empty_checks = 10
        assert lm._get_effective_check_interval() == 120


class TestBuildSkillRegistryCache:
    """Test module-level caching in _build_skill_registry."""

    def setup_method(self):
        """Reset cache before each test."""
        import app.loop_manager as lm
        lm._gh_cached_registry = None
        lm._gh_cached_extra_dirs = None
        lm._gh_cached_mtime = 0.0

    def teardown_method(self):
        """Reset cache after each test."""
        import app.loop_manager as lm
        lm._gh_cached_registry = None
        lm._gh_cached_extra_dirs = None
        lm._gh_cached_mtime = 0.0

    @patch("app.skills.build_registry")
    def test_caches_registry_across_calls(self, mock_build, tmp_path):
        """Second call reuses the cached registry without rebuilding."""
        from app.loop_manager import _build_skill_registry

        mock_registry = MagicMock()
        mock_build.return_value = mock_registry

        r1 = _build_skill_registry(str(tmp_path))
        r2 = _build_skill_registry(str(tmp_path))

        assert r1 is r2
        assert mock_build.call_count == 1

    @patch("app.skills.build_registry")
    def test_rebuilds_when_extra_dirs_change(self, mock_build, tmp_path):
        """Cache invalidates when instance skills dir appears."""
        from app.loop_manager import _build_skill_registry

        mock_build.side_effect = [MagicMock(), MagicMock()]

        # First call: no instance/skills dir
        r1 = _build_skill_registry(str(tmp_path))

        # Create instance/skills dir — changes extra_dirs tuple
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        r2 = _build_skill_registry(str(tmp_path))

        assert r1 is not r2
        assert mock_build.call_count == 2

    @patch("app.skills.build_registry")
    def test_passes_instance_skills_as_extra_dir(self, mock_build, tmp_path):
        """Instance skills directory is passed to build_registry."""
        from app.loop_manager import _build_skill_registry

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        mock_build.return_value = MagicMock()

        _build_skill_registry(str(tmp_path))

        args = mock_build.call_args[0][0]
        assert len(args) == 1
        assert args[0] == skills_dir

    @patch("app.skills.build_registry")
    def test_rebuilds_when_mtime_changes(self, mock_build, tmp_path):
        """Cache invalidates when skills directory mtime increases."""
        import app.loop_manager as lm
        from app.loop_manager import _build_skill_registry

        mock_registry_1 = MagicMock()
        mock_registry_2 = MagicMock()
        mock_build.side_effect = [mock_registry_1, mock_registry_2]

        # First call builds
        r1 = _build_skill_registry(str(tmp_path))
        assert r1 is mock_registry_1

        # Second call returns cached
        r2 = _build_skill_registry(str(tmp_path))
        assert r2 is mock_registry_1
        assert mock_build.call_count == 1

        # Simulate mtime change by decrementing cached mtime
        lm._gh_cached_mtime -= 1.0

        # Third call detects change and rebuilds
        r3 = _build_skill_registry(str(tmp_path))
        assert r3 is mock_registry_2
        assert mock_build.call_count == 2


# --- Test notification processing cache ---


class TestNotificationCache:
    """Test the 24h notification processing cache in loop_manager."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_uncached_notification_is_not_cached(self):
        from app.loop_manager import _is_notif_cached
        notif = {"id": "100", "updated_at": "2026-03-15T10:00:00Z"}
        assert not _is_notif_cached(notif)

    def test_cached_notification_is_detected(self):
        from app.loop_manager import _is_notif_cached, _cache_notif
        notif = {"id": "100", "updated_at": "2026-03-15T10:00:00Z"}
        _cache_notif(notif)
        assert _is_notif_cached(notif)

    def test_updated_at_change_invalidates_cache(self):
        from app.loop_manager import _is_notif_cached, _cache_notif
        notif = {"id": "100", "updated_at": "2026-03-15T10:00:00Z"}
        _cache_notif(notif)
        # Same thread, updated timestamp — should NOT be cached
        updated_notif = {"id": "100", "updated_at": "2026-03-15T11:00:00Z"}
        assert not _is_notif_cached(updated_notif)

    def test_expired_entry_is_evicted(self):
        import app.loop_manager as lm
        from app.loop_manager import _is_notif_cached, _cache_notif, _notif_cache_lock
        notif = {"id": "100", "updated_at": "2026-03-15T10:00:00Z"}
        _cache_notif(notif)
        # Manually age the entry past TTL
        key = (str(notif["id"]), notif["updated_at"])
        with _notif_cache_lock:
            lm._notif_cache[key] = lm._notif_cache[key] - lm._NOTIF_CACHE_TTL - 1
        assert not _is_notif_cached(notif)

    def test_reset_github_backoff_clears_cache(self):
        from app.loop_manager import _is_notif_cached, _cache_notif, reset_github_backoff
        notif = {"id": "100", "updated_at": "2026-03-15T10:00:00Z"}
        _cache_notif(notif)
        assert _is_notif_cached(notif)
        reset_github_backoff()
        assert not _is_notif_cached(notif)

    def test_cache_eviction_on_overflow(self):
        import app.loop_manager as lm
        from app.loop_manager import _cache_notif, _notif_cache_lock
        # Fill cache beyond max
        original_max = lm._NOTIF_CACHE_MAX
        lm._NOTIF_CACHE_MAX = 5
        try:
            for i in range(7):
                _cache_notif({"id": str(i), "updated_at": f"2026-03-15T{i:02d}:00:00Z"})
            with _notif_cache_lock:
                assert len(lm._notif_cache) <= 5
        finally:
            lm._NOTIF_CACHE_MAX = original_max

    def test_expired_entries_evicted_on_cache_write_below_max(self):
        """Expired entries must be swept on every _cache_notif call, not just
        when cache size exceeds _NOTIF_CACHE_MAX. Otherwise stale entries
        accumulate and block re-appearing notifications."""
        import app.loop_manager as lm
        from app.loop_manager import _cache_notif, _notif_cache_lock
        # Cache a notification, then manually expire it
        old_notif = {"id": "200", "updated_at": "2026-03-15T10:00:00Z"}
        _cache_notif(old_notif)
        key = (str(old_notif["id"]), old_notif["updated_at"])
        with _notif_cache_lock:
            # Age the entry past TTL
            lm._notif_cache[key] = lm._notif_cache[key] - lm._NOTIF_CACHE_TTL - 1
            assert key in lm._notif_cache  # still present before sweep
        # Cache a DIFFERENT notification — this should trigger lazy sweep
        new_notif = {"id": "201", "updated_at": "2026-03-15T11:00:00Z"}
        _cache_notif(new_notif)
        with _notif_cache_lock:
            assert key not in lm._notif_cache, "expired entry should have been swept"

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_cached_notifications_skipped_in_processing(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """Cached notifications are not passed to process_single_notification."""
        from app.github_notifications import FetchResult
        from app.loop_manager import _cache_notif, process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        notif1 = {"id": "1", "updated_at": "2026-03-15T10:00:00Z",
                  "subject": {"url": "https://api.github.com/repos/o/r/issues/1"},
                  "repository": {"full_name": "o/r"}}
        notif2 = {"id": "2", "updated_at": "2026-03-15T10:00:00Z",
                  "subject": {"url": "https://api.github.com/repos/o/r/issues/2"},
                  "repository": {"full_name": "o/r"}}

        # Pre-cache notif1
        _cache_notif(notif1)

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications",
                   return_value=FetchResult([notif1, notif2], [])), \
             patch("app.github_command_handler.process_single_notification",
                   return_value=(True, None)) as mock_process:
            process_github_notifications(str(tmp_path), str(tmp_path))

        # Only notif2 should have been processed
        assert mock_process.call_count == 1
        assert mock_process.call_args[0][0]["id"] == "2"


class TestNotificationCacheIdValidation:
    """Verify that notifications with missing/falsy IDs are not cached."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_missing_id_returns_none_key(self):
        from app.loop_manager import _notif_cache_key
        notif = {"updated_at": "2026-03-20T10:00:00Z"}
        assert _notif_cache_key(notif) is None

    def test_none_id_returns_none_key(self):
        from app.loop_manager import _notif_cache_key
        notif = {"id": None, "updated_at": "2026-03-20T10:00:00Z"}
        assert _notif_cache_key(notif) is None

    def test_empty_string_id_returns_none_key(self):
        from app.loop_manager import _notif_cache_key
        notif = {"id": "", "updated_at": "2026-03-20T10:00:00Z"}
        assert _notif_cache_key(notif) is None

    def test_falsy_zero_id_returns_none_key(self):
        from app.loop_manager import _notif_cache_key
        notif = {"id": 0, "updated_at": "2026-03-20T10:00:00Z"}
        assert _notif_cache_key(notif) is None

    def test_truthy_id_returns_valid_key(self):
        from app.loop_manager import _notif_cache_key
        notif = {"id": "42", "updated_at": "2026-03-20T10:00:00Z"}
        key = _notif_cache_key(notif)
        assert key == ("42", "2026-03-20T10:00:00Z")

    def test_idless_notif_is_never_cached(self):
        from app.loop_manager import _is_notif_cached, _cache_notif
        notif = {"updated_at": "2026-03-20T10:00:00Z"}
        _cache_notif(notif)
        assert not _is_notif_cached(notif)

    def test_idless_notifs_dont_collide(self):
        """Two ID-less notifications with different updated_at must not
        deduplicate against each other."""
        from app.loop_manager import _is_notif_cached, _cache_notif
        notif_a = {"updated_at": "2026-03-20T10:00:00Z",
                   "subject": {"title": "A"}}
        notif_b = {"updated_at": "2026-03-20T11:00:00Z",
                   "subject": {"title": "B"}}
        _cache_notif(notif_a)
        _cache_notif(notif_b)
        # Neither should be considered cached — both pass through
        assert not _is_notif_cached(notif_a)
        assert not _is_notif_cached(notif_b)

    def test_warning_logged_for_missing_id(self, caplog):
        import logging
        from app.loop_manager import _notif_cache_key
        notif = {"subject": {"title": "Test PR"},
                 "updated_at": "2026-03-20T10:00:00Z"}
        with caplog.at_level(logging.WARNING, logger="app.loop_manager"):
            result = _notif_cache_key(notif)
        assert result is None
        assert "missing 'id'" in caplog.text
        assert "Test PR" in caplog.text

    def test_cache_notif_survives_full_ttl_eviction(self):
        """min() on an empty _notif_cache must not raise ValueError.

        If _NOTIF_CACHE_TTL is pathologically low (or all entries are
        somehow aged past TTL), the sweep can empty the cache entirely.
        Without a guard, the subsequent min(_notif_cache, ...) crashes.
        """
        import app.loop_manager as lm
        from app.loop_manager import _cache_notif, _notif_cache_lock

        original_max = lm._NOTIF_CACHE_MAX
        original_ttl = lm._NOTIF_CACHE_TTL
        # TTL of -1 means every entry (including just-added) is "expired"
        lm._NOTIF_CACHE_TTL = -1
        # Max of -1 means len(cache) > max is True even when cache is empty (0 > -1)
        lm._NOTIF_CACHE_MAX = -1
        try:
            # Without the guard, this raises ValueError: min() arg is empty sequence
            _cache_notif({"id": "901", "updated_at": "2026-03-20T11:00:00Z"})
            # Cache should be empty — everything was evicted by TTL sweep
            with _notif_cache_lock:
                assert len(lm._notif_cache) == 0
        finally:
            lm._NOTIF_CACHE_MAX = original_max
            lm._NOTIF_CACHE_TTL = original_ttl


class TestErrorReplyRetryQueue:
    """Test the failed error reply queue and retry logic."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def test_failed_reply_queued_for_retry(self):
        """When post_error_reply fails, the reply is queued for retry."""
        import app.loop_manager as lm

        notif = {
            "id": "1",
            "updated_at": "2026-03-20T10:00:00Z",
            "subject": {"url": "https://api.github.com/repos/o/r/issues/1"},
            "repository": {"full_name": "o/r"},
        }

        with patch("app.github_command_handler.resolve_project_from_notification",
                    return_value=("proj", "o", "r")), \
             patch("app.github_command_handler.extract_issue_number_from_notification",
                    return_value=42), \
             patch("app.github_notifications.get_comment_from_notification",
                    return_value={"id": 999, "url": "https://api.github.com/comment/999"}), \
             patch("app.github_command_handler.post_error_reply",
                    side_effect=OSError("network error")):
            lm._post_error_for_notification(notif, "some error")

        with lm._pending_error_replies_lock:
            assert len(lm._pending_error_replies) == 1
            entry = lm._pending_error_replies[0]
            assert entry["owner"] == "o"
            assert entry["repo"] == "r"
            assert entry["issue_num"] == 42
            assert entry["comment_id"] == "999"
            assert entry["attempts"] == 1

    def test_error_reply_queued_when_get_comment_raises(self):
        """Regression: NameError when get_comment_from_notification raises.

        If get_comment_from_notification raises an OSError (or similar),
        comment_id and comment_api_url were unbound, causing NameError in
        the except block. The fix initializes them before the try block.
        """
        import app.loop_manager as lm

        notif = {
            "id": "2",
            "updated_at": "2026-03-27T10:00:00Z",
            "subject": {"url": "https://api.github.com/repos/o/r/issues/5"},
            "repository": {"full_name": "o/r"},
        }

        with lm._pending_error_replies_lock:
            lm._pending_error_replies.clear()

        with patch("app.github_command_handler.resolve_project_from_notification",
                    return_value=("proj", "o", "r")), \
             patch("app.github_command_handler.extract_issue_number_from_notification",
                    return_value=5), \
             patch("app.github_notifications.get_comment_from_notification",
                    side_effect=OSError("network timeout")):
            # Before the fix, this raised NameError: name 'comment_id' is not defined
            lm._post_error_for_notification(notif, "dispatch failed")

        with lm._pending_error_replies_lock:
            assert len(lm._pending_error_replies) == 1
            entry = lm._pending_error_replies[0]
            assert entry["comment_id"] == ""
            assert entry["comment_api_url"] == ""
            assert entry["error"] == "dispatch failed"

    def test_retry_succeeds_clears_queue(self):
        """Successful retry removes entry from the queue."""
        import app.loop_manager as lm

        entry = {
            "owner": "o", "repo": "r", "issue_num": 42,
            "comment_id": "999", "error": "some error",
            "comment_api_url": "", "attempts": 1,
        }
        with lm._pending_error_replies_lock:
            lm._pending_error_replies.append(entry)

        with patch("app.github_command_handler.post_error_reply") as mock_reply:
            lm._retry_failed_replies()
            mock_reply.assert_called_once()

        with lm._pending_error_replies_lock:
            assert len(lm._pending_error_replies) == 0

    def test_retry_failure_requeues_with_incremented_attempts(self):
        """Failed retry re-queues with attempts incremented."""
        import app.loop_manager as lm

        entry = {
            "owner": "o", "repo": "r", "issue_num": 42,
            "comment_id": "999", "error": "some error",
            "comment_api_url": "", "attempts": 1,
        }
        with lm._pending_error_replies_lock:
            lm._pending_error_replies.append(entry)

        with patch("app.github_command_handler.post_error_reply",
                    side_effect=OSError("still broken")):
            lm._retry_failed_replies()

        with lm._pending_error_replies_lock:
            assert len(lm._pending_error_replies) == 1
            assert lm._pending_error_replies[0]["attempts"] == 2

    def test_max_retries_drops_entry(self):
        """After _MAX_REPLY_RETRIES attempts, the entry is dropped."""
        import app.loop_manager as lm

        entry = {
            "owner": "o", "repo": "r", "issue_num": 42,
            "comment_id": "999", "error": "some error",
            "comment_api_url": "", "attempts": lm._MAX_REPLY_RETRIES,
        }
        with lm._pending_error_replies_lock:
            lm._pending_error_replies.append(entry)

        with patch("app.github_command_handler.post_error_reply",
                    side_effect=OSError("permanent failure")):
            lm._retry_failed_replies()

        with lm._pending_error_replies_lock:
            assert len(lm._pending_error_replies) == 0

    def test_reset_clears_retry_queue(self):
        """reset_github_backoff clears the retry queue."""
        import app.loop_manager as lm

        with lm._pending_error_replies_lock:
            lm._pending_error_replies.append({"owner": "o", "repo": "r"})

        lm.reset_github_backoff()

        with lm._pending_error_replies_lock:
            assert len(lm._pending_error_replies) == 0

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_cache_written_before_error_reply(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path
    ):
        """Cache must be written before the error reply attempt so that a
        reply failure doesn't cause re-processing of the whole notification."""
        from app.github_notifications import FetchResult
        from app.loop_manager import process_github_notifications, _is_notif_cached

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        notif = {
            "id": "1", "updated_at": "2026-03-20T10:00:00Z",
            "subject": {"url": "https://api.github.com/repos/o/r/issues/1"},
            "repository": {"full_name": "o/r"},
        }

        call_order = []

        def mock_process(*a, **kw):
            return (False, "some error")

        def mock_post_error(n, e):
            # At the point when error reply is attempted, cache should exist
            call_order.append(("post_error", _is_notif_cached(notif)))

        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications",
                   return_value=FetchResult([notif], [])), \
             patch("app.github_command_handler.process_single_notification",
                   side_effect=mock_process), \
             patch("app.loop_manager._post_error_for_notification",
                   side_effect=mock_post_error):
            process_github_notifications(str(tmp_path), str(tmp_path))

        assert call_order == [("post_error", True)], \
            "Notification should be cached before error reply attempt"


# --- Thread-safety tests ---


class TestThreadSafety:
    """Verify that module-level mutable state is protected by _github_state_lock."""

    def test_concurrent_reset_and_check_interval(self):
        """reset_github_backoff and _get_effective_check_interval don't race."""
        import threading
        from app.loop_manager import (
            _get_effective_check_interval,
            reset_github_backoff,
        )

        errors = []

        def reset_loop():
            try:
                for _ in range(200):
                    reset_github_backoff()
            except Exception as exc:
                errors.append(exc)

        def read_loop():
            try:
                for _ in range(200):
                    val = _get_effective_check_interval()
                    assert isinstance(val, int)
                    assert val > 0
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reset_loop) for _ in range(3)]
        threads += [threading.Thread(target=read_loop) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread-safety errors: {errors}"

    @patch("app.skills.build_registry")
    def test_concurrent_build_skill_registry(self, mock_build, tmp_path):
        """_build_skill_registry handles concurrent calls without corruption."""
        import threading
        from app.loop_manager import _build_skill_registry, _gh_cached_registry

        import app.loop_manager as lm
        lm._gh_cached_registry = None
        lm._gh_cached_extra_dirs = None

        mock_build.return_value = MagicMock()
        errors = []

        def build_loop():
            try:
                for _ in range(50):
                    result = _build_skill_registry(str(tmp_path))
                    assert result is not None
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=build_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread-safety errors: {errors}"


# ---------------------------------------------------------------------------
# SSO alert detection (_check_sso_failures)
# ---------------------------------------------------------------------------

class TestCheckSSOFailures:
    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        from app.github_notifications import reset_sso_failure_count, reset_consecutive_sso_state
        reset_github_backoff()
        reset_sso_failure_count()
        reset_consecutive_sso_state()

    def teardown_method(self):
        from app.loop_manager import reset_github_backoff
        from app.github_notifications import reset_sso_failure_count, reset_consecutive_sso_state
        reset_github_backoff()
        reset_sso_failure_count()
        reset_consecutive_sso_state()

    @patch("app.loop_manager._github_log")
    def test_no_alert_when_no_sso_failures(self, mock_log):
        from app.loop_manager import _check_sso_failures
        _check_sso_failures()
        # Should not log any warning
        mock_log.assert_not_called()

    def test_logs_warning_on_sso_failure(self):
        from app.loop_manager import _check_sso_failures
        from app.github_notifications import _record_sso_failure
        _record_sso_failure("test")

        with patch("app.loop_manager._github_log") as mock_log:
            _check_sso_failures()
            mock_log.assert_called_once()
            msg = mock_log.call_args[0][0]
            assert "SSO" in msg

    def test_escalation_after_threshold(self, monkeypatch):
        """After enough consecutive failures, check_sso_escalation fires."""
        from app.loop_manager import _check_sso_failures
        from app.github_notifications import (
            _record_sso_failure, reset_sso_failure_count,
            SSO_ESCALATION_THRESHOLD,
        )
        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")

        with patch("app.utils.append_to_outbox") as mock_outbox:
            # Run enough cycles with failures to reach threshold
            for i in range(SSO_ESCALATION_THRESHOLD):
                reset_sso_failure_count()
                _record_sso_failure(f"test-{i}")
                _check_sso_failures()

            assert mock_outbox.call_count == 1
            msg = mock_outbox.call_args[0][1]
            assert "SSO" in msg

    def test_no_escalation_below_threshold(self, monkeypatch):
        from app.loop_manager import _check_sso_failures
        from app.github_notifications import _record_sso_failure, reset_sso_failure_count
        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")

        with patch("app.utils.append_to_outbox") as mock_outbox:
            # Only 2 cycles with failures — below threshold
            for i in range(2):
                reset_sso_failure_count()
                _record_sso_failure(f"test-{i}")
                _check_sso_failures()

            mock_outbox.assert_not_called()
