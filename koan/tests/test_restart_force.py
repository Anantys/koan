"""Tests for /restart --force — restart without waiting for the current mission.

Plain /restart is polite: the runner finishes its mission first. --force marks
the request on disk and signals the runner (SIGUSR2) so it kills the in-flight
mission and exits with RESTART_EXIT_CODE immediately.
"""

import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from app.restart_manager import (
    RESTART_BRIDGE_FILE,
    RESTART_EXIT_CODE,
    RESTART_RUN_FILE,
    is_force_restart,
    request_restart,
)
from app.skills import SkillContext


class TestForceMarker:
    def test_plain_request_is_not_forced(self, tmp_path):
        request_restart(str(tmp_path))
        assert is_force_restart(str(tmp_path), target="run") is False
        assert is_force_restart(str(tmp_path), target="bridge") is False

    def test_forced_request_marks_both_consumers(self, tmp_path):
        request_restart(str(tmp_path), force=True)
        assert is_force_restart(str(tmp_path), target="run") is True
        assert is_force_restart(str(tmp_path), target="bridge") is True

    def test_forced_marker_still_carries_timestamp(self, tmp_path):
        request_restart(str(tmp_path), force=True)
        assert "restart requested at" in (tmp_path / RESTART_RUN_FILE).read_text()

    def test_no_marker_is_not_forced(self, tmp_path):
        assert is_force_restart(str(tmp_path), target="run") is False

    def test_plain_request_overwrites_a_previous_forced_marker(self, tmp_path):
        request_restart(str(tmp_path), force=True)
        request_restart(str(tmp_path))
        assert is_force_restart(str(tmp_path), target="run") is False

    def test_marker_older_than_since_is_ignored(self, tmp_path):
        """A leftover marker from a previous incarnation must not force."""
        request_restart(str(tmp_path), force=True)
        mtime = os.path.getmtime(tmp_path / RESTART_RUN_FILE)
        assert is_force_restart(str(tmp_path), "run", since=mtime + 1) is False

    def test_marker_newer_than_since_still_forces(self, tmp_path):
        request_restart(str(tmp_path), force=True)
        mtime = os.path.getmtime(tmp_path / RESTART_RUN_FILE)
        assert is_force_restart(str(tmp_path), "run", since=mtime - 1) is True

    def test_unknown_target_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            is_force_restart(str(tmp_path), target="nope")

    def test_unreadable_marker_is_logged(self, tmp_path, monkeypatch):
        """An unreadable marker silently disables the fallback — log it."""
        import app.restart_manager as rm

        request_restart(str(tmp_path), force=True)
        monkeypatch.setattr(rm, "_force_read_error_logged", False)
        with patch("builtins.open", side_effect=PermissionError("EACCES")), \
             patch("app.run_log.log") as mock_log:
            assert is_force_restart(str(tmp_path), target="run") is False
        assert mock_log.called
        assert mock_log.call_args[0][0] == "error"

    def test_missing_marker_is_not_logged(self, tmp_path, monkeypatch):
        """Absence is the normal case — no error noise every poll tick."""
        import app.restart_manager as rm

        monkeypatch.setattr(rm, "_force_read_error_logged", False)
        with patch("app.run_log.log") as mock_log:
            assert is_force_restart(str(tmp_path), target="run") is False
        mock_log.assert_not_called()


class TestRestartHandler:
    def _ctx(self, tmp_path, args=""):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)
        return SkillContext(
            koan_root=tmp_path, instance_dir=instance_dir,
            command_name="restart", args=args,
        )

    def test_plain_restart_does_not_signal_runner(self, tmp_path):
        from skills.core.restart.handler import handle

        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill") as mock_kill:
            result = handle(self._ctx(tmp_path))

        mock_kill.assert_not_called()
        assert is_force_restart(str(tmp_path), target="run") is False
        assert "Restart requested" in result

    @pytest.mark.parametrize("args", ["--force", "-f", "force", "  --FORCE  "])
    def test_force_flag_signals_runner_with_sigusr2(self, tmp_path, args):
        from skills.core.restart.handler import handle

        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill") as mock_kill:
            result = handle(self._ctx(tmp_path, args))

        mock_kill.assert_called_once_with(4242, signal.SIGUSR2)
        assert is_force_restart(str(tmp_path), target="run") is True
        assert is_force_restart(str(tmp_path), target="bridge") is True
        assert "Force restart" in result

    def test_force_without_running_runner_still_writes_marker(self, tmp_path):
        from skills.core.restart.handler import handle

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("os.kill") as mock_kill:
            result = handle(self._ctx(tmp_path, "--force"))

        mock_kill.assert_not_called()
        assert is_force_restart(str(tmp_path), target="run") is True
        assert (tmp_path / RESTART_BRIDGE_FILE).exists()
        assert "not running" in result

    def test_force_does_not_signal_a_recycled_pid(self, tmp_path, monkeypatch):
        """PID reuse guard: SIGUSR2 would kill whatever inherited the PID."""
        from skills.core.restart.handler import handle

        monkeypatch.setattr(
            "pathlib.Path.read_bytes", lambda self: b"/usr/sbin/cron\x00-f\x00")
        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("os.kill") as mock_kill:
            handle(self._ctx(tmp_path, "--force"))

        mock_kill.assert_not_called()
        # Marker fallback still lets the runner restart on its own poll.
        assert is_force_restart(str(tmp_path), target="run") is True

    def test_force_survives_a_dead_pid_between_lookup_and_kill(self, tmp_path):
        from skills.core.restart.handler import handle

        with patch("app.pid_manager.check_pidfile", return_value=99999), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill", side_effect=ProcessLookupError):
            result = handle(self._ctx(tmp_path, "--force"))

        assert "Force restart" in result
        assert is_force_restart(str(tmp_path), target="run") is True


class TestRunnerSigusr2:
    def test_kills_mission_and_exits_for_relaunch(self, monkeypatch):
        from app import run

        class FakeProc:
            def poll(self):
                return None

        proc = FakeProc()
        killed = []
        monkeypatch.setattr(run._sig, "claude_proc", proc)
        monkeypatch.setattr(run, "_kill_process_group", killed.append)

        with pytest.raises(SystemExit) as exc:
            run._on_sigusr2(signal.SIGUSR2, None)

        assert exc.value.code == RESTART_EXIT_CODE
        assert killed == [proc]

    def test_exits_even_with_no_mission_running(self, monkeypatch):
        from app import run

        monkeypatch.setattr(run._sig, "claude_proc", None)
        monkeypatch.setattr(run._sig, "task_running", False)
        monkeypatch.setattr(
            run, "_kill_process_group", lambda p: pytest.fail("nothing to kill"))

        with pytest.raises(SystemExit) as exc:
            run._on_sigusr2(signal.SIGUSR2, None)

        assert exc.value.code == RESTART_EXIT_CODE

    def test_warns_when_a_running_mission_has_no_killable_subprocess(
            self, monkeypatch):
        """An unkilled session would survive the re-exec — say so."""
        from app import run

        monkeypatch.setattr(run._sig, "claude_proc", None)
        monkeypatch.setattr(run._sig, "task_running", True)

        with patch("app.run.log") as mock_log, pytest.raises(SystemExit):
            run._on_sigusr2(signal.SIGUSR2, None)

        assert any(c[0][0] == "warn" for c in mock_log.call_args_list)

    def test_signal_is_deferred_until_the_subprocess_is_published(self):
        """SIGUSR2 raised inside the guard must not fire before it exits."""
        from app import run

        fired = []
        previous = signal.signal(signal.SIGUSR2, lambda *a: fired.append(True))
        try:
            with run._sigusr2_deferred():
                os.kill(os.getpid(), signal.SIGUSR2)
                assert fired == []
            assert fired == [True]
        finally:
            signal.signal(signal.SIGUSR2, previous)


class TestForcedMarkerFallback:
    """If SIGUSR2 is lost, the mission poll loop must still restart."""

    @pytest.fixture(autouse=True)
    def _sandbox(self, monkeypatch):
        """Keep run_claude_task off host-wide state.

        Its real path takes the per-uid provider invocation lock (serialising
        against any other Kōan process on the machine) and, in its finally,
        sweeps stray /tmp trees — including a concurrent pytest run's
        ``pytest-of-*`` dirs — and drops the page cache.
        """
        def fake_popen_cli(cmd, provider=None, **kwargs):
            kwargs.pop("stdin", None)
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, **kwargs)
            return proc, lambda: None

        monkeypatch.setattr("app.cli_exec.popen_cli", fake_popen_cli)
        monkeypatch.setattr("app.utils.sweep_stray_tmp_dirs", lambda *a, **k: [])
        monkeypatch.setattr("app.page_cache.run_reclaim", lambda *a, **k: None)

    def test_mission_wait_loop_kills_and_exits_on_forced_marker(
            self, tmp_path, monkeypatch):
        from app import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setattr(run, "MISSION_POLL_INTERVAL", 0.2)
        run._sig.task_running = False
        request_restart(str(tmp_path), force=True)

        stdout_f = str(tmp_path / "out.txt")
        with pytest.raises(SystemExit) as exc:
            run.run_claude_task(
                cmd=["sleep", "30"],
                stdout_file=stdout_f,
                stderr_file=str(tmp_path / "err.txt"),
                cwd=str(tmp_path),
            )

        assert exc.value.code == RESTART_EXIT_CODE
        assert Path(stdout_f).exists()

    def test_mission_wait_loop_ignores_a_plain_restart_marker(
            self, tmp_path, monkeypatch):
        """A polite /restart must let the mission finish."""
        from app import run

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setattr(run, "MISSION_POLL_INTERVAL", 0.2)
        run._sig.task_running = False
        request_restart(str(tmp_path))

        exit_code = run.run_claude_task(
            cmd=["sh", "-c", "sleep 0.5; echo done"],
            stdout_file=str(tmp_path / "out.txt"),
            stderr_file=str(tmp_path / "err.txt"),
            cwd=str(tmp_path),
        )

        assert exit_code == 0
        assert "done" in Path(tmp_path / "out.txt").read_text()
