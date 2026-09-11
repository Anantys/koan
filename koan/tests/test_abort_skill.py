"""Tests for the /abort core skill -- abort current in-progress mission."""

import signal
from unittest.mock import patch

import pytest

from app.skills import SkillContext


class TestAbortHandler:
    """Test the abort skill handler directly."""

    def _make_ctx(self, tmp_path, args=""):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance_dir,
            command_name="abort",
            args=args,
        )

    def test_creates_abort_signal_file(self, tmp_path):
        from skills.core.abort.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        abort_file = tmp_path / ".koan-abort"
        assert abort_file.exists()
        assert "abort" in abort_file.read_text().lower()
        assert "Abort requested" in result

    def test_response_mentions_failed(self, tmp_path):
        from skills.core.abort.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "Failed" in result

    def test_overwrites_existing_abort_file(self, tmp_path):
        from skills.core.abort.handler import handle

        abort_file = tmp_path / ".koan-abort"
        abort_file.write_text("old")
        ctx = self._make_ctx(tmp_path)
        handle(ctx)
        assert abort_file.exists()

    def test_sends_sigusr1_to_runner_when_pid_known(self, tmp_path):
        """The handler should signal the runner immediately, not just write a file.

        Without the signal, /abort sits idle for up to 30 s while ``proc.wait``
        polls the abort file. This test would fail against the original
        file-only implementation.
        """
        from skills.core.abort import handler as abort_handler

        ctx = self._make_ctx(tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=4242) as mock_check, \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill") as mock_kill:
            abort_handler.handle(ctx)

        assert mock_check.call_args[0][1] == "run"
        mock_kill.assert_called_once_with(4242, signal.SIGUSR1)

    def test_skips_signal_when_runner_not_running(self, tmp_path):
        """No PID file → no os.kill call. File-based fallback still works."""
        from skills.core.abort import handler as abort_handler

        ctx = self._make_ctx(tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("os.kill") as mock_kill:
            abort_handler.handle(ctx)

        mock_kill.assert_not_called()
        # Abort file is still written so a runner that starts mid-flight
        # picks up the abort on its next poll.
        assert (tmp_path / ".koan-abort").exists()

    def test_skips_signal_when_pid_does_not_belong_to_runner(self, tmp_path):
        """PID-reuse guard: refuse to signal an unrelated process.

        If the runner died and Linux recycled its PID, the cmdline check
        returns False and we must not send SIGUSR1 — its default disposition
        would terminate whatever inherited the PID.
        """
        from skills.core.abort import handler as abort_handler

        ctx = self._make_ctx(tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("app.pid_manager._cmdline_matches", return_value=False), \
             patch("os.kill") as mock_kill:
            abort_handler.handle(ctx)

        mock_kill.assert_not_called()
        # File fallback still triggers abort on the next poll cycle.
        assert (tmp_path / ".koan-abort").exists()

    def test_reply_says_when_signal_was_not_delivered(self, tmp_path):
        """Undelivered SIGUSR1 must not be reported as an immediate abort."""
        from skills.core.abort import handler as abort_handler

        ctx = self._make_ctx(tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.run_log.log") as mock_log:
            result = abort_handler.handle(ctx)

        assert "abort-file poll" in result
        assert mock_log.called

    def test_signal_skipped_when_cmdline_is_unrelated(self, tmp_path, monkeypatch):
        """A recycled PID whose cmdline is not run.py must not be signalled."""
        from skills.core.abort import handler as abort_handler

        ctx = self._make_ctx(tmp_path)

        def fake_read_bytes(self):
            return b"/usr/sbin/cron\x00-f\x00"

        monkeypatch.setattr("pathlib.Path.read_bytes", fake_read_bytes)
        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("os.kill") as mock_kill:
            abort_handler.handle(ctx)

        mock_kill.assert_not_called()

    def test_signal_failure_does_not_raise(self, tmp_path):
        """If the runner died between PID lookup and kill, swallow the error.

        The user still gets a confirmation and the file remains as a fallback.
        """
        from skills.core.abort import handler as abort_handler

        ctx = self._make_ctx(tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=99999), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill", side_effect=ProcessLookupError):
            # Must not raise
            result = abort_handler.handle(ctx)

        assert "Abort requested" in result
        assert (tmp_path / ".koan-abort").exists()


class TestRunSigusr1Handler:
    """Test the SIGUSR1 handler installed by run.main_loop()."""

    def test_handler_kills_running_claude_and_marks_aborted(self, tmp_path, monkeypatch):
        """SIGUSR1 should kill the active subprocess group and flag the mission as aborted."""
        from app import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / ".koan-abort").write_text("abort")

        # Fake a live Claude subprocess.
        class FakeProc:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None  # still running

        proc = FakeProc()
        monkeypatch.setattr(run._sig, "claude_proc", proc)
        monkeypatch.setattr(run, "_last_mission_aborted", False)

        killed = []

        def fake_kill(p):
            killed.append(p)
            proc.killed = True

        monkeypatch.setattr(run, "_kill_process_group", fake_kill)

        run._on_sigusr1(signal.SIGUSR1, None)

        assert killed == [proc]
        assert run._last_mission_aborted is True
        # Abort file is consumed by the handler so the file-based fallback
        # in proc.wait's poll loop doesn't double-fire.
        assert not (tmp_path / ".koan-abort").exists()

    def test_handler_noop_when_no_subprocess(self, tmp_path, monkeypatch):
        """SIGUSR1 with no active mission must not crash or touch flags."""
        from app import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setattr(run._sig, "claude_proc", None)
        monkeypatch.setattr(run, "_last_mission_aborted", False)
        monkeypatch.setattr(run, "_kill_process_group", lambda p: pytest.fail("should not kill"))

        run._on_sigusr1(signal.SIGUSR1, None)

        assert run._last_mission_aborted is False

    def test_handler_noop_when_subprocess_already_exited(self, tmp_path, monkeypatch):
        """A dead subprocess.poll() returning non-None must short-circuit."""
        from app import run

        class DeadProc:
            def poll(self):
                return 0

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setattr(run._sig, "claude_proc", DeadProc())
        monkeypatch.setattr(run, "_last_mission_aborted", False)
        monkeypatch.setattr(run, "_kill_process_group", lambda p: pytest.fail("should not kill"))

        run._on_sigusr1(signal.SIGUSR1, None)

        assert run._last_mission_aborted is False


class TestAbortSignalConstant:
    """Test that ABORT_FILE is properly defined in signals."""

    def test_abort_file_constant_exists(self):
        from app.signals import ABORT_FILE

        assert ABORT_FILE == ".koan-abort"


class TestAbortSkillRegistry:
    """Test that /abort is discoverable in the skill registry."""

    def test_abort_resolves_in_registry(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("abort")
        assert skill is not None
        assert skill.name == "abort"

    def test_abort_has_missions_group(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("abort")
        assert skill is not None
        assert skill.group == "missions"


class TestAbortCommandRouting:
    """Test that /abort routes correctly via awake command handling."""

    @patch("app.command_handlers.send_telegram")
    def test_abort_routes_via_skill(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path / "instance"):
            (tmp_path / "instance").mkdir(exist_ok=True)
            handle_command("/abort")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "Abort requested" in output

    @patch("app.command_handlers.send_telegram")
    def test_abort_appears_in_help_missions(self, mock_send, tmp_path):
        """Verify /abort is included in /help missions group output."""
        from app.command_handlers import _handle_help_detail

        _handle_help_detail("missions")
        mock_send.assert_called_once()
        help_text = mock_send.call_args[0][0]
        assert "/abort" in help_text
