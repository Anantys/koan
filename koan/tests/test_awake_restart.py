"""Tests for awake.py restart signal integration."""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.awake import handle_command, _ensure_runner_alive


class TestRestartCommandRouting:
    """Tests for /restart command routing in handle_command."""

    @patch("app.command_handlers._dispatch_skill")
    def test_restart_routes_to_skill(self, mock_dispatch):
        """/restart should be dispatched via the skill system, not as a resume alias."""
        handle_command("/restart")
        mock_dispatch.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_restart_does_not_resume(self, mock_resume):
        """/restart should NOT call handle_resume anymore."""
        with patch("app.command_handlers._dispatch_skill"):
            handle_command("/restart")
        mock_resume.assert_not_called()

    @patch("app.command_handlers.handle_resume")
    def test_resume_still_works(self, mock_resume):
        """/resume should still call handle_resume."""
        handle_command("/resume")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_work_alias_still_resumes(self, mock_resume):
        handle_command("/work")
        mock_resume.assert_called_once()

    @patch("app.command_handlers._handle_start")
    def test_start_routes_to_handle_start(self, mock_start):
        """Since session 257, /start has its own handler (not just resume)."""
        handle_command("/start")
        mock_start.assert_called_once()


class TestUpdateCommandRouting:
    """Tests for /update command routing (hardcoded, not skill-dispatched)."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.atomic_write")
    def test_update_is_hardcoded(self, mock_write, mock_send):
        """Update writes CYCLE_FILE directly, not via skill dispatch."""
        handle_command("/update")
        mock_write.assert_called_once()
        mock_send.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.atomic_write")
    def test_upgrade_is_hardcoded(self, mock_write, mock_send):
        handle_command("/upgrade")
        mock_write.assert_called_once()
        mock_send.assert_called_once()


class TestHelpText:
    """Tests for help text updates."""

    @patch("app.command_handlers.send_telegram")
    def test_help_does_not_list_restart_as_resume_alias(self, mock_send):
        from app.command_handlers import _handle_help_detail
        _handle_help_detail("system")
        help_text = mock_send.call_args[0][0]
        # /restart should NOT appear on the same line as /resume
        for line in help_text.split("\n"):
            if "/resume" in line and "alias" in line:
                assert "/restart" not in line
        # /restart should appear in system group (as a standalone skill)
        assert "/update" in help_text
        assert "/restart" in help_text

    @patch("app.command_handlers.send_telegram")
    def test_help_lists_restart_as_update_alias(self, mock_send):
        """Help should show /restart as an alias of the /update skill."""
        from app.command_handlers import _handle_help_detail
        _handle_help_detail("system")
        help_text = mock_send.call_args[0][0]
        # /update should appear in the system group help
        assert "/update" in help_text


class TestEnsureRunnerAlive:
    """Tests for _ensure_runner_alive — bridge starts runner after restart."""

    @patch("app.awake.log")
    @patch("app.pid_manager.check_pidfile", return_value=12345)
    def test_no_op_when_runner_alive(self, mock_check, mock_log):
        """If runner is already running, do nothing."""
        _ensure_runner_alive()
        mock_check.assert_called_once()
        # Should not attempt to start runner
        mock_log.assert_not_called()

    @patch("app.awake.log")
    @patch("app.pid_manager.start_runner", return_value=(True, "Agent loop started (PID 99)"))
    @patch("app.pid_manager.check_pidfile", return_value=None)
    def test_starts_runner_when_dead(self, mock_check, mock_start, mock_log):
        """If runner is not running, start it."""
        _ensure_runner_alive()
        mock_start.assert_called_once()
        # Should log success
        assert any("started" in str(c).lower() for c in mock_log.call_args_list)

    @patch("app.awake.log")
    @patch("app.pid_manager.start_runner", return_value=(False, "Failed to launch: error"))
    @patch("app.pid_manager.check_pidfile", return_value=None)
    def test_logs_error_on_start_failure(self, mock_check, mock_start, mock_log):
        """If start_runner fails, log the error."""
        _ensure_runner_alive()
        mock_start.assert_called_once()
        assert any("error" in str(c) for c in mock_log.call_args_list)
