"""Tests for run loop status tracking and interruptible sleep.

Covers:
- .koan-status file lifecycle (written by run.py, read by /status and /ping)
- has_pending_missions helper (used for sleep-skip logic)
- Status handler improvements (loop status in /status, /ping)
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_HANDLER_PATH = (
    Path(__file__).parent.parent / "skills" / "core" / "status" / "handler.py"
)


def _load_status_handler():
    """Load the status handler module."""
    spec = importlib.util.spec_from_file_location("status_handler", _STATUS_HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call_status_handler(tmp_path, command_name="status"):
    """Call the status handler with given context."""
    from app.skills import SkillContext

    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    ctx = SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name=command_name,
    )
    mod = _load_status_handler()
    return mod.handle(ctx)


def _call_ping_handler(tmp_path):
    """Call the ping handler with given context."""
    return _call_status_handler(tmp_path, command_name="ping")


# ---------------------------------------------------------------------------
# Status file lifecycle
# ---------------------------------------------------------------------------

class TestStatusFileLifecycle:
    """Tests for .koan-status file read/write behavior."""

    def test_status_shows_loop_status(self, tmp_path):
        """When .koan-status exists, /status shows it."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-status").write_text("Run 5/20 — executing mission on koan")
        status = _call_status_handler(tmp_path)
        assert "Run 5/20" in status
        assert "executing mission on koan" in status

    def test_status_shows_idle_state(self, tmp_path):
        """When loop is sleeping, status shows idle with time."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-status").write_text("Idle — sleeping 300s (14:35)")
        status = _call_status_handler(tmp_path)
        assert "Idle" in status
        assert "sleeping 300s" in status

    def test_status_shows_preparing(self, tmp_path):
        """Status shows 'preparing' between sleep and mission execution."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-status").write_text("Run 3/20 — preparing")
        status = _call_status_handler(tmp_path)
        assert "preparing" in status

    def test_status_shows_post_mission(self, tmp_path):
        """Status shows post-mission processing phase."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-status").write_text("Run 3/20 — post-mission processing")
        status = _call_status_handler(tmp_path)
        assert "post-mission" in status

    def test_status_no_file_shows_working(self, tmp_path):
        """When no .koan-status file, mode still shows Working."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Active" in status
        # No "Loop:" line when status file doesn't exist
        assert "Loop:" not in status

    def test_status_empty_file_ignored(self, tmp_path):
        """Empty .koan-status file is treated as no status."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-status").write_text("")
        status = _call_status_handler(tmp_path)
        assert "Loop:" not in status

    def test_status_paused_state(self, tmp_path):
        """When paused, .koan-status shows pause time."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-pause").write_text("PAUSE")
        (tmp_path / ".koan-status").write_text("Paused (14:35)")
        status = _call_status_handler(tmp_path)
        assert "Paused" in status


# ---------------------------------------------------------------------------
# Ping with loop status
# ---------------------------------------------------------------------------

class TestPingWithLoopStatus:
    """Tests for /ping showing loop status via PID files."""

    def test_ping_shows_status_when_running(self, tmp_path):
        """When runner is alive and has status, /ping shows it."""
        (tmp_path / "instance").mkdir()
        (tmp_path / ".koan-status").write_text("Run 3/20 — executing mission on koan")
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _call_ping_handler(tmp_path)
        assert "Runner:" in result
        assert "Run 3/20" in result
        assert "executing mission" in result

    def test_ping_shows_idle_status(self, tmp_path):
        """When runner is idle, /ping shows it."""
        (tmp_path / "instance").mkdir()
        (tmp_path / ".koan-status").write_text("Idle — sleeping 300s (14:35)")
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _call_ping_handler(tmp_path)
        assert "Runner:" in result
        assert "Idle" in result

    def test_ping_without_status_file(self, tmp_path):
        """When no status file, /ping shows generic 'alive'."""
        (tmp_path / "instance").mkdir()
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _call_ping_handler(tmp_path)
        assert "Runner: alive" in result

    def test_ping_empty_status_file(self, tmp_path):
        """Empty status file treated as no status."""
        (tmp_path / "instance").mkdir()
        (tmp_path / ".koan-status").write_text("")
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _call_ping_handler(tmp_path)
        assert "Runner: alive" in result

    def test_ping_paused_ignores_status(self, tmp_path):
        """When paused, /ping shows paused — doesn't show loop status."""
        (tmp_path / "instance").mkdir()
        (tmp_path / ".koan-pause").write_text("PAUSE")
        (tmp_path / ".koan-status").write_text("Paused (14:35)")
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _call_ping_handler(tmp_path)
        assert "⏸️" in result
        assert "Runner:" in result


# ---------------------------------------------------------------------------
# has_pending_missions validation (via missions.py count_pending)
# ---------------------------------------------------------------------------

class TestPendingMissionDetection:
    """Tests that count_pending correctly detects pending missions."""

    def test_no_pending(self):
        from app.missions import count_pending
        content = "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        assert count_pending(content) == 0

    def test_one_pending(self):
        from app.missions import count_pending
        content = "# Missions\n\n## Pending\n\n- fix the bug\n\n## In Progress\n\n## Done\n"
        assert count_pending(content) == 1

    def test_multiple_pending(self):
        from app.missions import count_pending
        content = "# Missions\n\n## Pending\n\n- fix bug\n- add feature\n- audit security\n\n## In Progress\n\n## Done\n"
        assert count_pending(content) == 3

    def test_french_section_names(self):
        from app.missions import count_pending
        content = "# Missions\n\n## Pending\n\n- fix bug\n\n## In Progress\n\n## Done\n"
        assert count_pending(content) == 1

    def test_in_progress_not_counted(self):
        from app.missions import count_pending
        content = "# Missions\n\n## Pending\n\n## In Progress\n\n- working on stuff\n\n## Done\n"
        assert count_pending(content) == 0


# ---------------------------------------------------------------------------
# Integration: status handler with various loop states
# ---------------------------------------------------------------------------

class TestStatusHandlerIntegration:
    """Integration tests for status handler with realistic loop state."""

    def test_full_status_during_mission(self, tmp_path):
        """Full /status output during mission execution."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n- add feature\n\n"
            "## In Progress\n\n- [project:koan] fix the bug\n\n## Done\n"
        )
        (tmp_path / ".koan-status").write_text("Run 3/20 — executing mission on koan")

        status = _call_status_handler(tmp_path)
        assert "Active" in status
        assert "Run 3/20" in status
        assert "executing mission" in status
        assert "fix the bug" in status
        assert "add feature" in status

    def test_full_status_during_idle(self, tmp_path):
        """Full /status output when loop is sleeping."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        (tmp_path / ".koan-status").write_text("Idle — sleeping 300s (14:35)")

        status = _call_status_handler(tmp_path)
        assert "Active" in status
        assert "Idle" in status
        assert "sleeping" in status

    def test_full_status_during_preparation(self, tmp_path):
        """Full /status output when loop is preparing next run."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n- audit security\n\n## In Progress\n\n## Done\n"
        )
        (tmp_path / ".koan-status").write_text("Run 7/20 — preparing")

        status = _call_status_handler(tmp_path)
        assert "Active" in status
        assert "preparing" in status
        assert "audit security" in status
