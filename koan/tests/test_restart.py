"""Tests for the restart_manager module and /restart as /update alias."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.restart_manager import (
    request_restart,
    check_restart,
    clear_restart,
    reexec_bridge,
    RESTART_FILE,
    RESTART_EXIT_CODE,
)


# ---------------------------------------------------------------------------
# restart_manager.py tests
# ---------------------------------------------------------------------------


class TestRequestRestart:
    def test_creates_restart_file(self, tmp_path):
        request_restart(str(tmp_path))
        assert (tmp_path / RESTART_FILE).exists()

    def test_restart_file_contains_timestamp(self, tmp_path):
        request_restart(str(tmp_path))
        content = (tmp_path / RESTART_FILE).read_text()
        assert "restart requested at" in content

    def test_overwrites_existing_file(self, tmp_path):
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("old content")
        request_restart(str(tmp_path))
        content = restart_file.read_text()
        assert "restart requested at" in content
        assert "old content" not in content


class TestCheckRestart:
    def test_returns_true_when_file_exists(self, tmp_path):
        (tmp_path / RESTART_FILE).write_text("restart")
        assert check_restart(str(tmp_path)) is True

    def test_returns_false_when_no_file(self, tmp_path):
        assert check_restart(str(tmp_path)) is False

    def test_since_ignores_old_file(self, tmp_path):
        """File touched before `since` is treated as stale."""
        (tmp_path / RESTART_FILE).write_text("restart")
        # Set mtime in the past
        past = time.time() - 10
        os.utime(tmp_path / RESTART_FILE, (past, past))
        assert check_restart(str(tmp_path), since=time.time()) is False

    def test_since_detects_fresh_file(self, tmp_path):
        """File touched after `since` is detected."""
        since = time.time() - 10
        (tmp_path / RESTART_FILE).write_text("restart")
        assert check_restart(str(tmp_path), since=since) is True

    def test_since_zero_means_no_filter(self, tmp_path):
        """Default since=0 behaves like the old check (any file triggers)."""
        (tmp_path / RESTART_FILE).write_text("restart")
        # Set mtime far in the past
        past = time.time() - 1000
        os.utime(tmp_path / RESTART_FILE, (past, past))
        assert check_restart(str(tmp_path), since=0) is True


class TestClearRestart:
    def test_removes_restart_file(self, tmp_path):
        (tmp_path / RESTART_FILE).write_text("restart")
        clear_restart(str(tmp_path))
        assert not (tmp_path / RESTART_FILE).exists()

    def test_no_error_when_file_missing(self, tmp_path):
        # Should not raise
        clear_restart(str(tmp_path))


class TestReexecBridge:
    @patch("os.execv")
    def test_calls_execv_with_python_and_argv(self, mock_execv):
        with patch.object(sys, "argv", ["/path/to/awake.py"]):
            reexec_bridge()
        mock_execv.assert_called_once_with(
            sys.executable, [sys.executable, "/path/to/awake.py"]
        )

    @patch("os.execv")
    def test_preserves_argv_arguments(self, mock_execv):
        with patch.object(sys, "argv", ["/path/to/awake.py", "--debug"]):
            reexec_bridge()
        mock_execv.assert_called_once_with(
            sys.executable, [sys.executable, "/path/to/awake.py", "--debug"]
        )


class TestRestartExitCode:
    def test_exit_code_is_42(self):
        assert RESTART_EXIT_CODE == 42


# ---------------------------------------------------------------------------
# /restart as /update alias — integration tests
# ---------------------------------------------------------------------------


class TestRestartAsStandaloneSkill:
    """/restart is a standalone skill that requests restart without pulling code."""

    @patch("app.command_handlers._dispatch_skill")
    def test_command_routes_restart_to_skill(self, mock_dispatch):
        from app.command_handlers import handle_command
        handle_command("/restart")
        mock_dispatch.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_handle_command_restart_end_to_end(self, mock_send, tmp_path):
        """End-to-end: handle_command('/restart') → restart skill → handler.

        /restart is now a standalone skill that requests a restart without
        pulling code. This test verifies the full path from command routing
        through the restart skill execution.
        """
        import app.command_handlers as ch
        from app.bridge_state import _reset_registry

        _reset_registry()
        # restart is a worker skill — set up a synchronous worker callback
        old_cb = ch._run_in_worker_cb
        ch._run_in_worker_cb = lambda fn: fn()
        try:
            with patch.object(ch, "KOAN_ROOT", tmp_path), \
                 patch.object(ch, "INSTANCE_DIR", tmp_path / "instance"), \
                 patch("app.restart_manager.request_restart") as mock_restart:
                ch.handle_command("/restart")

            assert mock_send.called
            assert "Restart requested" in mock_send.call_args[0][0]
            mock_restart.assert_called_once()
        finally:
            ch._run_in_worker_cb = old_cb
            _reset_registry()

    @patch("app.command_handlers.handle_resume")
    def test_restart_does_not_call_resume(self, mock_resume):
        with patch("app.command_handlers._dispatch_skill"):
            from app.command_handlers import handle_command
            handle_command("/restart")
        mock_resume.assert_not_called()

    @patch("app.command_handlers.handle_resume")
    def test_resume_aliases_still_work(self, mock_resume):
        """Verify /work, /awake still call resume, not restart.

        Note: /start has its own handler since session 257 (can start stopped runner).
        """
        from app.command_handlers import handle_command
        for cmd in ["/resume", "/work", "/awake"]:
            mock_resume.reset_mock()
            handle_command(cmd)
            assert mock_resume.call_count == 1, f"{cmd} should call handle_resume"


class TestRestartLoopPrevention:
    """Verify the dedup mechanism that prevents infinite restart loops."""

    def test_stale_file_does_not_trigger_restart(self, tmp_path):
        """A .koan-restart file from a previous incarnation is ignored."""
        restart_file = tmp_path / RESTART_FILE
        restart_file.write_text("old restart")
        past = time.time() - 10
        os.utime(restart_file, (past, past))

        startup_time = time.time()
        assert check_restart(str(tmp_path), since=startup_time) is False

    def test_fresh_file_triggers_restart(self, tmp_path):
        """A new .koan-restart file (after startup) triggers restart."""
        startup_time = time.time() - 10
        request_restart(str(tmp_path))
        assert check_restart(str(tmp_path), since=startup_time) is True


class TestRestartIsStandaloneSkill:
    """Verify /restart is a standalone skill, not an alias of /update."""

    @patch("app.command_handlers.send_telegram")
    def test_help_shows_restart_not_as_resume_alias(self, mock_send):
        from app.command_handlers import _handle_help
        _handle_help()
        help_text = mock_send.call_args[0][0]
        # /restart should NOT appear in the resume aliases
        for line in help_text.split("\n"):
            if "/resume" in line and "alias" in line:
                assert "/restart" not in line

    def test_restart_is_separate_skill(self):
        """The skill registry should list /restart as its own skill."""
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("restart")
        assert skill is not None
        assert skill.name == "restart"
        # /update is now hardcoded, not a skill
        assert registry.find_by_command("update") is None


class TestMainLoopRestartDetection:
    """Test that the main loop restart detection code is present and correct."""

    def test_main_imports_restart_functions(self):
        """Verify main() has the restart check/reexec/clear imports."""
        import inspect
        from app.awake import main
        source = inspect.getsource(main)
        assert "check_restart" in source
        assert "clear_restart" in source
        assert "reexec_bridge" in source

    def test_main_records_startup_time(self):
        """Verify main() records startup_time before the main loop."""
        import inspect
        from app.awake import main
        source = inspect.getsource(main)
        startup_idx = source.index("startup_time")
        while_idx = source.index("while True:")
        assert startup_idx < while_idx, "startup_time should be set before main loop"

    def test_main_uses_since_in_check(self):
        """Verify main() passes since=startup_time to check_restart."""
        import inspect
        from app.awake import main
        source = inspect.getsource(main)
        assert "since=startup_time" in source

    def test_main_checks_restart_in_loop(self):
        """Verify main() checks for restart signal inside the poll loop."""
        import inspect
        from app.awake import main
        source = inspect.getsource(main)
        while_idx = source.index("while True:")
        # check_restart should appear after the while loop starts
        check_idx = source.index("check_restart(str(KOAN_ROOT)", while_idx)
        assert check_idx > while_idx

    def test_main_clears_stale_file_after_first_poll(self):
        """Verify main() clears restart file after the first poll cycle."""
        import inspect
        from app.awake import main
        source = inspect.getsource(main)
        while_idx = source.index("while True:")
        # clear_restart should appear inside the loop (after first poll)
        clear_idx = source.index("clear_restart(str(KOAN_ROOT))", while_idx)
        assert clear_idx > while_idx
        # And it should be guarded by first_poll
        assert "first_poll" in source


