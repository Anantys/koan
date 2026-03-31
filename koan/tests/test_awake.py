"""Tests for awake.py — message classification, mission handling, project parsing, handlers."""

import importlib.util
import os
import re
import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

from app.awake import (
    is_mission,
    is_command,
    parse_project,
    handle_chat,
    handle_message,
    flush_outbox,
    _requeue_outbox,
    _write_outbox_failed,
    _recover_staged_outbox,
    _staging_path,
    _format_outbox_message,
    _clean_chat_response,
    _run_in_worker,
    _flush_outbox_async,
    get_updates,
    check_config,
)
from app.outbox_manager import OutboxManager
from app.bridge_state import (
    _get_registry,
    _reset_registry,
)
from app.command_handlers import (
    CORE_COMMANDS,
    handle_command,
    handle_mission,
    handle_resume,
    _handle_start,
    _dispatch_skill,
    _handle_help,
    _handle_help_command,
    _handle_help_detail,
    _handle_skill_command,
    _handle_skill_install,
    _handle_skill_remove,
    _handle_skill_sources,
    _handle_skill_update,
)

pytestmark = pytest.mark.slow

_STATUS_HANDLER_PATH = str(
    Path(__file__).parent.parent / "skills" / "core" / "status" / "handler.py"
)


def _call_status_handler(tmp_path):
    """Load and call the status skill handler directly.

    Creates instance_dir if needed and returns the handler output string.
    """
    from app.skills import SkillContext

    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    ctx = SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="status",
    )
    spec = importlib.util.spec_from_file_location("status_handler", _STATUS_HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handle(ctx)


# ---------------------------------------------------------------------------
# is_mission
# ---------------------------------------------------------------------------

class TestIsMission:
    """Test mission detection heuristics."""

    def test_explicit_mission_prefix(self):
        assert is_mission("mission: audit the backend") is True

    def test_explicit_mission_prefix_with_space(self):
        assert is_mission("mission : fix the login bug") is True

    def test_imperative_verb_start(self):
        assert is_mission("implement dark mode") is True
        assert is_mission("fix the authentication bug") is True
        assert is_mission("audit the security layer") is True
        assert is_mission("create a new endpoint") is True
        assert is_mission("add tests for awake.py") is True
        assert is_mission("review the PR") is True
        assert is_mission("analyze the logs") is True
        assert is_mission("explore the codebase") is True
        assert is_mission("build the dashboard") is True
        assert is_mission("write a migration script") is True
        assert is_mission("run the test suite") is True
        assert is_mission("deploy to staging") is True
        assert is_mission("test the new feature") is True
        assert is_mission("refactor the auth module") is True

    def test_long_message_with_verb(self):
        long_text = "implement " + "x " * 150  # >200 chars
        assert is_mission(long_text) is True

    def test_short_question_not_mission(self):
        assert is_mission("how are you?") is False
        assert is_mission("what's the status?") is False

    def test_greeting_not_mission(self):
        assert is_mission("hello") is False
        assert is_mission("good morning") is False

    def test_case_insensitive(self):
        assert is_mission("Implement dark mode") is True
        assert is_mission("MISSION: do something") is True

    def test_empty_string(self):
        assert is_mission("") is False

    def test_long_message_with_verb_only_at_start(self):
        """Long messages should only match verbs at the start, not mid-text."""
        # Verb at start — IS a mission
        long_mission = "implement " + "x " * 150
        assert is_mission(long_mission) is True
        # Verb buried mid-text — NOT a mission
        long_chat = "I was thinking about how we should " + "x " * 100 + " and then implement something"
        assert is_mission(long_chat) is False

    def test_conversational_with_action_verb(self):
        """Short messages starting with action verbs but clearly conversational."""
        # These are correctly classified as missions by the current heuristic.
        # Users should use /chat to override when needed.
        assert is_mission("add me to the list") is True  # starts with "add"
        assert is_mission("run me through the pipeline") is True  # starts with "run"

    def test_long_conversational_not_mission(self):
        """Long conversational messages without leading verbs are not missions."""
        long_chat = "Hey, I wanted to discuss something with you. " + "blah " * 60
        assert is_mission(long_chat) is False


# ---------------------------------------------------------------------------
# /chat command (force chat mode)
# ---------------------------------------------------------------------------

class TestHandleChatCommand:
    """Test /chat prefix to force chat mode.

    /chat is a worker skill — it runs in a background thread and calls
    ctx.handle_chat() from the skill handler. Tests verify the full
    dispatch path through the skill system.
    """

    @patch("app.command_handlers._run_in_worker_cb")
    def test_chat_command_dispatches_as_worker(self, mock_worker):
        """'/chat fix the bug' should dispatch via worker thread (skill is worker=true)."""
        handle_command("/chat fix the bug")
        mock_worker.assert_called_once()

    @patch("app.command_handlers._run_in_worker_cb")
    def test_chat_command_with_long_text(self, mock_worker):
        """/chat with imperative text should still route to chat, not mission."""
        handle_command("/chat implement dark mode for the dashboard")
        mock_worker.assert_called_once()

    def test_chat_command_empty_shows_usage(self, tmp_path):
        """/chat with no text should return usage from handler."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chat_handler",
            str(Path(__file__).parent.parent / "skills" / "core" / "chat" / "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from app.skills import SkillContext
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="")
        result = mod.handle(ctx)
        assert "Usage" in result
        assert "/chat" in result

    def test_chat_command_whitespace_only_shows_usage(self, tmp_path):
        """/chat followed by only whitespace shows usage from handler."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chat_handler",
            str(Path(__file__).parent.parent / "skills" / "core" / "chat" / "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from app.skills import SkillContext
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="")
        result = mod.handle(ctx)
        assert "Usage" in result

    @patch("app.command_handlers._run_in_worker_cb")
    def test_chat_via_handle_message(self, mock_worker):
        """/chat goes through handle_message -> handle_command -> worker dispatch."""
        handle_message("/chat add me to the list of testers")
        mock_worker.assert_called_once()


# ---------------------------------------------------------------------------
# is_command
# ---------------------------------------------------------------------------

class TestIsCommand:
    def test_slash_commands(self):
        assert is_command("/stop") is True
        assert is_command("/status") is True
        assert is_command("/resume") is True

    def test_non_commands(self):
        assert is_command("hello") is False
        assert is_command("fix the bug") is False
        assert is_command("") is False


# ---------------------------------------------------------------------------
# parse_project
# ---------------------------------------------------------------------------

class TestParseProject:
    def test_with_project_tag(self):
        project, text = parse_project("[project:anantys] fix the login")
        assert project == "anantys"
        assert text == "fix the login"

    def test_without_project_tag(self):
        project, text = parse_project("fix the login")
        assert project is None
        assert text == "fix the login"

    def test_project_tag_with_hyphen(self):
        project, text = parse_project("[project:anantys-back] deploy")
        assert project == "anantys-back"
        assert text == "deploy"

    def test_project_tag_with_underscore(self):
        project, text = parse_project("[project:my_project] test")
        assert project == "my_project"
        assert text == "test"

    def test_project_tag_in_middle(self):
        project, text = parse_project("fix [project:koan] the bug")
        assert project == "koan"
        assert text == "fix the bug"

    def test_french_projet_tag(self):
        project, text = parse_project("[projet:anantys] fix the login")
        assert project == "anantys"
        assert text == "fix the login"

    def test_french_projet_tag_in_middle(self):
        project, text = parse_project("fix [projet:koan] the bug")
        assert project == "koan"
        assert text == "fix the bug"


# ---------------------------------------------------------------------------
# handle_mission (integration with filesystem)
# ---------------------------------------------------------------------------

class TestHandleMission:
    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.MISSIONS_FILE")
    @patch("app.command_handlers.INSTANCE_DIR")
    def test_mission_appended_to_pending(self, mock_inst, mock_file, mock_send, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n(none)\n\n## In Progress\n\n## Done\n"
        )
        mock_file.__class__ = type(missions_file)
        # Directly test the file manipulation logic
        with patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_mission("mission: audit security")

        content = missions_file.read_text()
        assert "- audit security" in content
        mock_send.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_mission_with_project_tag(self, mock_send, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n(none)\n\n## In Progress\n\n"
        )
        with patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_mission("[project:koan] add tests")

        content = missions_file.read_text()
        assert "- [project:koan] add tests" in content

    @patch("app.command_handlers.send_telegram")
    def test_mission_auto_detects_project_from_first_word(self, mock_send, tmp_path):
        """'koan fix bug' should auto-detect project 'koan' from the first word."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n(none)\n\n## In Progress\n\n"
        )
        with patch("app.command_handlers.MISSIONS_FILE", missions_file), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/path/to/koan")]):
            handle_mission("koan fix the bug")

        content = missions_file.read_text()
        assert "- [project:koan] fix the bug" in content
        msg = mock_send.call_args[0][0]
        assert "project: koan" in msg

    @patch("app.command_handlers.send_telegram")
    def test_mission_no_project_when_first_word_unknown(self, mock_send, tmp_path):
        """First word 'fix' should not be detected as project."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n(none)\n\n## In Progress\n\n"
        )
        with patch("app.command_handlers.MISSIONS_FILE", missions_file), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/path/to/koan")]):
            handle_mission("fix the bug")

        content = missions_file.read_text()
        assert "- fix the bug" in content
        assert "[project:" not in content


# ---------------------------------------------------------------------------
# Status skill handler (was _build_status)
# ---------------------------------------------------------------------------

class TestBuildStatus:
    """Tests for the status skill handler output."""

    def test_status_with_french_sections(self, tmp_path):
        """Status handler parses French section names from missions.md."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] add tests\n"
            "- fix bug\n\n"
            "## In Progress\n\n"
            "- [project:koan] doing stuff\n\n"
        )
        status = _call_status_handler(tmp_path)

        assert "Kōan Status" in status
        assert "koan" in status
        assert "default" in status
        assert "In progress: 1" in status

    def test_status_shows_pending_titles(self, tmp_path):
        """Status handler shows pending mission titles."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] add tests\n"
            "- [project:koan] fix dashboard\n\n"
            "## In Progress\n\n"
        )
        status = _call_status_handler(tmp_path)

        assert "Pending: 2" in status
        assert "add tests" in status
        assert "fix dashboard" in status
        assert "[project:koan]" not in status

    def test_status_empty(self, tmp_path):
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Kōan Status" in status

    def test_status_with_stop_file(self, tmp_path):
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-stop").write_text("STOP")
        status = _call_status_handler(tmp_path)
        assert "Stopping" in status

    def test_status_with_loop_status(self, tmp_path):
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-status").write_text("Run 3/20")
        status = _call_status_handler(tmp_path)
        assert "Run 3/20" in status


# ---------------------------------------------------------------------------
# handle_command
# ---------------------------------------------------------------------------

class TestHandleCommand:
    @patch("app.command_handlers.send_telegram")
    def test_stop_creates_file(self, mock_send, tmp_path):
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/stop")
        assert (tmp_path / ".koan-stop").exists()
        mock_send.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_status_routes_via_skill(self, mock_send, tmp_path):
        """Status now goes through skill system."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/status")
        mock_send.assert_called_once()
        assert "Status" in mock_send.call_args[0][0]

    @patch("app.command_handlers.handle_resume")
    def test_resume_delegates(self, mock_resume):
        handle_command("/resume")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_work_delegates_to_resume(self, mock_resume):
        handle_command("/work")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_awake_delegates_to_resume(self, mock_resume):
        handle_command("/awake")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_run_delegates_to_resume(self, mock_resume):
        handle_command("/run")
        mock_resume.assert_called_once()

    @patch("app.command_handlers._dispatch_skill")
    def test_restart_routes_to_skill(self, mock_dispatch):
        handle_command("/restart")
        mock_dispatch.assert_called_once()

    @patch("app.command_handlers._handle_start")
    def test_start_routes_to_handle_start(self, mock_start):
        handle_command("/start")
        mock_start.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_verbose_via_skill(self, mock_send, tmp_path):
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/verbose")
        assert (tmp_path / ".koan-verbose").exists()
        mock_send.assert_called_once()
        assert "verbose" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers.send_telegram")
    def test_silent_via_skill(self, mock_send, tmp_path):
        verbose_file = tmp_path / ".koan-verbose"
        verbose_file.write_text("VERBOSE")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/silent")
        assert not verbose_file.exists()
        mock_send.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_silent_when_already_silent(self, mock_send, tmp_path):
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/silent")
        mock_send.assert_called_once()
        assert "silent" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers.send_telegram")
    def test_unknown_command_rejected_without_llm(self, mock_send):
        """Unknown /commands are rejected immediately — no LLM call."""
        handle_command("/unknown")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Unknown command" in msg
        assert "/unknown" in msg
        assert "/help" in msg

    @patch("app.command_handlers._run_in_worker_cb")
    @patch("app.command_handlers._handle_chat_cb")
    @patch("app.command_handlers.send_telegram")
    def test_unknown_command_does_not_call_llm(self, mock_send, mock_chat, mock_worker):
        """Unknown /commands must NOT fall through to Claude chat."""
        handle_command("/nonexistent_thing")
        mock_chat.assert_not_called()
        mock_worker.assert_not_called()


# ---------------------------------------------------------------------------
# handle_resume
# ---------------------------------------------------------------------------

class TestHandleResume:
    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_no_quota_file(self, mock_send, mock_alive, tmp_path):
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert "No pause or quota hold" in mock_send.call_args[0][0]

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_likely_reset(self, mock_send, mock_alive, tmp_path):
        quota_file = tmp_path / ".koan-quota-reset"
        old_ts = str(int(time.time()) - 3 * 3600)  # 3 hours ago
        quota_file.write_text(f"resets 7pm (Europe/Paris)\n{old_ts}")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert not quota_file.exists()
        assert "Quota likely reset" in mock_send.call_args[0][0]

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_not_yet_reset(self, mock_send, mock_alive, tmp_path):
        quota_file = tmp_path / ".koan-quota-reset"
        recent_ts = str(int(time.time()) - 30 * 60)  # 30 min ago
        quota_file.write_text(f"resets 7pm (Europe/Paris)\n{recent_ts}")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert quota_file.exists()
        assert "not reset yet" in mock_send.call_args[0][0]

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_corrupt_quota_file(self, mock_send, mock_alive, tmp_path):
        """Corrupt timestamp in legacy quota file should degrade gracefully.
        With the fix, the invalid timestamp defaults to 0 (epoch), which
        means hours_since_pause is huge → treated as 'likely reset'."""
        quota_file = tmp_path / ".koan-quota-reset"
        quota_file.write_text("garbage\nnot-a-number")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        # paused_at defaults to 0 → huge hours_since_pause → likely reset
        assert "Quota likely reset" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# _reset_session_counters
# ---------------------------------------------------------------------------

class TestResetSessionCounters:
    """Tests for _reset_session_counters — called by handle_resume on quota resume."""

    @patch("app.command_handlers.log")
    @patch("app.command_handlers.INSTANCE_DIR", "")
    def test_calls_cmd_reset_session(self, mock_log, tmp_path):
        """Should call cmd_reset_session with correct paths."""
        from app.command_handlers import _reset_session_counters
        with patch("app.command_handlers.INSTANCE_DIR", str(tmp_path)), \
             patch("app.usage_estimator.cmd_reset_session") as mock_reset:
            _reset_session_counters()
        mock_reset.assert_called_once()
        # Verify paths
        args = mock_reset.call_args[0]
        assert str(args[0]).endswith("usage_state.json")
        assert str(args[1]).endswith("usage.md")

    @patch("app.command_handlers.log")
    def test_handles_import_error_gracefully(self, mock_log, tmp_path):
        """Should not crash if usage_estimator is unavailable."""
        from app.command_handlers import _reset_session_counters
        with patch("app.command_handlers.INSTANCE_DIR", str(tmp_path)), \
             patch.dict("sys.modules", {"app.usage_estimator": None}):
            # Should not raise
            _reset_session_counters()
        # Should log an error
        mock_log.assert_called()


# ---------------------------------------------------------------------------
# _handle_start
# ---------------------------------------------------------------------------


class TestHandleStart:
    """Tests for /start — start agent loop or resume if paused."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.handle_resume")
    def test_runner_running_and_paused_delegates_to_resume(self, mock_resume, mock_send, tmp_path):
        """If runner is running but paused, /start behaves like /resume."""
        (tmp_path / ".koan-pause").write_text("PAUSE")
        # check_pidfile is imported lazily inside _handle_start, patch at source
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.pid_manager.check_pidfile", return_value=os.getpid()), \
             patch("app.command_handlers.log"):
            _handle_start()
        mock_resume.assert_called_once()
        mock_send.assert_not_called()  # resume handles messaging

    @patch("app.command_handlers.send_telegram")
    def test_runner_running_not_paused(self, mock_send, tmp_path):
        """If runner is running and not paused, tell user."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.pid_manager.check_pidfile", return_value=12345):
            _handle_start()
        assert "already running" in mock_send.call_args[0][0]
        assert "12345" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    def test_runner_stopped_launches_successfully(self, mock_send, tmp_path):
        """If runner is stopped, /start launches it."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager.start_runner", return_value=(True, "Agent loop started (PID 99)")):
            _handle_start()
        # Two messages: "Starting..." and the success result
        assert mock_send.call_count == 2
        assert "Starting" in mock_send.call_args_list[0][0][0]
        assert "Agent loop started" in mock_send.call_args_list[1][0][0]

    @patch("app.command_handlers.send_telegram")
    def test_runner_stopped_launch_fails(self, mock_send, tmp_path):
        """If launch fails, report the error."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager.start_runner", return_value=(False, "Failed to launch: No such file")):
            _handle_start()
        assert mock_send.call_count == 2
        assert "Starting" in mock_send.call_args_list[0][0][0]
        assert "Failed to launch" in mock_send.call_args_list[1][0][0]

    @patch("app.command_handlers.send_telegram")
    def test_stop_file_cleared_before_launch(self, mock_send, tmp_path):
        """Verify start_runner is called (which internally clears .koan-stop)."""
        (tmp_path / ".koan-stop").write_text("STOP")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager.start_runner", return_value=(True, "Started")) as mock_start:
            _handle_start()
        mock_start.assert_called_once_with(tmp_path)

    @patch("app.command_handlers.send_telegram")
    def test_start_routed_from_handle_command(self, mock_send, tmp_path):
        """The /start command routes to _handle_start, not handle_resume."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.pid_manager.check_pidfile", return_value=42):
            handle_command("/start")
        assert "already running" in mock_send.call_args[0][0]

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_still_works_separately(self, mock_send, mock_alive, tmp_path):
        """/resume should NOT call _handle_start — verify separation."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/resume")
        # /resume with no pause file → "No pause or quota hold"
        assert "No pause" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    def test_help_shows_system_group(self, mock_send, tmp_path):
        """/help should list the system group (which contains start-like operations)."""
        registry_mock = MagicMock()
        registry_mock.groups.return_value = []
        registry_mock.list_all.return_value = []
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers._get_registry", return_value=registry_mock):
            _handle_help()
        help_text = mock_send.call_args[0][0]
        assert "/system" in help_text


# ---------------------------------------------------------------------------
# handle_chat
# ---------------------------------------------------------------------------

class TestHandleChat:
    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_successful_chat(self, mock_run, mock_send, mock_tools,
                             mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        mock_run.return_value = MagicMock(stdout="Hello back!", returncode=0)
        journal_dir = tmp_path / "journal" / "2026-02-01"
        journal_dir.mkdir(parents=True)
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", "test soul"), \
             patch("app.awake.SUMMARY", "test summary"):
            handle_chat("hello")
        mock_send.assert_called_once_with("Hello back!")
        # Saved both user and assistant messages
        assert mock_save.call_count == 2

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_chat_timeout(self, mock_run, mock_send, mock_tools,
                          mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 180)
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""), \
             patch("app.awake.CHAT_TIMEOUT", 180), \
             patch("app.awake.time.sleep"):
            handle_chat("complex question")
        mock_send.assert_called_once()
        assert "Timeout" in mock_send.call_args[0][0]

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_chat_error_nonzero_exit(self, mock_run, mock_send, mock_tools,
                                     mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        mock_run.return_value = MagicMock(stdout="", returncode=1, stderr="API error")
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""):
            handle_chat("hello")
        mock_send.assert_called_once()
        assert "couldn't formulate" in mock_send.call_args[0][0]

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_chat_reads_journal_flat_fallback(self, mock_run, mock_send, mock_tools,
                                              mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        """Falls back to flat journal if nested dir doesn't exist."""
        mock_run.return_value = MagicMock(stdout="ok", returncode=0)
        # No nested journal dir — create flat file
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir(parents=True)
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""):
            handle_chat("hi")
        mock_run.assert_called_once()

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.cli_exec.run_cli")
    def test_chat_unexpected_error_sends_feedback(self, mock_run, mock_send, mock_tools,
                                                   mock_tools_desc, mock_fmt, mock_hist,
                                                   mock_save, tmp_path):
        """Unexpected exceptions in handle_chat should still send error feedback to the user."""
        mock_run.side_effect = RuntimeError("unexpected import failure")
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""):
            handle_chat("hello")
        # Must send error feedback (not silent)
        mock_send.assert_called_once()
        assert "Something went wrong" in mock_send.call_args[0][0]
        # Must save the error message to conversation history
        assert mock_save.call_count >= 2  # user msg + error msg

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_empty_response_sends_fallback(self, mock_run, mock_send, mock_tools,
                                           mock_tools_desc, mock_fmt, mock_hist,
                                           mock_save, tmp_path):
        """Empty Claude response (exit 0, blank stdout) must still reply to the user."""
        mock_run.return_value = MagicMock(stdout="", returncode=0, stderr="")
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""):
            handle_chat("hello")
        # User must receive a reply — not silence
        mock_send.assert_called_once()
        # Must persist the fallback to conversation history
        assert mock_save.call_count >= 2  # user msg + fallback msg


# ---------------------------------------------------------------------------
# flush_outbox
# ---------------------------------------------------------------------------

class TestFlushOutbox:
    @patch.object(OutboxManager, "_format_message", return_value="Formatted msg")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_formats_and_sends(self, mock_send, mock_fmt, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Raw message here")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        mock_fmt.assert_called_once_with("Raw message here")
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[0][0] == "Formatted msg"
        assert call_kwargs[1]["priority"].name == "ACTION"
        assert outbox.read_text() == ""

    @patch.object(OutboxManager, "_format_message", return_value="Formatted msg")
    @patch("app.outbox_manager.send_telegram", return_value=False)
    def test_flush_keeps_on_send_failure(self, mock_send, mock_fmt, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Important message")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        # Message re-queued to outbox on send failure
        assert "Important message" in outbox.read_text()

    def test_flush_no_file(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()  # Should not raise

    @patch.object(OutboxManager, "_format_message", return_value="X")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_empty_file(self, mock_send, mock_fmt, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        mock_send.assert_not_called()

    @patch.object(OutboxManager, "_format_message", return_value="X")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_whitespace_only(self, mock_send, mock_fmt, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("   \n\n  ")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        mock_send.assert_not_called()

    @patch.object(OutboxManager, "_format_message", return_value="Formatted msg")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_clears_before_format(self, mock_send, mock_fmt, tmp_path):
        """File should be cleared BEFORE the slow format call, not after."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Original message")

        file_content_during_format = []

        def capture_format(content):
            # During formatting, the file should already be empty
            file_content_during_format.append(outbox.read_text())
            return "Formatted"

        mock_fmt.side_effect = capture_format
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        # File was cleared before format was called
        assert file_content_during_format == [""]

    @patch.object(OutboxManager, "_format_message", return_value="Fmt")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_concurrent_write_during_format_preserved(self, mock_send, mock_fmt, tmp_path):
        """Messages written during formatting should survive (not be truncated)."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Message A")

        def format_and_inject(content):
            # Simulate another process appending during the slow format call
            with open(outbox, "a") as f:
                f.write("Message B\n")
            return "Formatted A"

        mock_fmt.side_effect = format_and_inject
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        # Message B was written AFTER the file was cleared — it should survive
        assert "Message B" in outbox.read_text()

    @patch.object(OutboxManager, "_format_message", return_value="Fmt")
    @patch("app.outbox_manager.send_telegram", return_value=False)
    def test_flush_requeue_on_failure_preserves_new_writes(self, mock_send, mock_fmt, tmp_path):
        """On send failure, re-queued content should not overwrite new messages."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Message A")

        def format_and_inject(content):
            # Another message arrives during formatting
            with open(outbox, "a") as f:
                f.write("Message B\n")
            return "Formatted A"

        mock_fmt.side_effect = format_and_inject
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        content = outbox.read_text()
        # Both messages should be in the file
        assert "Message B" in content
        assert "Message A" in content

    @patch("app.outbox_manager.scan_and_log")
    def test_flush_blocked_clears_file_before_quarantine(self, mock_scan, tmp_path):
        """Blocked messages should still clear the outbox promptly."""
        from types import SimpleNamespace
        mock_scan.return_value = SimpleNamespace(blocked=True, reason="secret detected")
        outbox = tmp_path / "outbox.md"
        outbox.write_text("SECRET_KEY=abc123")
        with patch("app.awake.OUTBOX_FILE", outbox), \
             patch("app.awake.INSTANCE_DIR", tmp_path):
            flush_outbox()
        # File is cleared
        assert outbox.read_text() == ""
        # Quarantine file has the content
        quarantine = tmp_path / "outbox-quarantine.md"
        assert quarantine.exists()
        assert "SECRET_KEY" in quarantine.read_text()


    @patch.object(OutboxManager, "_format_message", return_value="PR #42 merged")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_expands_github_refs(self, mock_send, mock_fmt, tmp_path):
        """GitHub #refs should be expanded to full URLs before sending."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("🏁 [myproject]\nPR #42 merged")
        with patch("app.awake.OUTBOX_FILE", outbox), \
             patch("app.projects_merged.get_github_url",
                   return_value="https://github.com/owner/myproject"):
            flush_outbox()
        sent_text = mock_send.call_args[0][0]
        assert "https://github.com/owner/myproject/issues/42" in sent_text

    @patch.object(OutboxManager, "_format_message", return_value="All good")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_no_expansion_without_project(self, mock_send, mock_fmt, tmp_path):
        """When no project tag is found, text is sent unchanged."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("All good")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[0][0] == "All good"
        assert call_kwargs[1]["priority"].name == "ACTION"


class TestOutboxPriorityParsing:
    """Tests for _parse_outbox_priority() — header parsing and stripping."""

    def test_no_header_defaults_to_action(self):
        from app.awake import _parse_outbox_priority
        from app.notify import NotificationPriority
        priority, cleaned = _parse_outbox_priority("Hello world")
        assert priority.name == "ACTION"
        assert cleaned == "Hello world"

    def test_urgent_header_parsed(self):
        from app.awake import _parse_outbox_priority
        priority, cleaned = _parse_outbox_priority("[priority:urgent]\nMessage")
        assert priority.name == "URGENT"
        assert cleaned == "Message"

    def test_info_header_parsed(self):
        from app.awake import _parse_outbox_priority
        priority, cleaned = _parse_outbox_priority("[priority:info]\nSoft update")
        assert priority.name == "INFO"
        assert cleaned == "Soft update"

    def test_warning_header_parsed(self):
        from app.awake import _parse_outbox_priority
        priority, cleaned = _parse_outbox_priority("[priority:warning]\nQuota low")
        assert priority.name == "WARNING"
        assert cleaned == "Quota low"

    def test_multiple_blocks_highest_wins(self):
        from app.awake import _parse_outbox_priority
        content = "[priority:info]\nUpdate 1\n[priority:urgent]\nCritical alert"
        priority, cleaned = _parse_outbox_priority(content)
        assert priority.name == "URGENT"
        assert "[priority:" not in cleaned

    def test_header_stripped_from_content(self):
        from app.awake import _parse_outbox_priority
        _, cleaned = _parse_outbox_priority("[priority:action]\nMission complete")
        assert "[priority:" not in cleaned
        assert "Mission complete" in cleaned

    @patch.object(OutboxManager, "_format_message", return_value="Formatted")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_passes_priority_to_send(self, mock_send, mock_fmt, tmp_path):
        """flush_outbox passes parsed priority to send_telegram."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("[priority:urgent]\nServer is down")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        mock_send.assert_called_once()
        assert mock_send.call_args[1]["priority"].name == "URGENT"

    @patch.object(OutboxManager, "_format_message", return_value="Formatted")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_legacy_entry_defaults_to_action(self, mock_send, mock_fmt, tmp_path):
        """Legacy outbox entries without a header default to ACTION."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Old-style message without header")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        assert mock_send.call_args[1]["priority"].name == "ACTION"

    @patch.object(OutboxManager, "_format_message")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_priority_header_stripped_before_format(self, mock_send, mock_fmt, tmp_path):
        """Priority header is stripped before content is passed to Claude formatter."""
        mock_fmt.return_value = "fmt"
        outbox = tmp_path / "outbox.md"
        outbox.write_text("[priority:warning]\nQuota is low")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        formatted_input = mock_fmt.call_args[0][0]
        assert "[priority:" not in formatted_input
        assert "Quota is low" in formatted_input


class TestRequeueOutbox:
    def test_requeue_appends_content(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        with patch("app.awake.OUTBOX_FILE", outbox):
            _requeue_outbox("Failed message")
        assert "Failed message" in outbox.read_text()

    def test_requeue_preserves_existing_content(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("New message\n")
        with patch("app.awake.OUTBOX_FILE", outbox):
            _requeue_outbox("Failed message")
        content = outbox.read_text()
        assert "New message" in content
        assert "Failed message" in content

    def test_requeue_handles_missing_file(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        # File doesn't exist — requeue should create it
        with patch("app.awake.OUTBOX_FILE", outbox):
            _requeue_outbox("Recovered message")
        assert "Recovered message" in outbox.read_text()


# ---------------------------------------------------------------------------
# _write_outbox_failed
# ---------------------------------------------------------------------------


class TestWriteOutboxFailed:
    def test_writes_content_to_failed_file(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        with patch("app.awake.OUTBOX_FILE", outbox):
            _write_outbox_failed("Lost message", OSError("disk full"))
        failed = tmp_path / "outbox-failed.md"
        assert failed.exists()
        content = failed.read_text()
        assert "Lost message" in content
        assert "disk full" in content

    def test_appends_to_existing_failed_file(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        failed = tmp_path / "outbox-failed.md"
        failed.write_text("<!-- previous entry -->\nOld content\n")
        with patch("app.awake.OUTBOX_FILE", outbox):
            _write_outbox_failed("New lost message", OSError("perm denied"))
        content = failed.read_text()
        assert "Old content" in content
        assert "New lost message" in content

    def test_includes_timestamp_comment(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        with patch("app.awake.OUTBOX_FILE", outbox):
            _write_outbox_failed("Msg", ValueError("oops"))
        failed = tmp_path / "outbox-failed.md"
        content = failed.read_text()
        assert content.startswith("<!-- lost ")
        assert "oops" in content

    def test_requeue_calls_fallback_on_write_error(self, tmp_path):
        """_requeue_outbox should call _write_outbox_failed when outbox write fails."""
        bad_outbox = tmp_path / "no-such-dir" / "outbox.md"
        with patch("app.awake.OUTBOX_FILE", bad_outbox), \
             patch.object(OutboxManager, "_write_failed") as mock_fallback:
            _requeue_outbox("Important message")
        mock_fallback.assert_called_once()
        args = mock_fallback.call_args[0]
        assert args[0] == "Important message"
        assert isinstance(args[1], Exception)

    def test_fallback_failure_logs_content_snippet(self, tmp_path):
        """If even the fallback file can't be written, log the content."""
        bad_failed_dir = tmp_path / "no-such-dir" / "outbox-failed.md"
        with patch("app.awake.OUTBOX_FILE", bad_failed_dir), \
             patch("app.outbox_manager.log") as mock_log:
            _write_outbox_failed("Critical data", OSError("boom"))
        mock_log.assert_called()
        logged = str(mock_log.call_args_list[-1])
        assert "Critical data" in logged


class TestStagingFileRecovery:
    """Crash-safety: staging file (outbox-sending.md) prevents message loss."""

    @patch.object(OutboxManager, "_format_message", return_value="Formatted")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_staging_file_created_then_cleaned_on_success(self, mock_send, mock_fmt, tmp_path):
        """Staging file is created before truncation and deleted after successful send."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Test message")
        staging = tmp_path / "outbox-sending.md"

        staging_existed_during_format = []

        def check_staging(content):
            staging_existed_during_format.append(staging.exists())
            return "Formatted"

        mock_fmt.side_effect = check_staging
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        # Staging existed during formatting (crash-safe window)
        assert staging_existed_during_format == [True]
        # Staging cleaned up after success
        assert not staging.exists()

    @patch.object(OutboxManager, "_format_message", return_value="Formatted")
    @patch("app.outbox_manager.send_telegram", return_value=False)
    def test_staging_file_cleaned_on_send_failure(self, mock_send, mock_fmt, tmp_path):
        """Staging file is cleaned up even when send fails (content is re-queued)."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Important message")
        staging = tmp_path / "outbox-sending.md"
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        # Content re-queued to outbox
        assert "Important message" in outbox.read_text()
        # Staging file cleaned up
        assert not staging.exists()

    def test_recover_staged_outbox_requeues_content(self, tmp_path):
        """_recover_staged_outbox re-queues content from a crashed flush."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        staging = tmp_path / "outbox-sending.md"
        staging.write_text("Crashed message")
        with patch("app.awake.OUTBOX_FILE", outbox):
            _recover_staged_outbox()
        assert "Crashed message" in outbox.read_text()
        assert not staging.exists()

    def test_recover_staged_outbox_no_staging_file(self, tmp_path):
        """_recover_staged_outbox is a no-op when no staging file exists."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("Existing content")
        with patch("app.awake.OUTBOX_FILE", outbox):
            _recover_staged_outbox()
        assert outbox.read_text() == "Existing content"

    def test_recover_staged_outbox_empty_staging(self, tmp_path):
        """Empty staging file is cleaned up without re-queuing."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        staging = tmp_path / "outbox-sending.md"
        staging.write_text("   \n  ")
        with patch("app.awake.OUTBOX_FILE", outbox):
            _recover_staged_outbox()
        assert not staging.exists()
        assert outbox.read_text() == ""

    @patch.object(OutboxManager, "_format_message", return_value="New formatted")
    @patch("app.outbox_manager.send_telegram", return_value=True)
    def test_flush_recovers_staged_before_processing_new(self, mock_send, mock_fmt, tmp_path):
        """flush_outbox recovers staged content before processing new outbox content."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("New message")
        staging = tmp_path / "outbox-sending.md"
        staging.write_text("Crashed old message")
        with patch("app.awake.OUTBOX_FILE", outbox):
            flush_outbox()
        # Staged content was re-queued, new message was processed
        fmt_calls = [call[0][0] for call in mock_fmt.call_args_list]
        combined = " ".join(fmt_calls)
        assert "New message" in combined or "Crashed old message" in combined

    @patch("app.outbox_manager.scan_and_log")
    def test_staging_file_cleaned_on_blocked_message(self, mock_scan, tmp_path):
        """Staging file is cleaned up when outbox content is blocked by scanner."""
        from types import SimpleNamespace
        mock_scan.return_value = SimpleNamespace(blocked=True, reason="secret found")
        outbox = tmp_path / "outbox.md"
        outbox.write_text("API_KEY=secret123")
        staging = tmp_path / "outbox-sending.md"
        with patch("app.awake.OUTBOX_FILE", outbox), \
             patch("app.awake.INSTANCE_DIR", tmp_path):
            flush_outbox()
        assert not staging.exists()

    def test_staging_path_resolves_to_outbox_sibling(self, tmp_path):
        """_staging_path returns outbox-sending.md next to outbox.md."""
        outbox = tmp_path / "outbox.md"
        with patch("app.awake.OUTBOX_FILE", outbox):
            result = _staging_path()
        assert result == tmp_path / "outbox-sending.md"


# ---------------------------------------------------------------------------
# _format_outbox_message
# ---------------------------------------------------------------------------

class TestFormatOutboxMessage:
    @patch("app.outbox_manager.format_message", return_value="Formatted")
    @patch("app.outbox_manager.load_memory_context", return_value="mem")
    @patch("app.outbox_manager.load_human_prefs", return_value="prefs")
    @patch("app.outbox_manager.load_soul", return_value="soul")
    def test_formats_with_context(self, mock_soul, mock_prefs, mock_mem, mock_fmt, tmp_path):
        with patch("app.awake.INSTANCE_DIR", tmp_path):
            result = _format_outbox_message("raw content")
        assert result == "Formatted"
        mock_fmt.assert_called_once_with("raw content", "soul", "prefs", "mem")

    @patch("app.outbox_manager.load_soul", side_effect=Exception("load error"))
    def test_fallback_on_error(self, mock_soul, tmp_path):
        with patch("app.awake.INSTANCE_DIR", tmp_path):
            result = _format_outbox_message("raw content")
        # fallback_format() strips and cleans the raw content
        assert result == "raw content"


# ---------------------------------------------------------------------------
# handle_message (dispatch)
# ---------------------------------------------------------------------------

class TestHandleMessage:
    @patch("app.awake.handle_command")
    def test_dispatches_command(self, mock_cmd):
        handle_message("/stop")
        mock_cmd.assert_called_once_with("/stop")

    @patch("app.awake.handle_mission")
    def test_dispatches_mission(self, mock_mission):
        handle_message("implement dark mode")
        mock_mission.assert_called_once_with("implement dark mode")

    @patch("app.awake._run_in_worker")
    def test_dispatches_chat(self, mock_worker):
        handle_message("how are you?")
        mock_worker.assert_called_once_with(handle_chat, "how are you?")

    @patch("app.awake.handle_command")
    @patch("app.awake.handle_mission")
    @patch("app.awake._run_in_worker")
    def test_empty_message_ignored(self, mock_worker, mock_mission, mock_cmd):
        handle_message("")
        mock_cmd.assert_not_called()
        mock_mission.assert_not_called()
        mock_worker.assert_not_called()

    @patch("app.awake.handle_command")
    @patch("app.awake.handle_mission")
    @patch("app.awake._run_in_worker")
    def test_whitespace_only_ignored(self, mock_worker, mock_mission, mock_cmd):
        handle_message("   \n  ")
        mock_cmd.assert_not_called()
        mock_mission.assert_not_called()
        mock_worker.assert_not_called()

    @patch("app.awake.reset_flood_state")
    @patch("app.awake.handle_command")
    def test_resets_flood_state_on_command(self, mock_cmd, mock_reset):
        """Each user message resets flood protection so /help twice works."""
        handle_message("/help")
        mock_reset.assert_called_once()

    @patch("app.awake.reset_flood_state")
    @patch("app.awake._run_in_worker")
    def test_resets_flood_state_on_chat(self, mock_worker, mock_reset):
        handle_message("how are you?")
        mock_reset.assert_called_once()

    @patch("app.awake.reset_flood_state")
    @patch("app.awake.handle_command")
    @patch("app.awake.handle_mission")
    @patch("app.awake._run_in_worker")
    def test_no_flood_reset_on_empty(self, mock_worker, mock_mission, mock_cmd, mock_reset):
        """Empty messages skip flood reset (no message to respond to)."""
        handle_message("")
        mock_reset.assert_not_called()


# ---------------------------------------------------------------------------
# get_updates
# ---------------------------------------------------------------------------

class TestGetUpdates:
    @patch("app.messaging.get_messaging_provider")
    def test_returns_results(self, mock_get_provider):
        from app.messaging.base import Update
        mock_provider = MagicMock()
        mock_provider.poll_updates.return_value = [
            Update(update_id=1, raw_data={"update_id": 1})
        ]
        mock_get_provider.return_value = mock_provider
        result = get_updates()
        assert len(result) == 1
        assert result[0]["update_id"] == 1

    @patch("app.messaging.get_messaging_provider")
    def test_passes_offset(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.poll_updates.return_value = []
        mock_get_provider.return_value = mock_provider
        get_updates(offset=42)
        mock_provider.poll_updates.assert_called_once_with(42)

    @patch("app.messaging.get_messaging_provider")
    def test_handles_provider_error(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.poll_updates.return_value = []
        mock_get_provider.return_value = mock_provider
        result = get_updates()
        assert result == []

    @patch("app.messaging.get_messaging_provider")
    def test_handles_empty_results(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.poll_updates.return_value = []
        mock_get_provider.return_value = mock_provider
        result = get_updates()
        assert result == []


# ---------------------------------------------------------------------------
# check_config
# ---------------------------------------------------------------------------

class TestCheckConfig:
    def test_exits_without_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "")
        with patch("app.awake.BOT_TOKEN", ""), \
             patch("app.awake.CHAT_ID", "123"), \
             pytest.raises(SystemExit):
            check_config()

    def test_exits_without_chat_id(self, monkeypatch, tmp_path):
        with patch("app.awake.BOT_TOKEN", "token"), \
             patch("app.awake.CHAT_ID", ""), \
             pytest.raises(SystemExit):
            check_config()

    def test_exits_without_instance_dir(self, tmp_path):
        with patch("app.awake.BOT_TOKEN", "token"), \
             patch("app.awake.CHAT_ID", "123"), \
             patch("app.awake.INSTANCE_DIR", tmp_path / "nonexistent"), \
             pytest.raises(SystemExit):
            check_config()

    def test_passes_with_valid_config(self, tmp_path):
        inst = tmp_path / "instance"
        inst.mkdir()
        with patch("app.awake.BOT_TOKEN", "token"), \
             patch("app.awake.CHAT_ID", "123"), \
             patch("app.awake.INSTANCE_DIR", inst):
            check_config()  # Should not raise


# ---------------------------------------------------------------------------
# main() loop
# ---------------------------------------------------------------------------

class TestMainLoop:
    """Test the main polling loop behavior."""

    TEST_CHAT_ID = "123456789"

    @pytest.fixture(autouse=True)
    def mock_pid_manager(self):
        """Auto-mock PID file management for all main() tests."""
        with patch("app.pid_manager.acquire_pidfile") as mock_acquire, \
             patch("app.pid_manager.release_pidfile"):
            mock_acquire.return_value = MagicMock()
            yield

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)  # Break after first iteration
    def test_main_processes_updates(self, mock_sleep, mock_config, mock_updates,
                                    mock_handle, mock_flush, mock_heartbeat):
        """main() fetches updates, dispatches messages, flushes outbox, writes heartbeat."""
        from app.awake import main
        mock_updates.return_value = [
            {"update_id": 100, "message": {"text": "hello", "chat": {"id": int(self.TEST_CHAT_ID)}}}
        ]
        with pytest.raises(StopIteration):
            main()
        mock_config.assert_called_once()
        mock_updates.assert_called_once_with(None)
        mock_handle.assert_called_once_with("hello")
        mock_flush.assert_called_once()
        mock_heartbeat.assert_called()

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_main_ignores_wrong_chat_id(self, mock_sleep, mock_config, mock_updates,
                                         mock_handle, mock_flush, mock_heartbeat):
        """Messages from other chat IDs are ignored."""
        from app.awake import main
        mock_updates.return_value = [
            {"update_id": 100, "message": {"text": "hello", "chat": {"id": 999999}}}
        ]
        with pytest.raises(StopIteration):
            main()
        mock_handle.assert_not_called()

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep")
    def test_main_updates_offset(self, mock_sleep, mock_config, mock_updates,
                                  mock_handle, mock_flush, mock_heartbeat):
        """Offset advances to update_id + 1 after processing."""
        from app.awake import main
        test_chat_id = self.TEST_CHAT_ID
        call_count = [0]
        def side_effect(offset=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{"update_id": 42, "message": {"text": "hi", "chat": {"id": int(test_chat_id)}}}]
            raise StopIteration  # Stop on second get_updates call
        mock_updates.side_effect = side_effect

        with pytest.raises(StopIteration):
            main()
        assert mock_updates.call_count == 2
        mock_updates.assert_called_with(43)

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", return_value=[])
    @patch("app.awake.check_config")
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_main_empty_updates_still_flushes(self, mock_sleep, mock_config, mock_updates,
                                               mock_handle, mock_flush, mock_heartbeat):
        """Even with no updates, outbox is flushed and heartbeat written."""
        from app.awake import main
        with pytest.raises(StopIteration):
            main()
        mock_handle.assert_not_called()
        mock_flush.assert_called_once()
        mock_heartbeat.assert_called()

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_main_skips_updates_without_text(self, mock_sleep, mock_config, mock_updates,
                                              mock_handle, mock_flush, mock_heartbeat):
        """Updates without text field (e.g., photo, sticker) are ignored."""
        from app.awake import main
        mock_updates.return_value = [
            {"update_id": 100, "message": {"chat": {"id": int(self.TEST_CHAT_ID)}}}  # no text
        ]
        with pytest.raises(StopIteration):
            main()
        mock_handle.assert_not_called()

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", side_effect=KeyboardInterrupt)
    @patch("app.awake.check_config")
    def test_main_ctrl_c_exits_gracefully(self, mock_config, mock_updates,
                                           mock_handle, mock_flush, mock_heartbeat,
                                           capsys):
        """CTRL-C (KeyboardInterrupt) exits cleanly without traceback."""
        from app.awake import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Shutting down" in captured.err

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", return_value=[])
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=KeyboardInterrupt)
    def test_main_ctrl_c_during_sleep_exits_gracefully(self, mock_sleep, mock_config,
                                                        mock_updates, mock_handle,
                                                        mock_flush, mock_heartbeat,
                                                        capsys):
        """CTRL-C during sleep between polls also exits cleanly."""
        from app.awake import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Shutting down" in captured.err

    @patch("app.awake.clear_shutdown")
    @patch("app.awake.is_shutdown_requested", return_value=True)
    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", return_value=[])
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    def test_main_exits_on_shutdown_signal(self, mock_config, mock_updates,
                                            mock_handle, mock_flush,
                                            mock_heartbeat, mock_shutdown,
                                            mock_clear, capsys):
        """Bridge exits cleanly when /shutdown signal is detected."""
        from app.awake import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        mock_shutdown.assert_called()
        mock_clear.assert_called()
        captured = capsys.readouterr()
        assert "Shutdown requested" in captured.err

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", side_effect=KeyboardInterrupt)
    @patch("app.awake.check_config")
    def test_main_sets_pythonpath(self, mock_config, mock_updates,
                                  mock_handle, mock_flush, mock_heartbeat,
                                  tmp_path, monkeypatch):
        """main() sets PYTHONPATH to include koan/ package directory."""
        from app.awake import main
        monkeypatch.setattr("app.awake.KOAN_ROOT", tmp_path)
        monkeypatch.delenv("PYTHONPATH", raising=False)

        with pytest.raises(SystemExit):
            main()

        pythonpath = os.environ.get("PYTHONPATH", "")
        koan_dir = str(tmp_path / "koan")
        assert koan_dir in pythonpath.split(os.pathsep)

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", side_effect=KeyboardInterrupt)
    @patch("app.awake.check_config")
    def test_main_preserves_existing_pythonpath(self, mock_config, mock_updates,
                                                 mock_handle, mock_flush,
                                                 mock_heartbeat,
                                                 tmp_path, monkeypatch):
        """main() prepends koan/ to PYTHONPATH without losing existing entries."""
        from app.awake import main
        monkeypatch.setattr("app.awake.KOAN_ROOT", tmp_path)
        monkeypatch.setenv("PYTHONPATH", "/existing/path")

        with pytest.raises(SystemExit):
            main()

        pythonpath = os.environ.get("PYTHONPATH", "")
        parts = pythonpath.split(os.pathsep)
        koan_dir = str(tmp_path / "koan")
        assert koan_dir in parts
        assert "/existing/path" in parts

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", side_effect=KeyboardInterrupt)
    @patch("app.awake.check_config")
    def test_main_does_not_duplicate_pythonpath(self, mock_config, mock_updates,
                                                 mock_handle, mock_flush,
                                                 mock_heartbeat,
                                                 tmp_path, monkeypatch):
        """main() does not add koan/ to PYTHONPATH if already present."""
        from app.awake import main
        koan_dir = str(tmp_path / "koan")
        monkeypatch.setattr("app.awake.KOAN_ROOT", tmp_path)
        monkeypatch.setenv("PYTHONPATH", koan_dir)

        with pytest.raises(SystemExit):
            main()

        pythonpath = os.environ.get("PYTHONPATH", "")
        # Should not be duplicated
        assert pythonpath.count(koan_dir) == 1


# ---------------------------------------------------------------------------
# /pause command
# ---------------------------------------------------------------------------

class TestPauseCommand:
    @patch("app.command_handlers.send_telegram")
    def test_pause_creates_file(self, mock_send, tmp_path):
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/pause")
        assert (tmp_path / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "paused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers.send_telegram")
    def test_sleep_creates_file(self, mock_send, tmp_path):
        """The /sleep alias creates the pause file just like /pause."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/sleep")
        assert (tmp_path / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "paused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers.send_telegram")
    def test_pause_already_paused(self, mock_send, tmp_path):
        (tmp_path / ".koan-pause").write_text("PAUSE")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/pause")
        assert "already paused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers.send_telegram")
    def test_sleep_already_paused(self, mock_send, tmp_path):
        (tmp_path / ".koan-pause").write_text("PAUSE")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/sleep")
        assert "already paused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_clears_pause(self, mock_send, mock_alive, tmp_path):
        (tmp_path / ".koan-pause").write_text("PAUSE")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert not (tmp_path / ".koan-pause").exists()
        assert "unpaused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_pause_takes_priority_over_quota(self, mock_send, mock_alive, tmp_path):
        """If both pause and quota files exist, /resume clears pause first."""
        (tmp_path / ".koan-pause").write_text("PAUSE")
        (tmp_path / ".koan-quota-reset").write_text("resets 7pm\n0")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert not (tmp_path / ".koan-pause").exists()
        assert (tmp_path / ".koan-quota-reset").exists()
        assert "unpaused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_with_quota_reason(self, mock_send, mock_alive, tmp_path):
        """Resume cleans up pause file and reports quota reason."""
        (tmp_path / ".koan-pause").write_text("quota\n1234567890")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert not (tmp_path / ".koan-pause").exists()
        assert "quota" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_with_max_runs_reason(self, mock_send, mock_alive, tmp_path):
        """Resume cleans up pause file and reports max_runs reason."""
        (tmp_path / ".koan-pause").write_text("max_runs\n1234567890")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert not (tmp_path / ".koan-pause").exists()
        assert "max_runs" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_pause_without_reason(self, mock_send, mock_alive, tmp_path):
        """Resume with pause file but no reason file (manual /pause)."""
        (tmp_path / ".koan-pause").write_text("PAUSE")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        assert not (tmp_path / ".koan-pause").exists()
        # Should say "unpaused" without specific reason
        assert "unpaused" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers._reset_session_counters")
    @patch("app.command_handlers.send_telegram")
    def test_resume_quota_resets_session_counters(self, mock_send, mock_reset, mock_alive, tmp_path):
        """Resume from quota pause should reset internal session counters."""
        (tmp_path / ".koan-pause").write_text("quota\n9999999999\nresets 7pm")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        mock_reset.assert_called_once()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers._reset_session_counters")
    @patch("app.command_handlers.send_telegram")
    def test_resume_max_runs_does_not_reset_session(self, mock_send, mock_reset, mock_alive, tmp_path):
        """Resume from max_runs should NOT reset session counters."""
        (tmp_path / ".koan-pause").write_text("max_runs\n1234567890")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        mock_reset.assert_not_called()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers._reset_session_counters")
    @patch("app.command_handlers.send_telegram")
    def test_resume_manual_pause_does_not_reset_session(self, mock_send, mock_reset, mock_alive, tmp_path):
        """Resume from manual pause (no reason file) should NOT reset session counters."""
        (tmp_path / ".koan-pause").write_text("PAUSE")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_resume()
        mock_reset.assert_not_called()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_quota_message_includes_counter_info(self, mock_send, mock_alive, tmp_path):
        """Resume message from quota should mention that counters were cleared."""
        future_ts = str(int(time.time()) + 3600)
        (tmp_path / ".koan-pause").write_text(f"quota\n{future_ts}\nresets 7pm")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers._reset_session_counters"):
            handle_resume()
        msg = mock_send.call_args[0][0].lower()
        assert "counter" in msg or "clear" in msg

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers.send_telegram")
    def test_resume_quota_past_reset_time(self, mock_send, mock_alive, tmp_path):
        """Resume after reset time should confirm quota reset."""
        past_ts = str(int(time.time()) - 3600)  # 1h ago
        (tmp_path / ".koan-pause").write_text(f"quota\n{past_ts}\nresets 7pm")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers._reset_session_counters"):
            handle_resume()
        msg = mock_send.call_args[0][0].lower()
        assert "reset" in msg

    def test_status_shows_paused(self, tmp_path):
        """Status skill handler shows Paused when paused."""
        (tmp_path / ".koan-pause").write_text("PAUSE")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Paused" in status
        assert "/resume" in status

    def test_status_shows_paused_with_quota_reason(self, tmp_path):
        """Status shows Paused with quota reason."""
        (tmp_path / ".koan-pause").write_text("quota\n1234567890")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Paused" in status
        assert "quota" in status.lower()

    def test_status_shows_paused_with_max_runs_reason(self, tmp_path):
        """Status shows Paused with max_runs reason."""
        (tmp_path / ".koan-pause").write_text("max_runs\n1234567890")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Paused" in status
        assert "max runs" in status.lower()

    def test_status_shows_working_when_active(self, tmp_path):
        """Status shows Active when no pause/stop/passive."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Active" in status

    def test_status_shows_stopping(self, tmp_path):
        """Status shows Stopping when stop file exists."""
        (tmp_path / ".koan-stop").write_text("STOP")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Stopping" in status


# ---------------------------------------------------------------------------
# handle_chat — lite retry error distinction
# ---------------------------------------------------------------------------

class TestChatLiteRetryErrors:
    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_lite_retry_non_timeout_error(self, mock_run, mock_send, mock_tools,
                                           mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        """Non-timeout error on lite retry should say 'something went wrong', not 'timeout'."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 180),
            OSError("connection refused"),
        ]
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""), \
             patch("app.awake.CHAT_TIMEOUT", 180), \
             patch("app.awake.time.sleep"):
            handle_chat("complex question")
        assert "went wrong" in mock_send.call_args[0][0].lower()

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_lite_retry_timeout_says_timeout(self, mock_run, mock_send, mock_tools,
                                              mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        """Timeout on lite retry should still say 'timeout'."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 180),
            subprocess.TimeoutExpired("claude", 180),
        ]
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""), \
             patch("app.awake.CHAT_TIMEOUT", 180), \
             patch("app.awake.time.sleep"):
            handle_chat("complex question")
        assert "timeout" in mock_send.call_args[0][0].lower()

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_lite_retry_backoff_delay(self, mock_run, mock_send, mock_tools,
                                      mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        """Lite retry should sleep before retrying to let API pressure ease."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 180),
            MagicMock(stdout="OK reply", returncode=0),
        ]
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""), \
             patch("app.awake.CHAT_TIMEOUT", 180), \
             patch("app.awake.time.sleep") as mock_sleep:
            handle_chat("complex question")
        mock_sleep.assert_called_once_with(4)

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram")
    @patch("app.awake.subprocess.run")
    def test_lite_retry_uses_shorter_timeout(self, mock_run, mock_send, mock_tools,
                                              mock_tools_desc, mock_fmt, mock_hist, mock_save, tmp_path):
        """Lite retry should use CHAT_TIMEOUT//2 to avoid doubling user wait time."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 180),
            MagicMock(stdout="OK reply", returncode=0),
        ]
        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""), \
             patch("app.awake.CHAT_TIMEOUT", 180), \
             patch("app.awake.time.sleep"):
            handle_chat("complex question")
        # Second call (lite retry) should use timeout=90 (180//2)
        retry_call = mock_run.call_args_list[1]
        assert retry_call.kwargs["timeout"] == 90


class TestCleanChatResponse:
    """Test _clean_chat_response strips errors, markdown, and truncates."""

    def test_strips_max_turns_error(self):
        text = "Some reply\nError: Reached max turns (1)\nMore text"
        assert "max turns" not in _clean_chat_response(text)
        assert "Some reply" in _clean_chat_response(text)

    def test_strips_markdown(self):
        text = "**Bold** and ```code``` and __underline__ and ~~strike~~"
        result = _clean_chat_response(text)
        assert "**" not in result
        assert "```" not in result
        assert "__" not in result
        assert "~~" not in result

    def test_strips_headings(self):
        text = "## Heading\nContent"
        result = _clean_chat_response(text)
        assert "##" not in result
        assert "Content" in result

    def test_truncates_long_messages(self):
        text = "x" * 2500
        result = _clean_chat_response(text)
        assert len(result) <= 2000
        assert result.endswith("...")

    def test_empty_after_cleanup(self):
        text = "Error: Reached max turns (1)"
        assert _clean_chat_response(text) == ""

    def test_preserves_normal_text(self):
        text = "Tout va bien, j'ai fini le travail."
        assert _clean_chat_response(text) == text


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

class TestHandleHelp:
    @patch("app.command_handlers.send_telegram")
    def test_help_sends_grouped_overview(self, mock_send):
        _handle_help()
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "/help" in msg
        assert "/missions" in msg
        assert "/code" in msg
        assert "/status" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_mentions_mission_syntax(self, mock_send):
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "mission" in msg.lower()

    @patch("app.command_handlers.send_telegram")
    def test_help_mentions_group_navigation(self, mock_send):
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/help <group>" in msg

    @patch("app.command_handlers._handle_help")
    def test_handle_command_routes_help(self, mock_help):
        handle_command("/help")
        mock_help.assert_called_once()


# ---------------------------------------------------------------------------
# /usage
# ---------------------------------------------------------------------------

class TestHandleUsage:
    @patch("app.command_handlers.send_telegram")
    def test_handle_command_routes_usage_through_skill(self, mock_send, tmp_path):
        """Usage is routed through skill system (non-blocking, reads files only)."""
        instance = tmp_path / "instance"
        instance.mkdir()
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance):
            handle_command("/usage")
        mock_send.assert_called_once()
        assert "Quota" in mock_send.call_args[0][0] or "No quota" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Pause awareness in chat and status
# ---------------------------------------------------------------------------

class TestPauseAwareness:
    """Tests for pause state visibility in chat and status."""

    def test_status_shows_paused_at_top(self, tmp_path):
        """When paused, status shows Paused FIRST, not at the bottom."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n- fix bug\n\n## In Progress\n\n"
        )
        (tmp_path / ".koan-pause").write_text("PAUSE")

        status = _call_status_handler(tmp_path)

        lines = status.split("\n")
        paused_line_idx = next(i for i, l in enumerate(lines) if "Paused" in l)
        mission_line_idx = next((i for i, l in enumerate(lines) if "fix bug" in l), len(lines))
        assert paused_line_idx < mission_line_idx, "Paused status should appear before mission details"

    def test_status_shows_working_when_running(self, tmp_path):
        """When not paused, status shows Working."""
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
        )
        status = _call_status_handler(tmp_path)
        assert "Active" in status

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_chat_prompt_includes_pause_status_when_paused(
        self, mock_run, mock_send, mock_tools, mock_tools_desc, mock_fmt,
        mock_hist, mock_save, tmp_path
    ):
        """Chat prompt should include PAUSED status when .koan-pause exists."""
        from app.awake import _build_chat_prompt

        (tmp_path / ".koan-pause").write_text("PAUSE")
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n- fix bug\n\n## In Progress\n\n")

        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.MISSIONS_FILE", missions_file), \
             patch("app.awake.SOUL", "test soul"), \
             patch("app.awake.SUMMARY", ""):
            prompt = _build_chat_prompt("what are you doing?")

        # Prompt should mention pause status
        assert "PAUSED" in prompt or "⏸️" in prompt

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_chat_prompt_includes_running_status_when_active(
        self, mock_run, mock_send, mock_tools, mock_tools_desc, mock_fmt,
        mock_hist, mock_save, tmp_path
    ):
        """Chat prompt should include RUNNING status when not paused."""
        from app.awake import _build_chat_prompt

        # No .koan-pause file
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n- fix bug\n\n## In Progress\n\n")

        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.MISSIONS_FILE", missions_file), \
             patch("app.awake.SOUL", "test soul"), \
             patch("app.awake.SUMMARY", ""):
            prompt = _build_chat_prompt("what are you doing?")

        # Prompt should mention running status
        assert "RUNNING" in prompt or "▶️" in prompt


class TestChatToolsSecurity:
    """Tests verifying chat security (restricted tools)."""

    def test_chat_tools_excludes_bash_by_default(self):
        """Chat tools should NOT include Bash by default (prompt injection protection)."""
        from app.utils import get_chat_tools
        with patch("app.utils.load_config", return_value={}):
            tools = get_chat_tools()
        assert "Bash" not in tools
        assert "Edit" not in tools
        assert "Write" not in tools

    def test_mission_tools_includes_bash(self):
        """Mission tools should include Bash for code execution."""
        from app.utils import get_mission_tools
        with patch("app.utils.load_config", return_value={}):
            tools = get_mission_tools()
        assert "Bash" in tools

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="Read,Glob,Grep")  # Restricted!
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_handle_chat_uses_chat_tools_not_mission_tools(
        self, mock_run, mock_send, mock_tools, mock_tools_desc, mock_fmt,
        mock_hist, mock_save, tmp_path
    ):
        """handle_chat() should use get_chat_tools(), not get_mission_tools()."""
        mock_run.return_value = MagicMock(stdout="Response", returncode=0)

        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.PROJECT_PATH", ""), \
             patch("app.awake.CONVERSATION_HISTORY_FILE", tmp_path / "history.jsonl"), \
             patch("app.awake.SOUL", ""), \
             patch("app.awake.SUMMARY", ""):
            handle_chat("test message")

        # Verify the claude call uses the restricted tools
        call_args = mock_run.call_args[0][0]
        allowed_idx = call_args.index("--allowedTools")
        tools_arg = call_args[allowed_idx + 1]
        assert tools_arg == "Read,Glob,Grep"
        assert "Bash" not in tools_arg


# ---------------------------------------------------------------------------
# /mission command
# ---------------------------------------------------------------------------

class TestHandleMissionCommand:
    """Test /mission command — now routed through skill system."""

    @patch("app.command_handlers.send_telegram")
    def test_bare_mission_shows_usage(self, mock_send, tmp_path):
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/mission")
        msg = mock_send.call_args[0][0]
        assert "Usage" in msg

    @patch("app.command_handlers.send_telegram")
    def test_handle_command_routes_mission(self, mock_send, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/path/to/koan")]):
            handle_command("/mission fix the bug")
        msg = mock_send.call_args[0][0]
        assert "fix the bug" in msg


class TestMissionProjectAutoDetection:
    """Test /mission auto-detects project from first word."""

    @patch("app.command_handlers.send_telegram")
    def test_mission_skill_detects_project_from_first_word(self, mock_send, tmp_path):
        """'/mission koan fix bug' should detect 'koan' as project."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/path/to/koan")]):
            handle_command("/mission koan fix the bug")
        msg = mock_send.call_args[0][0]
        assert "fix the bug" in msg
        assert "project: koan" in msg
        content = missions_file.read_text()
        assert "[project:koan]" in content

    @patch("app.command_handlers.send_telegram")
    def test_mission_skill_explicit_tag_takes_precedence(self, mock_send, tmp_path):
        """'[project:web] koan fix bug' uses explicit tag, not first word."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/p1"), ("web", "/p2")]):
            handle_command("/mission [project:web] fix the bug")
        content = missions_file.read_text()
        assert "[project:web]" in content

    @patch("app.command_handlers.send_telegram")
    def test_mission_skill_asks_when_no_project_detected(self, mock_send, tmp_path):
        """When first word is not a project and multiple projects exist, ask."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/p1"), ("web", "/p2")]):
            handle_command("/mission fix the bug")
        msg = mock_send.call_args[0][0]
        assert "Which project" in msg
        # Verify project names are displayed properly (not tuples)
        assert "koan" in msg
        assert "('koan'" not in msg

    @patch("app.command_handlers.send_telegram")
    def test_mission_skill_single_project_no_ask(self, mock_send, tmp_path):
        """Single project: no need to ask or detect."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n")
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/p1")]):
            handle_command("/mission fix the bug")
        msg = mock_send.call_args[0][0]
        assert "fix the bug" in msg
        assert "Which project" not in msg


class TestHandleHelpIncludesMission:
    @patch("app.command_handlers.send_telegram")
    def test_help_mentions_mission_command(self, mock_send):
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/mission" in msg


# ---------------------------------------------------------------------------
# /log command
# ---------------------------------------------------------------------------

class TestHandleLog:
    """Test /log and /journal command handler (now via skill system)."""

    @patch("app.command_handlers.send_telegram")
    def test_log_project_today(self, mock_send, tmp_path):
        """'/log koan' shows today's journal for koan."""
        from datetime import date
        d = tmp_path / "journal" / date.today().strftime("%Y-%m-%d")
        d.mkdir(parents=True)
        (d / "koan.md").write_text("## Session 29\nDid work on /log command.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/log koan")
        msg = mock_send.call_args[0][0]
        assert "koan" in msg
        assert "Did work" in msg

    @patch("app.command_handlers.send_telegram")
    def test_log_no_args_all_projects(self, mock_send, tmp_path):
        """'/log' shows today's journal for all projects."""
        from datetime import date
        d = tmp_path / "journal" / date.today().strftime("%Y-%m-%d")
        d.mkdir(parents=True)
        (d / "koan.md").write_text("koan stuff")
        (d / "web-app.md").write_text("web-app stuff")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/log")
        msg = mock_send.call_args[0][0]
        assert "koan" in msg
        assert "web-app" in msg

    @patch("app.command_handlers.send_telegram")
    def test_log_yesterday(self, mock_send, tmp_path):
        """'/log koan yesterday' shows yesterday's journal."""
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        d = tmp_path / "journal" / yesterday
        d.mkdir(parents=True)
        (d / "koan.md").write_text("Yesterday's work.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/log koan yesterday")
        msg = mock_send.call_args[0][0]
        assert "Yesterday's work" in msg

    @patch("app.command_handlers.send_telegram")
    def test_log_no_journal_found(self, mock_send, tmp_path):
        """Shows 'no journal' when nothing exists."""
        (tmp_path / "journal").mkdir()
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/log koan")
        msg = mock_send.call_args[0][0]
        assert "No journal" in msg

    @patch("app.command_handlers.send_telegram")
    def test_journal_alias_works(self, mock_send, tmp_path):
        """'/journal' is an alias for '/log'."""
        from datetime import date
        d = tmp_path / "journal" / date.today().strftime("%Y-%m-%d")
        d.mkdir(parents=True)
        (d / "koan.md").write_text("koan journal content")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.KOAN_ROOT", tmp_path):
            handle_command("/journal koan")
        msg = mock_send.call_args[0][0]
        assert "koan" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_status_group_includes_journal(self, mock_send):
        """/help status group should include /journal (successor of /log)."""
        _handle_help_detail("status")
        msg = mock_send.call_args[0][0]
        assert "/journal" in msg


# ---------------------------------------------------------------------------
# /pr command (now via skill system)
# ---------------------------------------------------------------------------

class TestHandlePr:
    """Tests for /pr command routed through skill system."""

    def _make_pr_handler(self):
        """Load the PR skill handler module."""
        spec = importlib.util.spec_from_file_location(
            "pr_handler",
            str(Path(__file__).parent.parent / "skills" / "core" / "pr" / "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_no_args_shows_usage(self, tmp_path):
        mod = self._make_pr_handler()
        from app.skills import SkillContext
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="")
        result = mod.handle(ctx)
        assert "Usage" in result

    def test_invalid_url_shows_error(self, tmp_path):
        mod = self._make_pr_handler()
        from app.skills import SkillContext
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="not a url")
        result = mod.handle(ctx)
        assert "No valid GitHub PR URL" in result

    @patch("app.utils.get_known_projects", return_value=[])
    def test_no_matching_project(self, mock_projects, tmp_path):
        mod = self._make_pr_handler()
        from app.skills import SkillContext
        ctx = SkillContext(
            koan_root=tmp_path,
            instance_dir=tmp_path,
            args="https://github.com/sukria/unknown-repo/pull/1",
        )
        result = mod.handle(ctx)
        assert "Could not find local project" in result

    @patch("app.command_handlers.send_telegram")
    def test_handle_command_routes_pr(self, mock_send, tmp_path):
        """handle_command dispatches /pr through worker (skill has worker=true)."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers._run_in_worker_cb") as mock_worker:
            handle_command("/pr")
        # PR is a worker skill — should dispatch via _run_in_worker
        mock_worker.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_help_includes_pr(self, mock_send):
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/pr" in msg
# /language
# ---------------------------------------------------------------------------

class TestHandleLanguage:
    """Tests for /language command (now via skill system)."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.language_preference.get_language", return_value="")
    def test_bare_language_shows_usage(self, mock_get, mock_send, tmp_path):
        """Bare /language shows current state and usage."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/language")
        msg = mock_send.call_args[0][0]
        assert "No language override" in msg

    @patch("app.command_handlers.send_telegram")
    @patch("app.language_preference.set_language")
    def test_set_language(self, mock_set, mock_send, tmp_path):
        """Setting a language via skill."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/language english")
        mock_set.assert_called_once_with("english")
        assert "english" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    @patch("app.language_preference.reset_language")
    def test_reset_language(self, mock_reset, mock_send, tmp_path):
        """'reset' arg calls reset_language."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            handle_command("/language reset")
        mock_reset.assert_called_once()
        assert "reset" in mock_send.call_args[0][0].lower()

    @patch("app.command_handlers.send_telegram")
    def test_help_config_group_includes_language(self, mock_send):
        """/help config group should include /language."""
        _handle_help_detail("config")
        msg = mock_send.call_args[0][0]
        assert "/language" in msg


# ---------------------------------------------------------------------------
# Phase 2: Scoped command dispatch
# ---------------------------------------------------------------------------

class TestScopedDispatch:
    """Tests for /<scope>.<name> command routing."""

    @patch("app.command_handlers.send_telegram")
    def test_scoped_command_dispatches_to_skill(self, mock_send, tmp_path):
        """/<scope>.<name> should dispatch to a matching skill."""
        instance = tmp_path / "instance"
        instance.mkdir()
        # Create a custom skill in instance/skills/
        myskill_dir = instance / "skills" / "myproj" / "greet"
        myskill_dir.mkdir(parents=True)
        (myskill_dir / "SKILL.md").write_text(
            "---\nname: greet\nscope: myproj\ndescription: Greet\n"
            "commands:\n  - name: greet\n    description: Say hi\n"
            "handler: handler.py\n---\n"
        )
        (myskill_dir / "handler.py").write_text(
            "def handle(ctx): return f'Hello from {ctx.command_name}!'"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.bridge_state.INSTANCE_DIR", instance):
            _reset_registry()
            handle_command("/myproj.greet")
        mock_send.assert_called()
        assert "Hello from greet" in mock_send.call_args[0][0]
        _reset_registry()

    @patch("app.command_handlers.send_telegram")
    def test_unknown_scoped_command_rejected_without_llm(self, mock_send, tmp_path):
        """Unknown scoped command is rejected immediately — no LLM call."""
        instance = tmp_path / "instance"
        instance.mkdir()
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.bridge_state.INSTANCE_DIR", instance):
            _reset_registry()
            handle_command("/unknown.thing")
        msg = mock_send.call_args[0][0]
        assert "Unknown command" in msg
        assert "/help" in msg
        _reset_registry()

    @patch("app.command_handlers._run_in_worker_cb")
    @patch("app.command_handlers._handle_chat_cb")
    @patch("app.command_handlers.send_telegram")
    def test_unknown_scoped_command_does_not_call_llm(self, mock_send, mock_chat, mock_worker, tmp_path):
        """Unknown scoped /commands must NOT fall through to Claude chat."""
        instance = tmp_path / "instance"
        instance.mkdir()
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.bridge_state.INSTANCE_DIR", instance):
            _reset_registry()
            handle_command("/unknown.thing")
        mock_chat.assert_not_called()
        mock_worker.assert_not_called()
        _reset_registry()

    @patch("app.command_handlers.send_telegram")
    def test_custom_skill_command_name_differs_from_skill_name(self, mock_send, tmp_path):
        """Custom skill with command name ≠ skill name must dispatch correctly.

        When instance/skills/wp/refactor/SKILL.md has name: refactor but
        commands: [name: wp_refactor], /wp.wp_refactor should resolve via
        command name fallback in resolve_scoped_command.
        """
        instance = tmp_path / "instance"
        instance.mkdir()
        skill_dir = instance / "skills" / "wp" / "refactor"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: refactor\ndescription: WP refactoring\n"
            "commands:\n  - name: wp_refactor\n    description: Refactor WP\n"
            "handler: handler.py\n---\n"
        )
        (skill_dir / "handler.py").write_text(
            "def handle(ctx): return f'Refactored via {ctx.command_name}!'"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.bridge_state.INSTANCE_DIR", instance):
            _reset_registry()
            handle_command("/wp.wp_refactor")
        mock_send.assert_called()
        assert "Refactored via wp_refactor" in mock_send.call_args[0][0]
        _reset_registry()

    @patch("app.command_handlers.send_telegram")
    def test_custom_skill_plain_command_dispatch(self, mock_send, tmp_path):
        """Custom skill command can be invoked without scope prefix via /command_name."""
        instance = tmp_path / "instance"
        instance.mkdir()
        skill_dir = instance / "skills" / "wp" / "deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: Deploy tool\n"
            "commands:\n  - name: wp_deploy\n    description: Deploy WP\n"
            "handler: handler.py\n---\n"
        )
        (skill_dir / "handler.py").write_text(
            "def handle(ctx): return f'Deployed via {ctx.command_name}!'"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.bridge_state.INSTANCE_DIR", instance):
            _reset_registry()
            # Plain command name (no scope prefix) should work via _command_map
            handle_command("/wp_deploy")
        mock_send.assert_called()
        assert "Deployed via wp_deploy" in mock_send.call_args[0][0]
        _reset_registry()


# ---------------------------------------------------------------------------
# Phase 2: Worker dispatch via skill.worker field
# ---------------------------------------------------------------------------

class TestWorkerDispatch:
    """Tests for worker thread routing via skill.worker field."""

    @patch("app.command_handlers._run_in_worker_cb")
    @patch("app.command_handlers.send_telegram")
    def test_worker_skill_runs_in_worker_thread(self, mock_send, mock_worker, tmp_path):
        """Skills with worker=true should dispatch to worker thread."""
        from app.skills import Skill, SkillCommand
        skill = Skill(
            name="blocking",
            scope="core",
            worker=True,
            commands=[SkillCommand(name="blocking")],
        )
        _dispatch_skill(skill, "blocking", "")
        mock_worker.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_non_worker_skill_runs_inline(self, mock_send, tmp_path):
        """Skills without worker=true should execute inline."""
        from app.skills import Skill, SkillCommand
        skill = Skill(
            name="fast",
            scope="core",
            worker=False,
            prompt_body="Some result",
            commands=[SkillCommand(name="fast")],
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _dispatch_skill(skill, "fast", "")
        mock_send.assert_called_once_with("Some result")


# ---------------------------------------------------------------------------
# Phase 2: /skill listing format
# ---------------------------------------------------------------------------

class TestSkillListingFormat:
    """Tests for improved /skill listing with / prefix."""

    @patch("app.command_handlers.send_telegram")
    def test_skill_list_uses_slash_prefix(self, mock_send, tmp_path):
        """Non-core skills should be listed with /<scope>.<name> format."""
        instance = tmp_path / "instance"
        instance.mkdir()
        myskill_dir = instance / "skills" / "proj" / "deploy"
        myskill_dir.mkdir(parents=True)
        (myskill_dir / "SKILL.md").write_text(
            "---\nname: deploy\nscope: proj\ndescription: Deploy it\n"
            "commands:\n  - name: deploy\n    description: Run deploy\n---\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.bridge_state.INSTANCE_DIR", instance):
            _reset_registry()
            _handle_skill_command("")
        msg = mock_send.call_args[0][0]
        assert "/proj.deploy" in msg
        assert "/<scope>.<name>" in msg
        _reset_registry()

    @patch("app.command_handlers.send_telegram")
    def test_skill_scope_listing_core_uses_bare_prefix(self, mock_send, tmp_path):
        """Core skills listed via /skill core should show /command (no scope prefix)."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _reset_registry()
            _handle_skill_command("core")
        msg = mock_send.call_args[0][0]
        # Core commands should use bare /status not /core.status
        assert "/status" in msg
        assert "/core.status" not in msg
        _reset_registry()


# ---------------------------------------------------------------------------
# /help <command>
# ---------------------------------------------------------------------------

class TestHandleHelpCommand:
    """Tests for /help <command> — show usage for a specific command."""

    @patch("app.command_handlers.send_telegram")
    def test_help_command_with_usage(self, mock_send):
        """/help mission should show usage from SKILL.md."""
        _handle_help_command("mission")
        msg = mock_send.call_args[0][0]
        assert "/mission" in msg
        assert "Usage:" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_command_without_usage(self, mock_send):
        """/help status should show 'No usage defined'."""
        _handle_help_command("status")
        msg = mock_send.call_args[0][0]
        assert "/status" in msg
        assert "No usage defined" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_command_unknown(self, mock_send):
        """/help nonexistent should show unknown command."""
        _handle_help_command("nonexistent")
        msg = mock_send.call_args[0][0]
        assert "Unknown command" in msg
        assert "/nonexistent" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_command_with_slash_prefix(self, mock_send):
        """/help /mission should work (strip leading /) — routed via _handle_help_detail."""
        _handle_help_detail("/mission")
        msg = mock_send.call_args[0][0]
        # "missions" group matches after stripping /, so we get the group view
        assert "Missions" in msg or "/mission" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_command_alias(self, mock_send):
        """/help st should resolve to /status via alias."""
        _handle_help_command("st")
        msg = mock_send.call_args[0][0]
        assert "/status" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_command_shows_description(self, mock_send):
        """/help mission should include the command description."""
        _handle_help_command("mission")
        msg = mock_send.call_args[0][0]
        # The description should be present
        assert "mission" in msg.lower()

    @patch("app.command_handlers.send_telegram")
    def test_help_command_shows_aliases(self, mock_send):
        """/help cancel should show aliases if any."""
        _handle_help_command("cancel")
        msg = mock_send.call_args[0][0]
        assert "/cancel" in msg

    @patch("app.command_handlers.send_telegram")
    def test_help_command_case_insensitive(self, mock_send):
        """/help MISSION should work case-insensitively via _handle_help_detail."""
        _handle_help_detail("MISSION")
        msg = mock_send.call_args[0][0]
        # "missions" group matches after lowercasing, so we get group or command view
        assert "mission" in msg.lower()

    def test_handle_command_routes_help_with_args(self):
        """handle_command('/help mission') should call _handle_help_detail."""
        with patch("app.command_handlers._handle_help_detail") as mock_help_detail:
            handle_command("/help mission")
            mock_help_detail.assert_called_once_with("mission")

    def test_handle_command_routes_help_without_args(self):
        """handle_command('/help') should call _handle_help, not _handle_help_command."""
        with patch("app.command_handlers._handle_help") as mock_help:
            handle_command("/help")
            mock_help.assert_called_once()


class TestHelpNoInlineUsage:
    """Tests that /help list no longer shows inline usage lines."""

    @patch("app.command_handlers.send_telegram")
    def test_help_does_not_show_inline_usage(self, mock_send):
        """/help should not show 'Usage:' lines inline (moved to /help <cmd>)."""
        _handle_help()
        msg = mock_send.call_args[0][0]
        # The main help list should mention /help <command> as a hint
        assert "/help <command>" in msg
        # Inline usage lines like "  /mission <description>..." should not appear
        lines = msg.split("\n")
        usage_lines = [l for l in lines if l.startswith("  /")]
        assert len(usage_lines) == 0, f"Found inline usage lines: {usage_lines}"

    @patch("app.command_handlers.send_telegram")
    def test_help_mentions_help_command_hint(self, mock_send):
        """/help should suggest using /help <command> for details."""
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/help <command>" in msg


# ---------------------------------------------------------------------------
# Skill management commands
# ---------------------------------------------------------------------------

class TestSkillInstallCommand:
    """Tests for /skill install integration in command_handlers.py."""

    @patch("app.command_handlers.send_telegram")
    def test_install_no_args_shows_usage(self, mock_send):
        _handle_skill_install("")
        msg = mock_send.call_args[0][0]
        assert "Usage" in msg
        assert "install" in msg

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers._reset_registry")
    @patch("app.skill_manager.install_skill_source")
    def test_install_success(self, mock_install, mock_reset, mock_send, tmp_path):
        mock_install.return_value = (True, "Installed 3 skills as scope 'ops'.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_install("myorg/koan-skills-ops")
        mock_install.assert_called_once()
        mock_reset.assert_called_once()
        assert "Installed" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    @patch("app.skill_manager.install_skill_source")
    def test_install_failure_no_reset(self, mock_install, mock_send, tmp_path):
        mock_install.return_value = (False, "Scope 'core' is reserved.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_install("myorg/repo core")
        assert "reserved" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    @patch("app.skill_manager.install_skill_source")
    def test_install_with_scope_and_ref(self, mock_install, mock_send, tmp_path):
        mock_install.return_value = (True, "Installed 1 skill.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_install("myorg/repo myteam --ref=v1.0.0")
        call_kwargs = mock_install.call_args
        assert call_kwargs[1]["scope"] == "myteam"
        assert call_kwargs[1]["ref"] == "v1.0.0"


class TestSkillUpdateCommand:
    """Tests for /skill update integration in command_handlers.py."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers._reset_registry")
    @patch("app.skill_manager.update_skill_source")
    def test_update_specific_scope(self, mock_update, mock_reset, mock_send, tmp_path):
        mock_update.return_value = (True, "Updated 'ops' (3 skills).")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_update("ops")
        mock_update.assert_called_once_with(tmp_path, "ops")
        mock_reset.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers._reset_registry")
    @patch("app.skill_manager.update_all_sources")
    def test_update_all(self, mock_update_all, mock_reset, mock_send, tmp_path):
        mock_update_all.return_value = (True, "All updated.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_update("")
        mock_update_all.assert_called_once()


class TestSkillRemoveCommand:
    """Tests for /skill remove integration in command_handlers.py."""

    @patch("app.command_handlers.send_telegram")
    def test_remove_no_args_shows_usage(self, mock_send):
        _handle_skill_remove("")
        assert "Usage" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers._reset_registry")
    @patch("app.skill_manager.remove_skill_source")
    def test_remove_success(self, mock_remove, mock_reset, mock_send, tmp_path):
        mock_remove.return_value = (True, "Removed skill source 'ops'.")
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_remove("ops")
        mock_remove.assert_called_once()
        mock_reset.assert_called_once()


class TestSkillSourcesCommand:
    """Tests for /skill sources integration in command_handlers.py."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.skill_manager.list_sources")
    def test_sources(self, mock_list, mock_send, tmp_path):
        mock_list.return_value = "No external skill sources installed."
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_sources()
        mock_list.assert_called_once()
        mock_send.assert_called_once()


class TestSkillCommandRouting:
    """Tests that /skill routes to management subcommands."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.skill_manager.list_sources")
    def test_skill_sources_routing(self, mock_list, mock_send, tmp_path):
        """'/skill sources' routes to list_sources."""
        mock_list.return_value = "No sources."
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_command("sources")
        mock_list.assert_called_once()

    @patch("app.command_handlers.send_telegram")
    def test_skill_install_routing(self, mock_send, tmp_path):
        """'/skill install' with no args shows usage."""
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_command("install")
        assert "Usage" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    def test_skill_remove_routing(self, mock_send, tmp_path):
        """'/skill remove' with no args shows usage."""
        with patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _handle_skill_command("remove")
        assert "Usage" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    def test_skill_no_args_includes_install_hint(self, mock_send, tmp_path):
        """'/skill' with no extra skills shows install hint."""
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path):
            _reset_registry()
            _handle_skill_command("")
        msg = mock_send.call_args[0][0]
        assert "install" in msg.lower()
        _reset_registry()


# ---------------------------------------------------------------------------
# Test: missions.md read protection in _build_chat_prompt
# ---------------------------------------------------------------------------

class TestBuildChatPromptMissionsReadProtection:
    """Tests that _build_chat_prompt handles OSError on missions.md read."""

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_missions_read_oserror_does_not_crash(
        self, mock_run, mock_send, mock_tools, mock_tools_desc, mock_fmt,
        mock_hist, mock_save, tmp_path
    ):
        """_build_chat_prompt should not crash if missions.md raises OSError."""
        from app.awake import _build_chat_prompt

        # Create a mock MISSIONS_FILE that exists() returns True but read_text() raises
        mock_missions = MagicMock()
        mock_missions.exists.return_value = True
        mock_missions.read_text.side_effect = OSError("locked")

        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.MISSIONS_FILE", mock_missions), \
             patch("app.awake.SOUL", "test soul"), \
             patch("app.awake.SUMMARY", ""):
            # Should not raise
            prompt = _build_chat_prompt("hello")

        # Prompt should still be built (missions context will be empty)
        assert isinstance(prompt, str)
        assert len(prompt) > 0


class TestBridgeExceptionResilience:
    """Bug fix: unhandled exceptions in handle_message must not crash the bridge."""

    TEST_CHAT_ID = "123456789"

    @pytest.fixture(autouse=True)
    def mock_pid_manager(self):
        with patch("app.pid_manager.acquire_pidfile") as mock_acquire, \
             patch("app.pid_manager.release_pidfile"):
            mock_acquire.return_value = MagicMock()
            yield

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.send_telegram")
    @patch("app.awake.handle_message", side_effect=RuntimeError("unexpected bug"))
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_exception_in_handle_message_does_not_crash_bridge(
        self, mock_sleep, mock_config, mock_updates, mock_handle,
        mock_send, mock_flush, mock_heartbeat
    ):
        """If handle_message raises, the bridge logs and continues."""
        from app.awake import main
        mock_updates.return_value = [
            {"update_id": 100, "message": {"text": "/bad", "chat": {"id": int(self.TEST_CHAT_ID)}}}
        ]
        # Should stop at StopIteration (time.sleep), NOT at RuntimeError
        with pytest.raises(StopIteration):
            main()
        mock_handle.assert_called_once_with("/bad")
        # Error notification sent to user
        mock_send.assert_called_once()
        assert "RuntimeError" in mock_send.call_args[0][0]

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.send_telegram", side_effect=Exception("telegram down"))
    @patch("app.awake.handle_message", side_effect=RuntimeError("bug"))
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_exception_in_error_notification_also_swallowed(
        self, mock_sleep, mock_config, mock_updates, mock_handle,
        mock_send, mock_flush, mock_heartbeat
    ):
        """If both handle_message AND the error notification fail, bridge still survives."""
        from app.awake import main
        mock_updates.return_value = [
            {"update_id": 100, "message": {"text": "/bad", "chat": {"id": int(self.TEST_CHAT_ID)}}}
        ]
        with pytest.raises(StopIteration):
            main()
        # Bridge survived both exceptions


class TestBridgeInfrastructureResilience:
    """Infrastructure calls (get_updates, flush_outbox, write_heartbeat) must not crash the bridge."""

    TEST_CHAT_ID = "123456789"

    @pytest.fixture(autouse=True)
    def mock_pid_manager(self):
        with patch("app.pid_manager.acquire_pidfile") as mock_acquire, \
             patch("app.pid_manager.release_pidfile"):
            mock_acquire.return_value = MagicMock()
            yield

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates", side_effect=ConnectionError("network down"))
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=[None, StopIteration])
    def test_get_updates_exception_does_not_crash_bridge(
        self, mock_sleep, mock_config, mock_updates, mock_handle,
        mock_flush, mock_heartbeat, capsys
    ):
        """If get_updates raises, bridge logs error and retries next iteration."""
        from app.awake import main
        with pytest.raises(StopIteration):
            main()
        # handle_message never called (no updates received)
        mock_handle.assert_not_called()
        # flush_outbox and heartbeat NOT called when get_updates fails
        # (continue skips to next iteration)
        captured = capsys.readouterr()
        assert "get_updates failed" in captured.err

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async", side_effect=OSError("disk full"))
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_flush_outbox_exception_does_not_crash_bridge(
        self, mock_sleep, mock_config, mock_updates, mock_handle,
        mock_flush, mock_heartbeat, capsys
    ):
        """If flush_outbox raises, bridge logs error and continues."""
        from app.awake import main
        mock_updates.return_value = []
        with pytest.raises(StopIteration):
            main()
        mock_flush.assert_called_once()
        # Heartbeat still called despite flush failure
        mock_heartbeat.assert_called()
        captured = capsys.readouterr()
        assert "flush_outbox failed" in captured.err

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async")
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_write_heartbeat_exception_does_not_crash_bridge(
        self, mock_sleep, mock_config, mock_updates, mock_handle,
        mock_flush, mock_heartbeat, capsys
    ):
        """If write_heartbeat raises in the loop, bridge logs error and continues."""
        from app.awake import main
        mock_updates.return_value = []
        # First call succeeds (startup), second call (loop) fails
        mock_heartbeat.side_effect = [None, PermissionError("read-only fs")]
        with pytest.raises(StopIteration):
            main()
        mock_flush.assert_called_once()
        assert mock_heartbeat.call_count == 2
        captured = capsys.readouterr()
        assert "write_heartbeat failed" in captured.err

    @patch("app.awake.write_heartbeat")
    @patch("app.awake._flush_outbox_async", side_effect=OSError("flush boom"))
    @patch("app.awake.handle_message")
    @patch("app.awake.get_updates")
    @patch("app.awake.check_config")
    @patch("app.awake.CHAT_ID", TEST_CHAT_ID)
    @patch("app.awake.time.sleep", side_effect=StopIteration)
    def test_multiple_infrastructure_failures_do_not_crash(
        self, mock_sleep, mock_config, mock_updates, mock_handle,
        mock_flush, mock_heartbeat, capsys
    ):
        """Both flush_outbox and write_heartbeat can fail without crashing."""
        from app.awake import main
        mock_updates.return_value = []
        # First call succeeds (startup), second call (loop) fails
        mock_heartbeat.side_effect = [None, RuntimeError("heartbeat boom")]
        with pytest.raises(StopIteration):
            main()
        captured = capsys.readouterr()
        assert "flush_outbox failed" in captured.err
        assert "write_heartbeat failed" in captured.err


class TestAsyncOutboxFlush:
    """flush_outbox runs in a background thread to avoid blocking Telegram polling."""

    @pytest.fixture(autouse=True)
    def reset_outbox_thread(self):
        import app.awake as awake_mod

        mgr = awake_mod._outbox_mgr
        mgr._thread = None
        yield
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)
        mgr._thread = None

    def test_flush_outbox_async_runs_in_thread(self):
        """_flush_outbox_async spawns a background thread for flush_outbox."""
        import app.awake as awake_mod

        mgr = awake_mod._outbox_mgr
        with patch.object(mgr, "flush") as mock_flush:
            _flush_outbox_async()
            # Wait for the thread to complete
            mgr._thread.join(timeout=2)
            mock_flush.assert_called_once()

    def test_flush_outbox_async_skips_if_already_running(self):
        """If a flush is already in progress, the next call is a no-op."""
        import threading
        import app.awake as awake_mod

        mgr = awake_mod._outbox_mgr
        # Simulate a long-running flush
        barrier = threading.Event()

        def slow_flush():
            barrier.wait(timeout=5)

        with patch.object(mgr, "flush", side_effect=slow_flush) as mock_flush:
            _flush_outbox_async()  # Starts the slow flush
            _flush_outbox_async()  # Should skip (thread still alive)
            barrier.set()
            mgr._thread.join(timeout=2)

        mock_flush.assert_called_once()

    def test_flush_outbox_async_catches_exceptions(self, capsys):
        """Exceptions in flush_outbox are caught and logged, not propagated."""
        import app.awake as awake_mod

        mgr = awake_mod._outbox_mgr
        with patch.object(mgr, "flush", side_effect=RuntimeError("boom")):
            _flush_outbox_async()
            mgr._thread.join(timeout=2)

        captured = capsys.readouterr()
        assert "Background flush_outbox failed" in captured.err

    def test_flush_outbox_async_allows_retry_after_completion(self):
        """After a flush completes, the next call spawns a new thread."""
        import app.awake as awake_mod

        mgr = awake_mod._outbox_mgr
        call_count = 0

        def counting_flush():
            nonlocal call_count
            call_count += 1

        with patch.object(mgr, "flush", side_effect=counting_flush):
            _flush_outbox_async()
            mgr._thread.join(timeout=2)
            _flush_outbox_async()
            mgr._thread.join(timeout=2)

        assert call_count == 2


# ---------------------------------------------------------------------------
# Test: hard text truncation when lite mode still exceeds MAX_PROMPT_CHARS
# ---------------------------------------------------------------------------

class TestBuildChatPromptHardTruncation:
    """Tests that _build_chat_prompt truncates user text as last resort."""

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_truncates_long_message_in_lite_mode(
        self, mock_run, mock_send, mock_tools, mock_tools_desc, mock_fmt,
        mock_hist, mock_save, tmp_path
    ):
        """When lite=True and prompt still exceeds 12k, user text is truncated."""
        from app.awake import _build_chat_prompt

        # Create a very long user message that will exceed 12k even in lite mode
        long_text = "x" * 15000

        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.MISSIONS_FILE", tmp_path / "missions.md"), \
             patch("app.awake.SOUL", "test soul"), \
             patch("app.awake.SUMMARY", ""):
            prompt = _build_chat_prompt(long_text, lite=True)

        # Prompt must be at or under the cap
        assert len(prompt) <= 12000, f"Prompt is {len(prompt)} chars, expected <= 12000"
        # The truncation marker should be present
        assert "[truncated]" in prompt

    @patch("app.awake.save_conversation_message")
    @patch("app.awake.load_recent_history", return_value=[])
    @patch("app.awake.format_conversation_history", return_value="")
    @patch("app.awake.get_tools_description", return_value="")
    @patch("app.awake.get_chat_tools", return_value="")
    @patch("app.awake.send_telegram", return_value=True)
    @patch("app.awake.subprocess.run")
    def test_short_message_not_truncated(
        self, mock_run, mock_send, mock_tools, mock_tools_desc, mock_fmt,
        mock_hist, mock_save, tmp_path
    ):
        """Short messages should not be truncated."""
        from app.awake import _build_chat_prompt

        short_text = "hello, how are you?"

        with patch("app.awake.INSTANCE_DIR", tmp_path), \
             patch("app.awake.KOAN_ROOT", tmp_path), \
             patch("app.awake.MISSIONS_FILE", tmp_path / "missions.md"), \
             patch("app.awake.SOUL", "test soul"), \
             patch("app.awake.SUMMARY", ""):
            prompt = _build_chat_prompt(short_text, lite=True)

        assert "[truncated]" not in prompt
        assert short_text in prompt
