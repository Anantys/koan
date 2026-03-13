"""Tests for pid_manager — exclusive PID file enforcement."""

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.pid_manager import (
    _pidfile_path,
    _read_pid,
    _read_runner_state,
    _is_process_alive,
    _bootout_launchd_service,
    _detect_provider,
    _needs_ollama,
    _log_dir,
    _open_log_file,
    _launch_python_process,
    _wait_for_exit,
    acquire_pidfile,
    release_pidfile,
    acquire_pid,
    release_pid,
    check_pidfile,
    stop_processes,
    start_runner,
    start_awake,
    start_ollama,
    start_all,
    start_stack,
    get_status_processes,
    format_status_all,
    _print_stack_results,
    PROCESS_NAMES,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# _pidfile_path
# ---------------------------------------------------------------------------


class TestPidfilePath:
    def test_run_process(self, tmp_path):
        assert _pidfile_path(tmp_path, "run") == tmp_path / ".koan-pid-run"

    def test_awake_process(self, tmp_path):
        assert _pidfile_path(tmp_path, "awake") == tmp_path / ".koan-pid-awake"

    def test_custom_name(self, tmp_path):
        assert _pidfile_path(tmp_path, "foo") == tmp_path / ".koan-pid-foo"


# ---------------------------------------------------------------------------
# _read_pid
# ---------------------------------------------------------------------------


class TestReadPid:
    def test_reads_valid_pid(self, tmp_path):
        pidfile = tmp_path / "test.pid"
        pidfile.write_text("12345")
        assert _read_pid(pidfile) == 12345

    def test_reads_pid_with_whitespace(self, tmp_path):
        pidfile = tmp_path / "test.pid"
        pidfile.write_text("  12345  \n")
        assert _read_pid(pidfile) == 12345

    def test_returns_none_for_empty_file(self, tmp_path):
        pidfile = tmp_path / "test.pid"
        pidfile.write_text("")
        assert _read_pid(pidfile) is None

    def test_returns_none_for_invalid_content(self, tmp_path):
        pidfile = tmp_path / "test.pid"
        pidfile.write_text("not-a-pid")
        assert _read_pid(pidfile) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        pidfile = tmp_path / "nonexistent.pid"
        assert _read_pid(pidfile) is None


# ---------------------------------------------------------------------------
# _is_process_alive
# ---------------------------------------------------------------------------


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        # PID 99999999 is almost certainly not running
        assert _is_process_alive(99999999) is False

    def test_pid_zero_is_special(self):
        # PID 0 on macOS/Linux sends signal to process group
        # _is_process_alive should handle this gracefully
        result = _is_process_alive(0)
        assert isinstance(result, bool)

    def test_zombie_detected_via_ps(self):
        """Zombie processes (state=Z) should be reported as dead."""
        ps_result = MagicMock(stdout="Z", returncode=0)
        with patch("os.kill"):  # kill(pid, 0) succeeds (zombie exists)
            with patch("builtins.open", side_effect=FileNotFoundError):  # no /proc
                with patch("app.pid_manager.subprocess.run", return_value=ps_result):
                    assert _is_process_alive(42) is False

    def test_zombie_detected_via_proc(self):
        """On Linux, zombie detected via /proc/PID/status."""
        from io import StringIO
        proc_content = StringIO("Name:\ttest\nState:\tZ (zombie)\nPid:\t42\n")
        with patch("os.kill"):  # kill(pid, 0) succeeds
            with patch("builtins.open", return_value=proc_content):
                assert _is_process_alive(42) is False

    def test_alive_process_not_zombie(self):
        """Running process detected via ps should be reported as alive."""
        ps_result = MagicMock(stdout="S", returncode=0)
        with patch("os.kill"):  # kill(pid, 0) succeeds
            with patch("builtins.open", side_effect=FileNotFoundError):  # no /proc
                with patch("app.pid_manager.subprocess.run", return_value=ps_result):
                    assert _is_process_alive(42) is True

    def test_ps_failure_assumes_alive(self):
        """If ps fails, assume process is alive (safe default)."""
        with patch("os.kill"):  # kill(pid, 0) succeeds
            with patch("builtins.open", side_effect=FileNotFoundError):
                with patch("app.pid_manager.subprocess.run",
                           side_effect=subprocess.SubprocessError("ps failed")):
                    assert _is_process_alive(42) is True


# ---------------------------------------------------------------------------
# acquire_pidfile (flock-based, for Python processes)
# ---------------------------------------------------------------------------


class TestAcquirePidfile:
    def test_creates_pidfile_with_current_pid(self, tmp_path):
        fh = acquire_pidfile(tmp_path, "awake")
        pidfile = tmp_path / ".koan-pid-awake"
        assert pidfile.exists()
        assert pidfile.read_text().strip() == str(os.getpid())
        release_pidfile(fh, tmp_path, "awake")

    def test_returns_open_file_handle(self, tmp_path):
        fh = acquire_pidfile(tmp_path, "awake")
        assert not fh.closed
        release_pidfile(fh, tmp_path, "awake")

    def test_flock_is_exclusive(self, tmp_path):
        """Second acquire should fail when first holds the lock."""
        fh = acquire_pidfile(tmp_path, "awake")

        with pytest.raises(SystemExit) as exc_info:
            acquire_pidfile(tmp_path, "awake")

        assert exc_info.value.code == 1
        release_pidfile(fh, tmp_path, "awake")

    def test_error_message_includes_pid(self, tmp_path, capsys):
        fh = acquire_pidfile(tmp_path, "awake")

        with pytest.raises(SystemExit):
            acquire_pidfile(tmp_path, "awake")

        captured = capsys.readouterr()
        assert "awake" in captured.err
        assert "already running" in captured.err
        assert str(os.getpid()) in captured.err
        release_pidfile(fh, tmp_path, "awake")

    def test_overwrites_stale_pidfile(self, tmp_path):
        """If no lock is held, acquire should succeed even if file exists."""
        pidfile = tmp_path / ".koan-pid-awake"
        pidfile.write_text("99999")  # stale PID

        fh = acquire_pidfile(tmp_path, "awake")
        assert pidfile.read_text().strip() == str(os.getpid())
        release_pidfile(fh, tmp_path, "awake")

    def test_different_process_names_dont_conflict(self, tmp_path):
        fh_run = acquire_pidfile(tmp_path, "run")
        fh_awake = acquire_pidfile(tmp_path, "awake")

        assert (tmp_path / ".koan-pid-run").exists()
        assert (tmp_path / ".koan-pid-awake").exists()

        release_pidfile(fh_run, tmp_path, "run")
        release_pidfile(fh_awake, tmp_path, "awake")


# ---------------------------------------------------------------------------
# release_pidfile
# ---------------------------------------------------------------------------


class TestReleasePidfile:
    def test_removes_pidfile(self, tmp_path):
        fh = acquire_pidfile(tmp_path, "awake")
        release_pidfile(fh, tmp_path, "awake")
        assert not (tmp_path / ".koan-pid-awake").exists()

    def test_closes_file_handle(self, tmp_path):
        fh = acquire_pidfile(tmp_path, "awake")
        release_pidfile(fh, tmp_path, "awake")
        assert fh.closed

    def test_lock_released_after_release(self, tmp_path):
        """After release, a new acquire should succeed."""
        fh1 = acquire_pidfile(tmp_path, "awake")
        release_pidfile(fh1, tmp_path, "awake")

        fh2 = acquire_pidfile(tmp_path, "awake")
        assert not fh2.closed
        release_pidfile(fh2, tmp_path, "awake")

    def test_tolerates_already_removed_file(self, tmp_path):
        fh = acquire_pidfile(tmp_path, "awake")
        (tmp_path / ".koan-pid-awake").unlink()
        # Should not raise
        release_pidfile(fh, tmp_path, "awake")


# ---------------------------------------------------------------------------
# acquire_pid (PID-liveness-based, for bash processes)
# ---------------------------------------------------------------------------


class TestAcquirePid:
    def test_creates_pidfile_with_given_pid(self, tmp_path):
        acquire_pid(tmp_path, "run", 12345)
        pidfile = tmp_path / ".koan-pid-run"
        assert pidfile.exists()
        assert pidfile.read_text() == "12345"

    def test_overwrites_stale_pid(self, tmp_path):
        """If existing PID is not alive, overwrite."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("99999999")  # dead PID
        acquire_pid(tmp_path, "run", 12345)
        assert pidfile.read_text() == "12345"

    def test_aborts_if_pid_is_alive(self, tmp_path):
        """If existing PID is alive and different, abort."""
        current_pid = os.getpid()
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(current_pid))

        with pytest.raises(SystemExit) as exc_info:
            acquire_pid(tmp_path, "run", 99999)

        assert exc_info.value.code == 1

    def test_error_message_includes_running_pid(self, tmp_path, capsys):
        current_pid = os.getpid()
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(current_pid))

        with pytest.raises(SystemExit):
            acquire_pid(tmp_path, "run", 99999)

        captured = capsys.readouterr()
        assert str(current_pid) in captured.err
        assert "already running" in captured.err

    def test_same_pid_allowed(self, tmp_path):
        """Same PID writing again (re-exec) should succeed."""
        current_pid = os.getpid()
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(current_pid))

        # Should not raise
        acquire_pid(tmp_path, "run", current_pid)
        assert pidfile.read_text() == str(current_pid)

    def test_no_existing_file(self, tmp_path):
        """First acquisition with no existing file."""
        acquire_pid(tmp_path, "run", 42)
        assert (tmp_path / ".koan-pid-run").read_text() == "42"


# ---------------------------------------------------------------------------
# release_pid
# ---------------------------------------------------------------------------


class TestReleasePid:
    def test_removes_pidfile(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("12345")
        release_pid(tmp_path, "run")
        assert not pidfile.exists()

    def test_tolerates_missing_file(self, tmp_path):
        # Should not raise
        release_pid(tmp_path, "run")


# ---------------------------------------------------------------------------
# check_pidfile
# ---------------------------------------------------------------------------


class TestCheckPidfile:
    def test_returns_none_when_no_file(self, tmp_path):
        assert check_pidfile(tmp_path, "awake") is None

    def test_returns_pid_when_flock_held(self, tmp_path):
        """If a Python process holds the flock, return its PID."""
        fh = acquire_pidfile(tmp_path, "awake")
        pid = check_pidfile(tmp_path, "awake")
        assert pid == os.getpid()
        release_pidfile(fh, tmp_path, "awake")

    def test_returns_none_when_flock_released(self, tmp_path):
        """After release, check should return None."""
        fh = acquire_pidfile(tmp_path, "awake")
        release_pidfile(fh, tmp_path, "awake")
        assert check_pidfile(tmp_path, "awake") is None

    def test_returns_pid_for_alive_bash_process(self, tmp_path):
        """If PID file exists with alive PID (no flock), return PID."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(os.getpid()))
        pid = check_pidfile(tmp_path, "run")
        assert pid == os.getpid()

    def test_returns_none_for_dead_bash_process(self, tmp_path):
        """If PID file exists but PID is dead, return None."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("99999999")
        assert check_pidfile(tmp_path, "run") is None

    def test_returns_none_for_empty_file(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("")
        assert check_pidfile(tmp_path, "run") is None

    def test_returns_none_for_corrupt_file(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("not-a-number")
        assert check_pidfile(tmp_path, "run") is None


# ---------------------------------------------------------------------------
# CLI interface (__main__)
# ---------------------------------------------------------------------------


class TestCLI:
    def _run_cli(self, *args, cwd=None):
        """Run pid_manager as a module and return the result."""
        cmd = [sys.executable, "-m", "app.pid_manager"] + list(args)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=cwd
        )

    def test_acquire_pid_cli(self, tmp_path):
        result = self._run_cli("acquire-pid", "run", str(tmp_path), "12345")
        assert result.returncode == 0
        pidfile = tmp_path / ".koan-pid-run"
        assert pidfile.read_text() == "12345"

    def test_acquire_pid_cli_blocks_duplicate(self, tmp_path):
        """Second acquire with alive PID should fail."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(os.getpid()))  # alive PID

        result = self._run_cli("acquire-pid", "run", str(tmp_path), "99999")
        assert result.returncode == 1
        assert "already running" in result.stderr

    def test_release_pid_cli(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("12345")

        result = self._run_cli("release-pid", "run", str(tmp_path))
        assert result.returncode == 0
        assert not pidfile.exists()

    def test_check_cli_not_running(self, tmp_path):
        result = self._run_cli("check", "run", str(tmp_path))
        assert result.returncode == 0
        assert "not_running" in result.stdout

    def test_check_cli_running(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(os.getpid()))

        result = self._run_cli("check", "run", str(tmp_path))
        assert result.returncode == 0
        assert f"running:{os.getpid()}" in result.stdout

    def test_missing_args(self):
        result = self._run_cli("acquire-pid")
        assert result.returncode == 2

    def test_unknown_action(self, tmp_path):
        result = self._run_cli("unknown", "run", str(tmp_path))
        assert result.returncode == 2

    def test_acquire_pid_missing_pid_arg(self, tmp_path):
        result = self._run_cli("acquire-pid", "run", str(tmp_path))
        assert result.returncode == 2
        assert "requires a PID" in result.stderr


# ---------------------------------------------------------------------------
# Integration: awake.py startup with PID lock
# ---------------------------------------------------------------------------


class TestAwakeIntegration:
    """Test that awake.py's main() acquires the PID lock."""

    @patch("app.awake.check_config")
    @patch("app.awake.compact_history", return_value=0)
    @patch("app.awake.write_heartbeat")
    @patch("app.awake._get_registry")
    @patch("app.awake.get_updates", side_effect=KeyboardInterrupt)
    @patch("app.awake.send_telegram")
    @patch("app.awake.log")
    @patch("app.awake.KOAN_ROOT")
    @patch("app.awake.BOT_TOKEN", "test-token-12345678")
    @patch("app.awake.CHAT_ID", "123")
    @patch("app.awake.SOUL", "test soul")
    @patch("app.awake.SUMMARY", "test summary")
    def test_main_acquires_pidfile(
        self,
        mock_root,
        mock_log,
        mock_send,
        mock_updates,
        mock_registry,
        mock_heartbeat,
        mock_compact,
        mock_config,
        tmp_path,
    ):
        from app.awake import main

        mock_root.__truediv__ = lambda self, x: tmp_path / x
        mock_root.unlink = MagicMock()
        # Set up the Path-like behavior
        type(mock_root).__truediv__ = lambda self, other: tmp_path / other

        registry_mock = MagicMock()
        registry_mock.list_by_scope.return_value = []
        registry_mock.__len__ = lambda self: 0
        mock_registry.return_value = registry_mock

        # Patch at the module level so lazy imports inside main() get mocked
        with patch("app.pid_manager.acquire_pidfile") as mock_acquire, \
             patch("app.pid_manager.release_pidfile") as mock_release:
            mock_fh = MagicMock()
            mock_acquire.return_value = mock_fh

            with pytest.raises(SystemExit):
                main()

            mock_acquire.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_acquire_after_crash_no_cleanup(self, tmp_path):
        """Simulate crash: file exists with old PID, no flock held."""
        pidfile = tmp_path / ".koan-pid-awake"
        pidfile.write_text("99999999")

        fh = acquire_pidfile(tmp_path, "awake")
        assert pidfile.read_text().strip() == str(os.getpid())
        release_pidfile(fh, tmp_path, "awake")

    def test_concurrent_run_and_awake(self, tmp_path):
        """run and awake PID files are independent."""
        acquire_pid(tmp_path, "run", 111)
        fh = acquire_pidfile(tmp_path, "awake")

        assert (tmp_path / ".koan-pid-run").read_text() == "111"
        assert (tmp_path / ".koan-pid-awake").read_text().strip() == str(os.getpid())

        release_pid(tmp_path, "run")
        release_pidfile(fh, tmp_path, "awake")

    def test_release_idempotent(self, tmp_path):
        """Multiple releases should not raise."""
        fh = acquire_pidfile(tmp_path, "awake")
        release_pidfile(fh, tmp_path, "awake")
        # Second release with closed fh — should not raise
        release_pidfile(fh, tmp_path, "awake")


# ---------------------------------------------------------------------------
# stop_processes
# ---------------------------------------------------------------------------


class TestStopProcesses:
    def test_no_processes_running(self, tmp_path):
        """When no processes are running, all results are not_running."""
        results = stop_processes(tmp_path)
        assert results["run"] == "not_running"
        assert results["awake"] == "not_running"
        assert results["ollama"] == "not_running"

    def test_creates_stop_file(self, tmp_path):
        """stop_processes always creates .koan-stop signal file."""
        stop_processes(tmp_path)
        assert (tmp_path / ".koan-stop").exists()
        assert (tmp_path / ".koan-stop").read_text() == "STOP"

    def test_stops_running_subprocess(self, tmp_path):
        """SIGTERM a real subprocess and verify it exits."""
        # Start a sleep process to simulate a running koan process
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(proc.pid))

        results = stop_processes(tmp_path, timeout=3.0)
        assert results["run"] in ("stopped", "force_killed")
        assert results["awake"] == "not_running"
        # PID file should be cleaned up
        assert not pidfile.exists()
        # Reap child to verify it's gone
        proc.wait(timeout=5)
        assert proc.returncode is not None

    def test_force_kills_stubborn_process(self, tmp_path):
        """If SIGTERM doesn't work within timeout, SIGKILL is sent."""
        # Start a process that ignores SIGTERM and signals readiness via file
        ready_file = tmp_path / ".ready"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             f"import signal, time, pathlib; "
             f"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             f"pathlib.Path('{ready_file}').write_text('ok'); "
             f"time.sleep(60)"]
        )
        pidfile = tmp_path / ".koan-pid-awake"
        pidfile.write_text(str(proc.pid))

        # Wait for child to install SIGTERM handler
        deadline = time.monotonic() + 5
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

        results = stop_processes(tmp_path, timeout=1.0)
        assert results["awake"] == "force_killed"
        assert not pidfile.exists()
        # Reap the child to verify it's gone
        proc.wait(timeout=5)
        assert proc.returncode is not None

    def test_handles_already_dead_pid(self, tmp_path):
        """If PID in file is already dead, report not_running."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text("99999999")  # dead PID
        results = stop_processes(tmp_path)
        assert results["run"] == "not_running"

    def test_stops_both_processes(self, tmp_path):
        """Can stop both run and awake simultaneously."""
        procs = []
        for name in PROCESS_NAMES:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            pidfile = tmp_path / f".koan-pid-{name}"
            pidfile.write_text(str(proc.pid))
            procs.append(proc)

        results = stop_processes(tmp_path, timeout=3.0)
        assert results["run"] in ("stopped", "force_killed")
        assert results["awake"] in ("stopped", "force_killed")

        for proc in procs:
            proc.wait(timeout=5)

    def test_cleans_up_pid_files_after_stop(self, tmp_path):
        """PID files are removed after stopping."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(proc.pid))

        stop_processes(tmp_path, timeout=3.0)
        assert not (tmp_path / ".koan-pid-run").exists()
        proc.wait(timeout=5)  # Clean up child

    def test_process_names_constant(self):
        """PROCESS_NAMES includes all expected processes."""
        assert "run" in PROCESS_NAMES
        assert "awake" in PROCESS_NAMES
        assert "ollama" in PROCESS_NAMES

    def test_bootouts_launchd_before_sigterm(self, tmp_path):
        """stop_processes calls _bootout_launchd_service for each process."""
        with patch("app.pid_manager._bootout_launchd_service") as mock_bootout:
            mock_bootout.return_value = False
            stop_processes(tmp_path)
            # Should be called for each process name
            assert mock_bootout.call_count == len(PROCESS_NAMES)
            called_names = [c[0][0] for c in mock_bootout.call_args_list]
            for name in PROCESS_NAMES:
                assert name in called_names


# ---------------------------------------------------------------------------
# _bootout_launchd_service
# ---------------------------------------------------------------------------


class TestBootoutLaunchdService:
    def test_returns_false_on_non_darwin(self):
        """On non-Darwin platforms, returns False immediately."""
        with patch("app.pid_manager.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert _bootout_launchd_service("run") is False

    def test_returns_false_when_service_not_loaded(self):
        """Returns False when launchctl list fails (service not registered)."""
        with patch("app.pid_manager.sys") as mock_sys, \
             patch("app.pid_manager.subprocess.run") as mock_run:
            mock_sys.platform = "darwin"
            mock_run.return_value = MagicMock(returncode=113)
            assert _bootout_launchd_service("run") is False
            mock_run.assert_called_once()

    def test_bootouts_loaded_service(self):
        """Successfully boots out a loaded launchd service."""
        with patch("app.pid_manager.sys") as mock_sys, \
             patch("app.pid_manager.subprocess.run") as mock_run, \
             patch("app.pid_manager.os.getuid", return_value=501):
            mock_sys.platform = "darwin"
            mock_run.return_value = MagicMock(returncode=0)
            assert _bootout_launchd_service("awake") is True
            assert mock_run.call_count == 2
            # Second call should be bootout
            bootout_call = mock_run.call_args_list[1]
            assert "bootout" in bootout_call[0][0]
            assert "gui/501/com.koan.awake" in bootout_call[0][0]

    def test_handles_subprocess_error(self):
        """Returns False on subprocess errors (e.g., launchctl not found)."""
        with patch("app.pid_manager.sys") as mock_sys, \
             patch("app.pid_manager.subprocess.run", side_effect=OSError("not found")):
            mock_sys.platform = "darwin"
            assert _bootout_launchd_service("run") is False


# ---------------------------------------------------------------------------
# _read_runner_state
# ---------------------------------------------------------------------------


class TestReadRunnerState:
    """Tests for _read_runner_state signal file reading."""

    def test_empty_state_when_no_files(self, tmp_path):
        state = _read_runner_state(tmp_path)
        assert state["status"] == ""
        assert state["paused"] is False
        assert state["pause_reason"] == ""
        assert state["project"] == ""

    def test_reads_status_file(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 3/20 — executing mission on koan")
        state = _read_runner_state(tmp_path)
        assert state["status"] == "Run 3/20 — executing mission on koan"

    def test_reads_project_file(self, tmp_path):
        (tmp_path / ".koan-project").write_text("myproject")
        state = _read_runner_state(tmp_path)
        assert state["project"] == "myproject"

    def test_reads_pause_state(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("PAUSE")
        state = _read_runner_state(tmp_path)
        assert state["paused"] is True

    def test_reads_pause_reason(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("quota\n1234567890")
        state = _read_runner_state(tmp_path)
        assert state["pause_reason"] == "quota"

    def test_pause_without_reason_content(self, tmp_path):
        """Empty .koan-pause should set paused but no reason."""
        (tmp_path / ".koan-pause").write_text("")
        state = _read_runner_state(tmp_path)
        assert state["paused"] is True
        assert state["pause_reason"] == ""

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / ".koan-status").write_text("  Idle  \n")
        (tmp_path / ".koan-project").write_text("  koan  \n")
        state = _read_runner_state(tmp_path)
        assert state["status"] == "Idle"
        assert state["project"] == "koan"

    def test_empty_status_file(self, tmp_path):
        (tmp_path / ".koan-status").write_text("")
        state = _read_runner_state(tmp_path)
        assert state["status"] == ""


# ---------------------------------------------------------------------------
# format_status_all
# ---------------------------------------------------------------------------


class TestFormatStatusAll:
    """Tests for format_status_all rich status display."""

    def test_all_not_running(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "run: not running" in output
        assert "awake: not running" in output

    def test_runner_running_with_status(self, tmp_path):
        """When runner is alive and has status, display it."""
        (tmp_path / ".koan-status").write_text("Run 5/20 — executing mission on koan")
        (tmp_path / ".koan-project").write_text("koan")
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "Run 5/20" in output
        assert "executing mission" in output
        assert f"PID {os.getpid()}" in output
        assert "project: koan" in output

    def test_runner_running_no_status(self, tmp_path):
        """When runner is alive but no status file, show generic running."""
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert f"run: running (PID {os.getpid()})" in output

    def test_runner_paused_quota(self, tmp_path):
        """When paused for quota, show pause reason."""
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))
        (tmp_path / ".koan-pause").write_text("quota\n1234567890\n")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "paused" in output
        assert "quota exhausted" in output

    def test_runner_paused_max_runs(self, tmp_path):
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))
        (tmp_path / ".koan-pause").write_text("max_runs\n1234567890\n")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "paused" in output
        assert "max runs reached" in output

    def test_runner_paused_errors(self, tmp_path):
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))
        (tmp_path / ".koan-pause").write_text("errors")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "paused" in output
        assert "too many errors" in output

    def test_runner_paused_generic(self, tmp_path):
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))
        (tmp_path / ".koan-pause").write_text("PAUSE")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "paused" in output

    def test_no_project_when_paused(self, tmp_path):
        """When paused, don't show project (it's stale context)."""
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))
        (tmp_path / ".koan-pause").write_text("PAUSE")
        (tmp_path / ".koan-project").write_text("koan")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "project:" not in output

    def test_no_state_files_when_not_running(self, tmp_path):
        """When runner is NOT running, don't show stale status files."""
        (tmp_path / ".koan-status").write_text("Run 3/20 — executing mission on koan")
        (tmp_path / ".koan-project").write_text("koan")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "run: not running" in output
        assert "Run 3/20" not in output
        assert "project:" not in output

    def test_bridge_running(self, tmp_path):
        (tmp_path / ".koan-pid-awake").write_text(str(os.getpid()))

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert f"awake: running (PID {os.getpid()})" in output

    def test_ollama_shown_for_local_provider(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="local"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "ollama: not running" in output

    def test_ollama_hidden_for_claude_provider(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "ollama" not in output

    def test_idle_status_shows_project(self, tmp_path):
        """Idle state should still show project context."""
        (tmp_path / ".koan-pid-run").write_text(str(os.getpid()))
        (tmp_path / ".koan-status").write_text("Idle — sleeping 300s (14:35)")
        (tmp_path / ".koan-project").write_text("myapp")

        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        output = "\n".join(lines)
        assert "Idle" in output
        assert "project: myapp" in output

    def test_returns_list_of_strings(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="claude"):
            lines = format_status_all(tmp_path)
        assert isinstance(lines, list)
        for line in lines:
            assert isinstance(line, str)


# ---------------------------------------------------------------------------
# CLI: stop-all and status-all
# ---------------------------------------------------------------------------


class TestCLIStopAll:
    def _run_cli(self, *args):
        cmd = [sys.executable, "-m", "app.pid_manager"] + list(args)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_stop_all_no_processes(self, tmp_path):
        result = self._run_cli("stop-all", str(tmp_path))
        assert result.returncode == 0
        assert "not running" in result.stdout
        assert "No processes were running." in result.stdout

    def test_stop_all_with_process(self, tmp_path):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(proc.pid))

        result = self._run_cli("stop-all", str(tmp_path))
        assert result.returncode == 0
        # Process may be stopped or force-killed depending on timing
        assert "run: stopped" in result.stdout or "run: force killed" in result.stdout
        # Reap to clean up
        proc.wait(timeout=5)

    def test_status_all_no_processes(self, tmp_path):
        result = self._run_cli("status-all", str(tmp_path))
        assert result.returncode == 0
        assert "run: not running" in result.stdout
        assert "awake: not running" in result.stdout

    def test_status_all_with_running_process(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(os.getpid()))

        result = self._run_cli("status-all", str(tmp_path))
        assert result.returncode == 0
        assert f"PID {os.getpid()}" in result.stdout
        assert "awake: not running" in result.stdout

    def test_start_runner_cli_already_running(self, tmp_path):
        """CLI start-runner exits 1 when runner is already running."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(os.getpid()))

        result = self._run_cli("start-runner", str(tmp_path))
        assert result.returncode == 1
        assert "already running" in result.stdout

    def test_start_runner_cli_no_koan_dir(self, tmp_path):
        """CLI start-runner fails gracefully when koan/ dir is missing."""
        result = self._run_cli("start-runner", str(tmp_path))
        assert result.returncode == 1
        assert "Failed to launch" in result.stdout or "PID not detected" in result.stdout


# ---------------------------------------------------------------------------
# start_runner
# ---------------------------------------------------------------------------


class TestStartRunner:
    def test_returns_already_running_if_pid_exists(self, tmp_path):
        """If runner is already alive, don't launch a second one."""
        pidfile = tmp_path / ".koan-pid-run"
        pidfile.write_text(str(os.getpid()))

        ok, msg = start_runner(tmp_path)
        assert ok is False
        assert "already running" in msg
        assert str(os.getpid()) in msg

    def test_clears_stop_file_before_launch(self, tmp_path):
        """The .koan-stop signal must be cleared, or run.py exits immediately."""
        stop_file = tmp_path / ".koan-stop"
        stop_file.write_text("STOP")

        # Mock Popen to avoid actually starting run.py
        with patch("app.pid_manager.subprocess.Popen"):
            with patch("app.pid_manager.check_pidfile", side_effect=[None, None, None, None, None, None, None, None, None, None]):
                start_runner(tmp_path, verify_timeout=0.5)

        assert not stop_file.exists()

    def test_launches_subprocess_with_correct_args(self, tmp_path):
        """Verify subprocess.Popen is called with the right command and env."""
        with patch("app.pid_manager.subprocess.Popen") as mock_popen:
            with patch("app.pid_manager.check_pidfile", side_effect=[None, None, None, None, None, None, None, None, None, None]):
                start_runner(tmp_path, verify_timeout=0.5)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        # Command should be [python, "app/run.py"]
        assert call_args[0][0][1] == "app/run.py"
        # Should be detached
        assert call_args[1]["start_new_session"] is True
        # cwd should be the koan subdirectory
        assert call_args[1]["cwd"] == str(tmp_path / "koan")
        # Env should include KOAN_ROOT and PYTHONPATH
        assert call_args[1]["env"]["KOAN_ROOT"] == str(tmp_path)
        assert call_args[1]["env"]["PYTHONPATH"] == str(tmp_path / "koan")

    def test_returns_success_when_pid_appears(self, tmp_path):
        """After launch, verify PID appears within timeout."""
        with patch("app.pid_manager.subprocess.Popen"):
            # First check_pidfile in the function body (already running?) returns None
            # Then verify loop: None, None, then a PID appears
            with patch("app.pid_manager.check_pidfile", side_effect=[None, None, None, 42]):
                ok, msg = start_runner(tmp_path, verify_timeout=2.0)

        assert ok is True
        assert "PID 42" in msg

    def test_returns_warning_when_pid_not_detected(self, tmp_path):
        """If PID never appears within timeout, return a warning."""
        with patch("app.pid_manager.subprocess.Popen"):
            with patch("app.pid_manager.check_pidfile", return_value=None):
                ok, msg = start_runner(tmp_path, verify_timeout=0.5)

        assert ok is False
        assert "PID not detected" in msg

    def test_returns_failure_on_popen_exception(self, tmp_path):
        """If Popen raises, return the error message."""
        with patch("app.pid_manager.subprocess.Popen", side_effect=OSError("No such file")):
            ok, msg = start_runner(tmp_path)

        assert ok is False
        assert "Failed to launch" in msg
        assert "No such file" in msg


# ---------------------------------------------------------------------------
# start_ollama
# ---------------------------------------------------------------------------


class TestStartOllama:
    def test_returns_already_running_if_pid_exists(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-ollama"
        pidfile.write_text(str(os.getpid()))

        ok, msg = start_ollama(tmp_path)
        assert ok is False
        assert "already running" in msg

    def test_returns_error_when_binary_not_found(self, tmp_path):
        with patch("app.pid_manager.shutil.which", return_value=None):
            ok, msg = start_ollama(tmp_path)

        assert ok is False
        assert "not found in PATH" in msg

    def test_launches_ollama_serve(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 54321

        with patch("app.pid_manager.shutil.which", return_value="/usr/local/bin/ollama"), \
             patch("app.pid_manager.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("app.pid_manager._is_process_alive", return_value=True):
            ok, msg = start_ollama(tmp_path, verify_timeout=0.5)

        assert ok is True
        assert "54321" in msg
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["/usr/local/bin/ollama", "serve"]
        assert call_args[1]["start_new_session"] is True

    def test_writes_pid_file(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 54321

        with patch("app.pid_manager.shutil.which", return_value="/usr/local/bin/ollama"), \
             patch("app.pid_manager.subprocess.Popen", return_value=mock_proc), \
             patch("app.pid_manager._is_process_alive", return_value=True):
            start_ollama(tmp_path, verify_timeout=0.5)

        pidfile = tmp_path / ".koan-pid-ollama"
        assert pidfile.exists()
        assert pidfile.read_text() == "54321"

    def test_returns_failure_on_popen_exception(self, tmp_path):
        with patch("app.pid_manager.shutil.which", return_value="/usr/local/bin/ollama"), \
             patch("app.pid_manager.subprocess.Popen", side_effect=OSError("Permission denied")):
            ok, msg = start_ollama(tmp_path)

        assert ok is False
        assert "Failed to launch ollama" in msg

    def test_returns_failure_when_process_exits_immediately(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 54321

        with patch("app.pid_manager.shutil.which", return_value="/usr/local/bin/ollama"), \
             patch("app.pid_manager.subprocess.Popen", return_value=mock_proc), \
             patch("app.pid_manager._is_process_alive", return_value=False):
            ok, msg = start_ollama(tmp_path, verify_timeout=0.5)

        assert ok is False
        assert "exited immediately" in msg

    def test_cleans_up_pid_file_when_process_exits_immediately(self, tmp_path):
        """When ollama dies right after launch, the PID file must be removed."""
        mock_proc = MagicMock()
        mock_proc.pid = 54321

        with patch("app.pid_manager.shutil.which", return_value="/usr/local/bin/ollama"), \
             patch("app.pid_manager.subprocess.Popen", return_value=mock_proc), \
             patch("app.pid_manager._is_process_alive", return_value=False):
            ok, msg = start_ollama(tmp_path, verify_timeout=0.5)

        assert ok is False
        pidfile = tmp_path / ".koan-pid-ollama"
        assert not pidfile.exists(), "Stale PID file should be removed after failed launch"


# ---------------------------------------------------------------------------
# start_awake
# ---------------------------------------------------------------------------


class TestStartAwake:
    def test_returns_already_running_if_pid_exists(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-awake"
        pidfile.write_text(str(os.getpid()))

        ok, msg = start_awake(tmp_path)
        assert ok is False
        assert "already running" in msg.lower()

    def test_launches_subprocess_with_correct_args(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen") as mock_popen:
            with patch("app.pid_manager.check_pidfile", side_effect=[None, None, None, None, None]):
                start_awake(tmp_path, verify_timeout=0.5)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0][1] == "app/awake.py"
        assert call_args[1]["start_new_session"] is True
        assert call_args[1]["cwd"] == str(tmp_path / "koan")
        assert call_args[1]["env"]["KOAN_ROOT"] == str(tmp_path)

    def test_returns_success_when_pid_appears(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen"):
            with patch("app.pid_manager.check_pidfile", side_effect=[None, None, 99]):
                ok, msg = start_awake(tmp_path, verify_timeout=2.0)

        assert ok is True
        assert "PID 99" in msg

    def test_returns_warning_when_pid_not_detected(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen"):
            with patch("app.pid_manager.check_pidfile", return_value=None):
                ok, msg = start_awake(tmp_path, verify_timeout=0.5)

        assert ok is False
        assert "PID not detected" in msg

    def test_returns_failure_on_popen_exception(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen", side_effect=OSError("No such file")):
            ok, msg = start_awake(tmp_path)

        assert ok is False
        assert "Failed to launch" in msg


# ---------------------------------------------------------------------------
# _detect_provider / _needs_ollama
# ---------------------------------------------------------------------------


class TestDetectProvider:
    def test_returns_claude_by_default(self, tmp_path):
        with patch("app.pid_manager._detect_provider") as mock:
            mock.return_value = "claude"
            assert _detect_provider(tmp_path) == "claude"

    def test_returns_configured_provider(self, tmp_path):
        with patch("app.provider.get_provider_name", return_value="copilot"):
            assert _detect_provider(tmp_path) == "copilot"

    def test_returns_claude_on_import_error(self, tmp_path):
        """If provider module can't be imported, fall back to claude."""
        import importlib
        import app.pid_manager as pm

        # Save and remove the cached provider module
        original = sys.modules.get("app.provider")
        sys.modules["app.provider"] = None
        try:
            result = pm._detect_provider(tmp_path)
        finally:
            if original is not None:
                sys.modules["app.provider"] = original
            else:
                sys.modules.pop("app.provider", None)
        assert result == "claude"

    def test_returns_local_when_configured(self, tmp_path):
        with patch("app.provider.get_provider_name", return_value="local"):
            assert _detect_provider(tmp_path) == "local"


class TestNeedsOllama:
    def test_local_needs_ollama(self):
        assert _needs_ollama("local") is True

    def test_ollama_needs_ollama(self):
        assert _needs_ollama("ollama") is True

    def test_claude_does_not_need_ollama(self):
        assert _needs_ollama("claude") is False

    def test_copilot_does_not_need_ollama(self):
        assert _needs_ollama("copilot") is False


# ---------------------------------------------------------------------------
# get_status_processes
# ---------------------------------------------------------------------------


class TestGetStatusProcesses:
    def test_excludes_ollama_for_claude(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="claude"):
            result = get_status_processes(tmp_path)
        assert "run" in result
        assert "awake" in result
        assert "ollama" not in result

    def test_excludes_ollama_for_copilot(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="copilot"):
            result = get_status_processes(tmp_path)
        assert "ollama" not in result

    def test_includes_ollama_for_local(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="local"):
            result = get_status_processes(tmp_path)
        assert "ollama" in result
        assert "run" in result
        assert "awake" in result

    def test_includes_ollama_for_ollama_provider(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="ollama"):
            result = get_status_processes(tmp_path)
        assert "ollama" in result

    def test_returns_tuple(self, tmp_path):
        with patch("app.pid_manager._detect_provider", return_value="claude"):
            result = get_status_processes(tmp_path)
        assert isinstance(result, tuple)


# ---------------------------------------------------------------------------
# start_all
# ---------------------------------------------------------------------------


class TestStartAll:
    def test_claude_starts_awake_and_run_only(self, tmp_path):
        """Claude provider should start 2 processes, no ollama."""
        with patch("app.pid_manager.start_awake", return_value=(True, "Bridge started (PID 10)")) as mock_awake, \
             patch("app.pid_manager.start_runner", return_value=(True, "Agent loop started (PID 20)")) as mock_run, \
             patch("app.pid_manager.start_ollama") as mock_ollama:
            results = start_all(tmp_path, provider="claude")

        assert "ollama" not in results
        assert results["awake"] == (True, "Bridge started (PID 10)")
        assert results["run"] == (True, "Agent loop started (PID 20)")
        mock_ollama.assert_not_called()
        mock_awake.assert_called_once()
        mock_run.assert_called_once()

    def test_copilot_starts_awake_and_run_only(self, tmp_path):
        """Copilot provider should start 2 processes, no ollama."""
        with patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")), \
             patch("app.pid_manager.start_ollama") as mock_ollama:
            results = start_all(tmp_path, provider="copilot")

        assert "ollama" not in results
        mock_ollama.assert_not_called()

    def test_local_starts_all_three(self, tmp_path):
        """Local provider should start ollama + awake + run."""
        with patch("app.pid_manager.start_ollama", return_value=(True, "ollama started (PID 5)")) as mock_ollama, \
             patch("app.pid_manager.start_awake", return_value=(True, "Bridge started (PID 10)")), \
             patch("app.pid_manager.start_runner", return_value=(True, "Agent loop started (PID 20)")):
            results = start_all(tmp_path, provider="local")

        assert "ollama" in results
        assert "awake" in results
        assert "run" in results
        mock_ollama.assert_called_once()

    def test_auto_detects_provider(self, tmp_path):
        """When provider is None, auto-detect from config."""
        with patch("app.pid_manager._detect_provider", return_value="copilot") as mock_detect, \
             patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            results = start_all(tmp_path)

        mock_detect.assert_called_once_with(tmp_path)
        assert "ollama" not in results

    def test_auto_detects_local_starts_ollama(self, tmp_path):
        """Auto-detect local provider should start ollama."""
        with patch("app.pid_manager._detect_provider", return_value="local"), \
             patch("app.pid_manager.start_ollama", return_value=(True, "ok")), \
             patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            results = start_all(tmp_path)

        assert "ollama" in results

    def test_continues_if_awake_fails(self, tmp_path):
        """If awake fails, run should still be attempted."""
        with patch("app.pid_manager.start_awake", return_value=(False, "PID not detected")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            results = start_all(tmp_path, provider="claude")

        ok_awake, _ = results["awake"]
        assert ok_awake is False
        ok_run, _ = results["run"]
        assert ok_run is True

    def test_continues_if_ollama_fails(self, tmp_path):
        """If ollama fails, awake and run should still be attempted."""
        with patch("app.pid_manager.start_ollama", return_value=(False, "not found")), \
             patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            results = start_all(tmp_path, provider="local")

        ok_ollama, _ = results["ollama"]
        assert ok_ollama is False
        ok_run, _ = results["run"]
        assert ok_run is True

    def test_all_already_running(self, tmp_path):
        """If everything is already running, report correctly."""
        with patch("app.pid_manager.start_ollama", return_value=(False, "already running (PID 1)")), \
             patch("app.pid_manager.start_awake", return_value=(False, "Bridge already running (PID 2)")), \
             patch("app.pid_manager.start_runner", return_value=(False, "Agent loop already running (PID 3)")):
            results = start_all(tmp_path, provider="local")

        for name in ("ollama", "awake", "run"):
            ok, msg = results[name]
            assert ok is False
            assert "already running" in msg.lower()


# ---------------------------------------------------------------------------
# start_stack (backward compat — delegates to start_all)
# ---------------------------------------------------------------------------


class TestShowStartupBanner:
    """Test _show_startup_banner integration in start_all."""

    def test_banner_called_before_processes(self, tmp_path):
        """Startup banner should display before launching processes."""
        call_order = []
        with patch("app.pid_manager._show_startup_banner", side_effect=lambda *a: call_order.append("banner")), \
             patch("app.pid_manager.start_awake", side_effect=lambda *a: (call_order.append("awake"), (True, "ok"))[-1]), \
             patch("app.pid_manager.start_runner", side_effect=lambda *a: (call_order.append("run"), (True, "ok"))[-1]):
            start_all(tmp_path, provider="claude")
        assert call_order == ["banner", "awake", "run"]

    def test_banner_exception_does_not_block_startup(self, tmp_path):
        """If banner gathering fails, processes should still start."""
        with patch("app.banners.print_startup_banner", side_effect=Exception("render error")), \
             patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            results = start_all(tmp_path, provider="claude")
        assert results["awake"] == (True, "ok")
        assert results["run"] == (True, "ok")

    def test_banner_receives_provider(self, tmp_path):
        """_show_startup_banner should receive koan_root and detected provider."""
        with patch("app.pid_manager._show_startup_banner") as mock_banner, \
             patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            start_all(tmp_path, provider="copilot")
        mock_banner.assert_called_once_with(tmp_path, "copilot")


class TestStartStack:
    def test_delegates_to_start_all_with_local_provider(self, tmp_path):
        """start_stack should call start_all with provider='local'."""
        with patch("app.pid_manager.start_all", return_value={"ollama": (True, "ok"), "awake": (True, "ok"), "run": (True, "ok")}) as mock:
            results = start_stack(tmp_path)

        mock.assert_called_once_with(tmp_path, provider="local")
        assert "ollama" in results

    def test_returns_all_three_components(self, tmp_path):
        with patch("app.pid_manager.start_ollama", return_value=(True, "ok")), \
             patch("app.pid_manager.start_awake", return_value=(True, "ok")), \
             patch("app.pid_manager.start_runner", return_value=(True, "ok")):
            results = start_stack(tmp_path)

        assert "ollama" in results
        assert "awake" in results
        assert "run" in results


# ---------------------------------------------------------------------------
# CLI: start-all, start-ollama, and start-stack
# ---------------------------------------------------------------------------


class TestCLIStartAll:
    def _run_cli(self, *args):
        cmd = [sys.executable, "-m", "app.pid_manager"] + list(args)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_start_all_with_claude_provider(self, tmp_path):
        """start-all claude should show awake and run, no ollama."""
        result = self._run_cli("start-all", str(tmp_path), "claude")
        # Should have output for awake and run (both will fail — no koan/ dir)
        assert "awake:" in result.stdout
        assert "run:" in result.stdout
        # Should NOT have ollama
        assert "ollama:" not in result.stdout

    def test_start_all_with_local_provider(self, tmp_path):
        """start-all local should show ollama, awake, and run."""
        result = self._run_cli("start-all", str(tmp_path), "local")
        assert "ollama:" in result.stdout
        assert "awake:" in result.stdout or "run:" in result.stdout

    def test_start_all_auto_detect(self, tmp_path):
        """start-all without provider arg should auto-detect."""
        result = self._run_cli("start-all", str(tmp_path))
        # Default is claude — should have awake + run, no ollama
        assert "awake:" in result.stdout
        assert "run:" in result.stdout


class TestCLIOllama:
    def _run_cli(self, *args):
        cmd = [sys.executable, "-m", "app.pid_manager"] + list(args)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_start_ollama_cli_already_running(self, tmp_path):
        pidfile = tmp_path / ".koan-pid-ollama"
        pidfile.write_text(str(os.getpid()))

        result = self._run_cli("start-ollama", str(tmp_path))
        assert result.returncode == 1
        assert "already running" in result.stdout

    def test_start_stack_cli_output_format(self, tmp_path):
        """start-stack CLI prints status for each component."""
        # ollama + awake + run will all fail (no ollama binary, no koan/ dir)
        # but the output format should still be correct
        result = self._run_cli("start-stack", str(tmp_path))
        # Should have output for each component
        assert "ollama:" in result.stdout
        assert "awake:" in result.stdout or "run:" in result.stdout

    def test_status_all_hides_ollama_for_claude_provider(self, tmp_path):
        """status-all should NOT show ollama when provider is claude (default)."""
        result = self._run_cli("status-all", str(tmp_path))
        assert result.returncode == 0
        assert "not running" in result.stdout
        assert "ollama" not in result.stdout

    def test_status_all_shows_ollama_for_local_provider(self, tmp_path):
        """status-all should show ollama when provider is local."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        env["KOAN_CLI_PROVIDER"] = "local"
        cmd = [sys.executable, "-m", "app.pid_manager", "status-all", str(tmp_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert result.returncode == 0
        assert "ollama: not running" in result.stdout

    def test_stop_all_includes_ollama(self, tmp_path):
        result = self._run_cli("stop-all", str(tmp_path))
        assert result.returncode == 0
        assert "ollama: not running" in result.stdout


# ---------------------------------------------------------------------------
# Log file management
# ---------------------------------------------------------------------------


class TestLogDir:
    def test_creates_logs_directory(self, tmp_path):
        d = _log_dir(tmp_path)
        assert d == tmp_path / "logs"
        assert d.is_dir()

    def test_idempotent_creation(self, tmp_path):
        _log_dir(tmp_path)
        _log_dir(tmp_path)
        assert (tmp_path / "logs").is_dir()


class TestOpenLogFile:
    def test_creates_log_file(self, tmp_path):
        fh = _open_log_file(tmp_path, "run")
        fh.close()
        assert (tmp_path / "logs" / "run.log").exists()

    def test_truncates_existing_log(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "awake.log"
        log_file.write_text("old content\n")

        fh = _open_log_file(tmp_path, "awake")
        fh.write("new content\n")
        fh.close()

        assert log_file.read_text() == "new content\n"

    def test_file_writable(self, tmp_path):
        fh = _open_log_file(tmp_path, "ollama")
        fh.write("test line\n")
        fh.close()
        assert (tmp_path / "logs" / "ollama.log").read_text() == "test line\n"


class TestStarterLogFiles:
    """Verify that start_runner/start_awake/start_ollama redirect to log files."""

    def test_start_runner_creates_log_file(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen") as mock_popen, \
             patch("app.pid_manager.check_pidfile", side_effect=[None, None, 42]):
            start_runner(tmp_path, verify_timeout=1.0)

        call_args = mock_popen.call_args
        # stdout should be an open file (not DEVNULL)
        stdout_arg = call_args[1]["stdout"]
        assert hasattr(stdout_arg, "name")
        assert "run.log" in stdout_arg.name
        # stderr should be STDOUT (merged with stdout)
        assert call_args[1]["stderr"] == subprocess.STDOUT

    def test_start_runner_sets_force_color(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen") as mock_popen, \
             patch("app.pid_manager.check_pidfile", side_effect=[None, None, 42]):
            start_runner(tmp_path, verify_timeout=1.0)

        env = mock_popen.call_args[1]["env"]
        assert env.get("KOAN_FORCE_COLOR") == "1"

    def test_start_awake_creates_log_file(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen") as mock_popen, \
             patch("app.pid_manager.check_pidfile", side_effect=[None, None, 42]):
            start_awake(tmp_path, verify_timeout=1.0)

        call_args = mock_popen.call_args
        stdout_arg = call_args[1]["stdout"]
        assert hasattr(stdout_arg, "name")
        assert "awake.log" in stdout_arg.name
        assert call_args[1]["stderr"] == subprocess.STDOUT

    def test_start_awake_sets_force_color(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen") as mock_popen, \
             patch("app.pid_manager.check_pidfile", side_effect=[None, None, 42]):
            start_awake(tmp_path, verify_timeout=1.0)

        env = mock_popen.call_args[1]["env"]
        assert env.get("KOAN_FORCE_COLOR") == "1"

    def test_start_ollama_creates_log_file(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 54321

        with patch("app.pid_manager.shutil.which", return_value="/usr/local/bin/ollama"), \
             patch("app.pid_manager.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("app.pid_manager._is_process_alive", return_value=True):
            start_ollama(tmp_path, verify_timeout=0.5)

        call_args = mock_popen.call_args
        stdout_arg = call_args[1]["stdout"]
        assert hasattr(stdout_arg, "name")
        assert "ollama.log" in stdout_arg.name
        assert call_args[1]["stderr"] == subprocess.STDOUT

    def test_start_runner_logs_dir_created(self, tmp_path):
        with patch("app.pid_manager.subprocess.Popen"), \
             patch("app.pid_manager.check_pidfile", side_effect=[None, None, 42]):
            start_runner(tmp_path, verify_timeout=1.0)

        assert (tmp_path / "logs").is_dir()


class TestPrintStackResults:
    def test_shows_ux_hints_on_success(self, capsys):
        results = {
            "awake": (True, "Bridge started (PID 42)"),
            "run": (True, "Agent loop started (PID 43)"),
        }
        code = _print_stack_results(results)
        assert code == 0
        output = capsys.readouterr().out
        assert "make logs" in output
        assert "make status" in output
        assert "make stop" in output

    def test_no_hints_on_failure(self, capsys):
        results = {
            "awake": (False, "Failed to launch"),
            "run": (True, "Agent loop started (PID 43)"),
        }
        code = _print_stack_results(results)
        assert code == 1
        output = capsys.readouterr().out
        assert "make logs" not in output

    def test_hints_shown_when_already_running(self, capsys):
        """'already running' is not a failure — show hints."""
        results = {
            "awake": (False, "Bridge already running (PID 42)"),
            "run": (True, "Agent loop started (PID 43)"),
        }
        code = _print_stack_results(results)
        assert code == 0
        output = capsys.readouterr().out
        assert "make logs" in output


# ---------------------------------------------------------------------------
# _launch_python_process
# ---------------------------------------------------------------------------


class TestLaunchPythonProcess:
    """Tests for _launch_python_process() — core process launcher."""

    def test_returns_already_running_if_pidfile_locked(self, tmp_path):
        """Should return early if check_pidfile() finds a running process."""
        with patch("app.pid_manager.check_pidfile", return_value=1234):
            ok, msg = _launch_python_process(tmp_path, "app/run.py", "run", 1.0)
        assert not ok
        assert "already running" in msg.lower()
        assert "1234" in msg

    def test_returns_already_running_for_awake(self, tmp_path):
        with patch("app.pid_manager.check_pidfile", return_value=5678):
            ok, msg = _launch_python_process(tmp_path, "app/awake.py", "awake", 1.0)
        assert not ok
        assert "Awake already running (PID 5678)" == msg

    def test_builds_correct_environment(self, tmp_path):
        """Verify KOAN_ROOT, PYTHONPATH, KOAN_FORCE_COLOR are set."""
        captured_env = {}

        def capture_popen(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", side_effect=capture_popen), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            _launch_python_process(tmp_path, "app/run.py", "run", 0.1)

        assert captured_env["KOAN_ROOT"] == str(tmp_path)
        assert captured_env["PYTHONPATH"] == str(tmp_path / "koan")
        assert captured_env["KOAN_FORCE_COLOR"] == "1"

    def test_uses_start_new_session(self, tmp_path):
        """Process must be launched in its own session group."""
        captured_kwargs = {}

        def capture_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", side_effect=capture_popen), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            _launch_python_process(tmp_path, "app/run.py", "run", 0.1)

        assert captured_kwargs["start_new_session"] is True
        assert captured_kwargs["stdin"] == subprocess.DEVNULL

    def test_returns_success_when_pid_appears(self, tmp_path):
        """PID file appearing within timeout -> success."""
        call_count = [0]

        def check_pid(*args):
            call_count[0] += 1
            if call_count[0] >= 3:
                return 42
            return None

        with patch("app.pid_manager.check_pidfile", side_effect=check_pid), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("app.pid_manager.time") as mock_time:
            # First call: check if already running (None)
            # Verification loop: monotonic calls for deadline check
            mock_time.monotonic.side_effect = [0.0, 0.1, 0.2, 0.3]
            mock_time.sleep = MagicMock()
            ok, msg = _launch_python_process(tmp_path, "app/run.py", "run", 5.0)

        assert ok
        assert "Agent loop started" in msg
        assert "42" in msg

    def test_bridge_label_for_awake(self, tmp_path):
        """process_name='awake' should use 'Bridge' label."""
        call_count = [0]

        def check_pid(*args):
            call_count[0] += 1
            if call_count[0] >= 2:
                return 99
            return None

        with patch("app.pid_manager.check_pidfile", side_effect=check_pid), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1, 0.2]
            mock_time.sleep = MagicMock()
            ok, msg = _launch_python_process(tmp_path, "app/awake.py", "awake", 5.0)

        assert ok
        assert "Bridge started" in msg

    def test_returns_failure_when_pid_never_appears(self, tmp_path):
        """PID file never appears -> timeout failure."""
        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            mock_time.sleep = MagicMock()
            ok, msg = _launch_python_process(tmp_path, "app/run.py", "run", 1.0)

        assert not ok
        assert "PID not detected" in msg

    def test_closes_log_on_popen_exception(self, tmp_path):
        """Log file must be closed if Popen raises."""
        mock_log = MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=mock_log), \
             patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            ok, msg = _launch_python_process(tmp_path, "app/run.py", "run", 1.0)

        assert not ok
        assert "Failed to launch" in msg
        assert "spawn failed" in msg
        mock_log.close.assert_called_once()

    def test_closes_log_on_success(self, tmp_path):
        """Log file handle must be closed in the parent after Popen succeeds."""
        mock_log = MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=mock_log), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            mock_time.sleep = MagicMock()
            _launch_python_process(tmp_path, "app/run.py", "run", 0.1)

        mock_log.close.assert_called_once()

    def test_uses_sys_executable(self, tmp_path):
        """Should use sys.executable as the Python binary."""
        captured_cmd = []

        def capture_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", side_effect=capture_popen), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            _launch_python_process(tmp_path, "app/run.py", "run", 0.1)

        assert captured_cmd[0] == sys.executable
        assert captured_cmd[1] == "app/run.py"

    def test_cwd_is_koan_dir(self, tmp_path):
        """Working directory should be koan_root/koan."""
        captured_kwargs = {}

        def capture_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", side_effect=capture_popen), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            _launch_python_process(tmp_path, "app/run.py", "run", 0.1)

        assert captured_kwargs["cwd"] == str(tmp_path / "koan")

    def test_stderr_merged_to_stdout(self, tmp_path):
        """stderr should be merged into stdout (same log file)."""
        captured_kwargs = {}

        def capture_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", side_effect=capture_popen), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            _launch_python_process(tmp_path, "app/run.py", "run", 0.1)

        assert captured_kwargs["stderr"] == subprocess.STDOUT

    def test_verification_polls_with_sleep(self, tmp_path):
        """Verification loop should sleep between checks."""
        call_count = [0]

        def check_pid(*args):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # Initial check: not running
            if call_count[0] == 4:
                return 77  # Found on 3rd poll
            return None

        with patch("app.pid_manager.check_pidfile", side_effect=check_pid), \
             patch("app.pid_manager._open_log_file", return_value=MagicMock()), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1, 0.4, 0.7, 1.0]
            mock_time.sleep = MagicMock()
            ok, msg = _launch_python_process(tmp_path, "app/run.py", "run", 5.0)

        assert ok
        # Sleep should have been called during polling
        mock_time.sleep.assert_called_with(0.3)


# ---------------------------------------------------------------------------
# _wait_for_exit
# ---------------------------------------------------------------------------


class TestWaitForExit:
    """Tests for _wait_for_exit() — graceful process termination waiter."""

    def test_returns_true_when_child_reaped(self):
        """waitpid successfully reaps a child -> True."""
        with patch("os.waitpid", return_value=(42, 0)), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1]
            result = _wait_for_exit(42, 5.0)
        assert result is True

    def test_returns_true_when_process_dead_via_kill_probe(self):
        """Non-child process dies -> _is_process_alive returns False -> True."""
        with patch("os.waitpid", side_effect=ChildProcessError("not our child")), \
             patch("app.pid_manager._is_process_alive", return_value=False), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1]
            result = _wait_for_exit(999, 5.0)
        assert result is True

    def test_returns_false_when_timeout_exceeded(self):
        """Process still alive after timeout -> False."""
        with patch("os.waitpid", side_effect=ChildProcessError()), \
             patch("app.pid_manager._is_process_alive", return_value=True), \
             patch("app.pid_manager.time") as mock_time:
            # First monotonic: start, second: already past deadline
            mock_time.monotonic.side_effect = [0.0, 0.1, 999.0]
            mock_time.sleep = MagicMock()
            result = _wait_for_exit(123, 1.0)
        assert result is False

    def test_polls_with_sleep_intervals(self):
        """Should sleep 0.2s between polls."""
        poll_count = [0]

        def fake_waitpid(pid, flags):
            poll_count[0] += 1
            raise ChildProcessError()

        with patch("os.waitpid", side_effect=fake_waitpid), \
             patch("app.pid_manager._is_process_alive", return_value=True), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1, 0.3, 999.0]
            mock_time.sleep = MagicMock()
            _wait_for_exit(123, 1.0)

        mock_time.sleep.assert_called_with(0.2)

    def test_waitpid_zero_means_not_yet_exited(self):
        """waitpid returning (0, 0) means child hasn't exited yet."""
        call_count = [0]

        def fake_waitpid(pid, flags):
            call_count[0] += 1
            if call_count[0] >= 3:
                return (pid, 0)  # Reaped
            return (0, 0)  # Not exited yet

        with patch("os.waitpid", side_effect=fake_waitpid), \
             patch("app.pid_manager._is_process_alive", return_value=True), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1, 0.3, 0.5, 0.7]
            mock_time.sleep = MagicMock()
            result = _wait_for_exit(42, 5.0)

        assert result is True

    def test_child_process_error_falls_through_to_kill_probe(self):
        """ChildProcessError means not our child — verify kill probe is used."""
        alive_checks = []

        def track_alive(pid):
            alive_checks.append(pid)
            return False  # Process is dead

        with patch("os.waitpid", side_effect=ChildProcessError()), \
             patch("app.pid_manager._is_process_alive", side_effect=track_alive), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1]
            result = _wait_for_exit(789, 5.0)

        assert result is True
        assert 789 in alive_checks

    def test_final_alive_check_on_timeout(self):
        """After loop exits, final _is_process_alive determines return value."""
        check_count = [0]

        def dynamic_alive(pid):
            check_count[0] += 1
            if check_count[0] <= 2:
                return True  # Alive during loop
            return False  # Dead on final check

        with patch("os.waitpid", side_effect=ChildProcessError()), \
             patch("app.pid_manager._is_process_alive", side_effect=dynamic_alive), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.1, 0.3, 999.0]
            mock_time.sleep = MagicMock()
            result = _wait_for_exit(42, 1.0)

        # Process died just as timeout expired — final check returns True
        assert result is True

    def test_zero_timeout_checks_once(self):
        """With timeout=0, should still do at least a final alive check."""
        with patch("os.waitpid", side_effect=ChildProcessError()), \
             patch("app.pid_manager._is_process_alive", return_value=False), \
             patch("app.pid_manager.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 999.0]
            mock_time.sleep = MagicMock()
            result = _wait_for_exit(42, 0)

        assert result is True


class TestStartRunnerClearsPause:
    """Bug fix: /start must clear .koan-pause so runner doesn't enter pause mode."""

    def test_clears_pause_file_on_start(self, tmp_path):
        """start_runner removes .koan-pause so the agent starts fresh."""
        pause_file = tmp_path / ".koan-pause"
        pause_file.write_text("2026-02-24T14:00:00")

        with patch("app.pid_manager.subprocess.Popen"), \
             patch("app.pid_manager.check_pidfile", side_effect=[None] * 10):
            start_runner(tmp_path, verify_timeout=0.5)

        assert not pause_file.exists()

    def test_clears_pause_with_reason_on_start(self, tmp_path):
        """start_runner removes .koan-pause (which contains reason data)."""
        pause_file = tmp_path / ".koan-pause"
        pause_file.write_text("errors\n1234567890\n")

        with patch("app.pid_manager.subprocess.Popen"), \
             patch("app.pid_manager.check_pidfile", side_effect=[None] * 10):
            start_runner(tmp_path, verify_timeout=0.5)

        assert not pause_file.exists()

    def test_clears_all_signals_on_start(self, tmp_path):
        """start_runner clears stop and pause together."""
        stop_file = tmp_path / ".koan-stop"
        stop_file.write_text("STOP")
        pause_file = tmp_path / ".koan-pause"
        pause_file.write_text("manual\n1234567890\n")

        with patch("app.pid_manager.subprocess.Popen"), \
             patch("app.pid_manager.check_pidfile", side_effect=[None] * 10):
            start_runner(tmp_path, verify_timeout=0.5)

        assert not stop_file.exists()
        assert not pause_file.exists()

    def test_no_error_when_pause_files_absent(self, tmp_path):
        """start_runner doesn't crash if pause files don't exist."""
        with patch("app.pid_manager.subprocess.Popen"), \
             patch("app.pid_manager.check_pidfile", side_effect=[None] * 10):
            # Should not raise
            start_runner(tmp_path, verify_timeout=0.5)
