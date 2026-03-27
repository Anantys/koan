"""Tests for restart_manager.py — file-based restart signaling."""

import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.restart_manager import (
    RESTART_FILE,
    RESTART_EXIT_CODE,
    request_restart,
    check_restart,
    clear_restart,
    reexec_bridge,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_restart_file_name(self):
        assert RESTART_FILE == ".koan-restart"

    def test_restart_exit_code(self):
        assert RESTART_EXIT_CODE == 42


# ---------------------------------------------------------------------------
# request_restart
# ---------------------------------------------------------------------------


class TestRequestRestart:
    def test_creates_file(self, tmp_path):
        request_restart(str(tmp_path))
        restart_file = tmp_path / RESTART_FILE
        assert restart_file.exists()

    def test_file_contains_timestamp(self, tmp_path):
        request_restart(str(tmp_path))
        content = (tmp_path / RESTART_FILE).read_text()
        assert "restart requested at" in content
        assert ":" in content  # Time format HH:MM:SS

    def test_overwrites_existing_file(self, tmp_path):
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("old content")
        request_restart(str(tmp_path))
        content = restart_file.read_text()
        assert "old content" not in content
        assert "restart requested at" in content

    def test_uses_atomic_write(self, tmp_path):
        """request_restart should use atomic_write for thread safety."""
        with patch("app.utils.atomic_write") as mock_aw:
            request_restart(str(tmp_path))
            mock_aw.assert_called_once()
            # First arg should be a Path
            call_path = mock_aw.call_args[0][0]
            assert str(call_path).endswith(RESTART_FILE)


# ---------------------------------------------------------------------------
# check_restart
# ---------------------------------------------------------------------------


class TestCheckRestart:
    def test_returns_false_when_no_file(self, tmp_path):
        assert check_restart(str(tmp_path)) is False

    def test_returns_true_when_file_exists(self, tmp_path):
        (tmp_path / RESTART_FILE).write_text("restart")
        assert check_restart(str(tmp_path)) is True

    def test_respects_since_parameter_newer(self, tmp_path):
        """File modified after 'since' should return True."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        # File was just created, so mtime is recent
        old_time = time.time() - 60  # 1 minute ago
        assert check_restart(str(tmp_path), since=old_time) is True

    def test_respects_since_parameter_older(self, tmp_path):
        """File modified before 'since' should return False (stale signal)."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        # Set file mtime to 5 seconds ago
        old_mtime = time.time() - 5
        os.utime(restart_file, (old_mtime, old_mtime))
        # Check with 'since' = 2 seconds ago (more recent than file)
        since_time = time.time() - 2
        assert check_restart(str(tmp_path), since=since_time) is False

    def test_since_zero_ignores_mtime(self, tmp_path):
        """When since=0, mtime is not checked."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        # Even with old mtime, since=0 should return True
        old_mtime = time.time() - 3600  # 1 hour ago
        os.utime(restart_file, (old_mtime, old_mtime))
        assert check_restart(str(tmp_path), since=0) is True

    def test_since_exact_boundary(self, tmp_path):
        """File with mtime == since should return False (not strictly after)."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        mtime = restart_file.stat().st_mtime
        # since == mtime means file was NOT modified AFTER since
        assert check_restart(str(tmp_path), since=mtime) is False

    def test_handles_oserror_on_stat(self, tmp_path):
        """check_restart returns False if stat() raises OSError."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        with patch("app.restart_manager.os.path.getmtime", side_effect=OSError):
            assert check_restart(str(tmp_path), since=1.0) is False


# ---------------------------------------------------------------------------
# clear_restart
# ---------------------------------------------------------------------------


class TestClearRestart:
    def test_removes_existing_file(self, tmp_path):
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        clear_restart(str(tmp_path))
        assert not restart_file.exists()

    def test_no_error_when_file_missing(self, tmp_path):
        # Should not raise even if file doesn't exist
        clear_restart(str(tmp_path))
        assert not (tmp_path / RESTART_FILE).exists()

    def test_idempotent(self, tmp_path):
        """Multiple clears should be safe."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("restart")
        clear_restart(str(tmp_path))
        clear_restart(str(tmp_path))
        clear_restart(str(tmp_path))
        assert not restart_file.exists()


# ---------------------------------------------------------------------------
# reexec_bridge
# ---------------------------------------------------------------------------


class TestReexecBridge:
    def test_calls_execv_with_correct_args(self):
        """reexec_bridge should call os.execv with sys.executable and sys.argv."""
        mock_execv = MagicMock()
        mock_argv = ["bridge.py", "--some-arg"]
        mock_executable = "/usr/bin/python3"

        with patch("app.restart_manager.os.execv", mock_execv), \
             patch("app.restart_manager.sys.argv", mock_argv), \
             patch("app.restart_manager.sys.executable", mock_executable):
            reexec_bridge()

        mock_execv.assert_called_once_with(
            "/usr/bin/python3",
            ["/usr/bin/python3", "bridge.py", "--some-arg"]
        )

    def test_preserves_all_argv(self):
        """All command line arguments should be passed to the new process."""
        mock_execv = MagicMock()
        mock_argv = ["script.py", "-v", "--config", "/path/to/config.yaml", "extra"]

        with patch("app.restart_manager.os.execv", mock_execv), \
             patch("app.restart_manager.sys.argv", mock_argv), \
             patch("app.restart_manager.sys.executable", "/python"):
            reexec_bridge()

        args = mock_execv.call_args[0][1]
        assert args == ["/python", "script.py", "-v", "--config", "/path/to/config.yaml", "extra"]


# ---------------------------------------------------------------------------
# Integration scenarios
# ---------------------------------------------------------------------------


class TestRestartWorkflow:
    def test_full_restart_cycle(self, tmp_path):
        """Test the complete request → check → clear cycle."""
        root = str(tmp_path)
        # Initially no restart pending
        assert check_restart(root) is False

        # Request restart
        request_restart(root)
        assert check_restart(root) is True

        # Clear it
        clear_restart(root)
        assert check_restart(root) is False

    def test_stale_signal_ignored(self, tmp_path):
        """Stale restart signals from previous incarnation should be ignored."""
        root = str(tmp_path)
        # Create a restart signal
        request_restart(root)
        restart_file = tmp_path / RESTART_FILE

        # Backdate the file to simulate stale signal
        old_mtime = time.time() - 300  # 5 minutes ago
        os.utime(restart_file, (old_mtime, old_mtime))

        # Process startup time is "now"
        startup_time = time.time()

        # Stale signal should be ignored
        assert check_restart(root, since=startup_time) is False

        # But a fresh request should work
        request_restart(root)
        # Ensure the fresh file's mtime is strictly after startup_time.
        # On fast CI, write + time.time() can land in the same tick,
        # so explicitly forward-date the file by 1 second.
        future_mtime = startup_time + 1
        os.utime(restart_file, (future_mtime, future_mtime))
        assert check_restart(root, since=startup_time) is True

    def test_accepts_str_not_path(self, tmp_path):
        """All functions should accept str, not Path objects."""
        root = str(tmp_path)
        request_restart(root)
        assert check_restart(root) is True
        clear_restart(root)
        assert check_restart(root) is False
