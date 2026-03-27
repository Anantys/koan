"""Tests for heartbeat.py — stale mission detection and disk space monitoring."""

import time
from unittest.mock import patch

import pytest

from app.heartbeat import (
    check_stale_missions,
    run_stale_mission_check,
    reset_stale_state,
    check_disk_space,
    get_disk_free_gb,
    run_disk_space_check,
    reset_disk_state,
    _get_last_journal_activity,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state before each test."""
    reset_stale_state()
    reset_disk_state()
    yield
    reset_stale_state()
    reset_disk_state()


def _create_missions_file(instance_dir, content):
    """Helper to create missions.md with given content."""
    missions_path = instance_dir / "missions.md"
    missions_path.write_text(content)


def _create_journal_file(instance_dir, project_name, age_seconds=0):
    """Helper to create a journal file with a specific mtime."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    journal_dir = instance_dir / "journal" / today
    journal_dir.mkdir(parents=True, exist_ok=True)
    f = journal_dir / f"{project_name}.md"
    f.write_text("# Journal entry\n")
    import os
    mtime = time.time() - age_seconds
    os.utime(f, (mtime, mtime))
    return f


class TestCheckStaleMissions:

    def test_no_missions_file(self, tmp_path):
        assert check_stale_missions(str(tmp_path)) == []

    def test_no_in_progress(self, tmp_path):
        _create_missions_file(tmp_path, "## Pending\n\n## In Progress\n\n## Done\n")
        assert check_stale_missions(str(tmp_path)) == []

    def test_fresh_mission_not_stale(self, tmp_path):
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix the bug [project:myapp]\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=60)
        assert check_stale_missions(str(tmp_path)) == []

    def test_stale_mission_detected(self, tmp_path):
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix the bug [project:myapp]\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=3 * 3600)
        result = check_stale_missions(str(tmp_path), max_age_hours=2)
        assert len(result) == 1
        assert "Fix the bug" in result[0]

    def test_complex_mission_skipped(self, tmp_path):
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "### Big refactoring [project:myapp]\n"
            "- Step 1\n"
            "- Step 2\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=10 * 3600)
        assert check_stale_missions(str(tmp_path), max_age_hours=2) == []

    def test_alerts_only_once(self, tmp_path):
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix the bug [project:myapp]\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=3 * 3600)
        result1 = check_stale_missions(str(tmp_path), max_age_hours=2)
        assert len(result1) == 1
        result2 = check_stale_missions(str(tmp_path), max_age_hours=2)
        assert len(result2) == 0

    def test_multiple_stale_missions(self, tmp_path):
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix bug A [project:myapp]\n"
            "- Fix bug B [project:myapp]\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=3 * 3600)
        result = check_stale_missions(str(tmp_path), max_age_hours=2)
        assert len(result) == 2

    def test_no_journal_files_not_flagged(self, tmp_path):
        """Missions with no journal at all are not flagged as stale."""
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix the bug [project:myapp]\n\n"
            "## Done\n"
        ))
        # No journal directory at all
        assert check_stale_missions(str(tmp_path), max_age_hours=2) == []


class TestGetLastJournalActivity:

    def test_no_journal_dir(self, tmp_path):
        assert _get_last_journal_activity(str(tmp_path)) == -1

    def test_pending_md_mtime(self, tmp_path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("# In progress\n")
        result = _get_last_journal_activity(str(tmp_path))
        assert result > 0
        assert abs(result - time.time()) < 2

    def test_project_specific_journal(self, tmp_path):
        _create_journal_file(tmp_path, "myapp", age_seconds=60)
        result = _get_last_journal_activity(str(tmp_path), project_name="myapp")
        assert result > 0
        assert abs(result - (time.time() - 60)) < 2


class TestRunStaleMissionCheck:

    @patch("app.heartbeat._send_stale_alert")
    def test_throttled(self, mock_alert, tmp_path):
        """Second call within STALE_CHECK_INTERVAL returns empty."""
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix the bug [project:myapp]\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=3 * 3600)

        result1 = run_stale_mission_check(str(tmp_path))
        assert len(result1) == 1
        result2 = run_stale_mission_check(str(tmp_path))
        assert len(result2) == 0

    @patch("app.notify.send_telegram")
    def test_sends_alert(self, mock_send, tmp_path):
        _create_missions_file(tmp_path, (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix the bug [project:myapp]\n\n"
            "## Done\n"
        ))
        _create_journal_file(tmp_path, "myapp", age_seconds=3 * 3600)

        run_stale_mission_check(str(tmp_path))
        mock_send.assert_called_once()
        assert "stale" in mock_send.call_args[0][0]


class TestCheckDiskSpace:

    def test_sufficient_space(self, tmp_path):
        assert check_disk_space(str(tmp_path)) is True

    @patch("app.heartbeat.shutil.disk_usage")
    def test_low_space(self, mock_usage, tmp_path):
        # Simulate 500 MB free
        mock_usage.return_value = type("Usage", (), {
            "total": 100 * 1024**3,
            "used": 99.5 * 1024**3,
            "free": 0.5 * 1024**3,
        })()
        assert check_disk_space(str(tmp_path), warn_threshold_gb=1.0) is False

    @patch("app.heartbeat.shutil.disk_usage")
    def test_os_error(self, mock_usage, tmp_path):
        mock_usage.side_effect = OSError("Permission denied")
        assert check_disk_space(str(tmp_path)) is True

    def test_get_free_gb(self, tmp_path):
        result = get_disk_free_gb(str(tmp_path))
        assert result > 0


class TestRunDiskSpaceCheck:

    @patch("app.notify.send_telegram")
    @patch("app.heartbeat.shutil.disk_usage")
    def test_low_space_alerts_once(self, mock_usage, mock_send, tmp_path):
        mock_usage.return_value = type("Usage", (), {
            "total": 100 * 1024**3,
            "used": 99.5 * 1024**3,
            "free": 0.5 * 1024**3,
        })()
        result1 = run_disk_space_check(str(tmp_path))
        assert result1 is False
        mock_send.assert_called_once()
        assert "Low disk space" in mock_send.call_args[0][0]

        # Second call should not alert again
        mock_send.reset_mock()
        result2 = run_disk_space_check(str(tmp_path))
        assert result2 is True  # Already alerted
        mock_send.assert_not_called()

    @patch("app.notify.send_telegram")
    def test_sufficient_space_no_alert(self, mock_send, tmp_path):
        result = run_disk_space_check(str(tmp_path))
        assert result is True
        mock_send.assert_not_called()
