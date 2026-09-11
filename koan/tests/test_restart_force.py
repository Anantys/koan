"""Tests for /restart --force — restart without waiting for the current mission.

Plain /restart is polite: the runner finishes its mission first. --force marks
the request on disk and signals the runner (SIGUSR2) so it kills the in-flight
mission and exits with RESTART_EXIT_CODE immediately.
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from app.restart_manager import (
    FORCE_SIGNAL_CAP,
    RESTART_BRIDGE_FILE,
    RESTART_EXIT_CODE,
    RESTART_RUN_FILE,
    RUN_CAPS_FILE,
    clear_runner_caps,
    declare_runner_caps,
    is_force_restart,
    request_restart,
    runner_supports_force_signal,
)
from app.skills import SkillContext

# Caps records are start-time-verified, so the tests below — which use PIDs
# that do not exist on the test host — need a deterministic, per-PID stand-in
# for the live start time. Distinct per PID so a "recycled PID" case can be
# expressed by simply moving the answer for that PID.
_FAKE_START = 1_700_000_000.0


@pytest.fixture(autouse=True)
def fake_process_start_times(monkeypatch):
    """Give every PID a stable, distinct fake start time."""
    monkeypatch.setattr(
        "app.restart_manager._runner_start_time", lambda pid: _FAKE_START + pid,
    )


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


class TestRunnerCaps:
    """The runner advertises SIGUSR2 only while it actually handles it."""

    def test_no_marker_means_unsupported(self, tmp_path):
        assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_declared_pid_supports_the_signal(self, tmp_path):
        declare_runner_caps(str(tmp_path), 4242)
        assert runner_supports_force_signal(str(tmp_path), 4242) is True

    def test_marker_from_another_incarnation_is_ignored(self, tmp_path):
        """A stale marker must not vouch for a different (older) runner."""
        declare_runner_caps(str(tmp_path), 4242)
        assert runner_supports_force_signal(str(tmp_path), 777) is False

    def test_cleared_marker_withdraws_support(self, tmp_path):
        declare_runner_caps(str(tmp_path), 4242)
        clear_runner_caps(str(tmp_path))
        assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_clearing_a_missing_marker_is_a_noop(self, tmp_path):
        clear_runner_caps(str(tmp_path))  # must not raise

    def test_unreadable_marker_fails_closed(self, tmp_path):
        declare_runner_caps(str(tmp_path), 4242)
        with patch("builtins.open", side_effect=PermissionError("EACCES")):
            assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_marker_outliving_its_writer_does_not_vouch_for_a_reused_pid(
            self, tmp_path, monkeypatch):
        """The one case the gate exists for: OOM-kill, then a PID reuse.

        A SIGKILLed runner never reaches ``clear_runner_caps``, so its marker
        survives. If a later runner — possibly a rollback with no SIGUSR2
        handler — starts on the same PID, only the start time tells them apart.
        """
        declare_runner_caps(str(tmp_path), 4242)
        monkeypatch.setattr(
            "app.restart_manager._runner_start_time",
            lambda pid: _FAKE_START + pid + 3600,
        )
        assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_start_time_within_tolerance_still_vouches(
            self, tmp_path, monkeypatch):
        """Coarse `ps etime` granularity must not break a live runner."""
        declare_runner_caps(str(tmp_path), 4242)
        monkeypatch.setattr(
            "app.restart_manager._runner_start_time",
            lambda pid: _FAKE_START + pid + 2,
        )
        assert runner_supports_force_signal(str(tmp_path), 4242) is True

    def test_marker_without_a_start_time_fails_closed(self, tmp_path):
        """A host that could not read its own start time gets no forced path."""
        (tmp_path / RUN_CAPS_FILE).write_text(f"pid=4242\n{FORCE_SIGNAL_CAP}\n")
        assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_declare_omits_an_unreadable_start_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.restart_manager._runner_start_time", lambda pid: None)
        declare_runner_caps(str(tmp_path), 4242)
        assert "start=" not in (tmp_path / RUN_CAPS_FILE).read_text()
        assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_unparseable_start_time_fails_closed(self, tmp_path):
        (tmp_path / RUN_CAPS_FILE).write_text(
            f"pid=4242\nstart=not-a-number\n{FORCE_SIGNAL_CAP}\n")
        assert runner_supports_force_signal(str(tmp_path), 4242) is False

    def test_live_start_time_unreadable_fails_closed(self, tmp_path, monkeypatch):
        declare_runner_caps(str(tmp_path), 4242)
        monkeypatch.setattr(
            "app.restart_manager._runner_start_time", lambda pid: None)
        assert runner_supports_force_signal(str(tmp_path), 4242) is False


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

        declare_runner_caps(str(tmp_path), 4242)
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

        declare_runner_caps(str(tmp_path), 4242)
        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill") as mock_kill:
            result = handle(self._ctx(tmp_path, args))

        mock_kill.assert_called_once_with(4242, signal.SIGUSR2)
        assert is_force_restart(str(tmp_path), target="run") is True
        assert is_force_restart(str(tmp_path), target="bridge") is True
        assert "Force restart" in result

    def test_force_does_not_signal_a_runner_without_the_sigusr2_cap(
            self, tmp_path):
        """A pre-upgrade runner would be terminated, orphaning its provider."""
        from skills.core.restart.handler import handle

        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill") as mock_kill:
            result = handle(self._ctx(tmp_path, "--force"))

        mock_kill.assert_not_called()
        assert "polite restart" in result
        # The polite markers still land, so the old runner restarts after the
        # mission it is on — the behaviour it does understand.
        assert (tmp_path / RESTART_RUN_FILE).exists()
        assert (tmp_path / RESTART_BRIDGE_FILE).exists()

    def test_force_does_not_signal_a_stale_caps_marker(self, tmp_path):
        """Caps left by a crashed newer runner must not vouch for this PID."""
        from skills.core.restart.handler import handle

        declare_runner_caps(str(tmp_path), 111)
        with patch("app.pid_manager.check_pidfile", return_value=4242), \
             patch("app.pid_manager._cmdline_matches", return_value=True), \
             patch("os.kill") as mock_kill:
            handle(self._ctx(tmp_path, "--force"))

        mock_kill.assert_not_called()

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

        declare_runner_caps(str(tmp_path), 4242)
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

        declare_runner_caps(str(tmp_path), 99999)
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

    def test_signal_is_deferred_until_the_subprocess_is_published(
            self, monkeypatch):
        """A forced restart mid-publication must kill the mission, not orphan it.

        The runner always has threads that leave SIGUSR2 unblocked (journal
        tail, watchdog, stagnation monitor), so a process-directed kill lands
        on one of them and CPython still runs the handler on the main thread.
        Reproduce that topology: signal from a second thread, then publish.
        """
        from app import run

        class FakeProc:
            def poll(self):
                return None

        proc = FakeProc()
        killed = []
        monkeypatch.setattr(run._sig, "claude_proc", None)
        monkeypatch.setattr(run._sig, "task_running", True)
        monkeypatch.setattr(run, "_kill_process_group", killed.append)

        # A thread that leaves SIGUSR2 unblocked, started before the guard just
        # like run_claude_task's journal tail. The kernel delivers the
        # process-directed signal here when the main thread masks it.
        release = threading.Event()
        bystander = threading.Thread(target=release.wait, daemon=True)
        bystander.start()

        previous = signal.signal(signal.SIGUSR2, run._on_sigusr2)
        try:
            with pytest.raises(SystemExit) as exc:
                with run._sigusr2_deferred():
                    os.kill(os.getpid(), signal.SIGUSR2)
                    time.sleep(0.05)  # let a non-deferred handler fire
                    run._sig.claude_proc = proc
        finally:
            signal.signal(signal.SIGUSR2, previous)
            release.set()
            bystander.join(timeout=1)

        assert exc.value.code == RESTART_EXIT_CODE
        assert killed == [proc]


class TestForcedMarkerFallback:
    """If SIGUSR2 is lost, the mission poll loop must still restart."""

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path, monkeypatch):
        """Keep run_claude_task off host-wide state.

        Its real path takes the per-uid provider invocation lock (serialising
        against any other Kōan process on the machine) and, in its finally,
        sweeps stray /tmp trees — including a concurrent pytest run's
        ``pytest-of-*`` dirs — and drops the page cache.

        The lock is acquired by ``run_claude_task`` itself (hoisted out of
        ``popen_cli``), so stubbing ``popen_cli`` does not bypass it —
        ``koan_tmp_dir`` is redirected here so the lock file is per-test.
        """
        def fake_popen_cli(cmd, provider=None, launcher=None, cli_lock=None,
                           **kwargs):
            kwargs.pop("stdin", None)
            argv = list(launcher) + list(cmd) if launcher else cmd
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **kwargs)
            return proc, lambda: (cli_lock.release() if cli_lock else None)

        monkeypatch.setattr("app.utils.koan_tmp_dir", lambda: str(tmp_path))
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


class TestForcedRestartWhileProviderLockContended:
    """The provider invocation lock must not swallow a forced restart.

    Providers such as codex serialize invocations through a per-uid flock that
    a peer Kōan can hold for an entire mission. The runner waits for it before
    forking, so /restart --force must be honoured *during* that wait — nothing
    has been spawned yet, so there is nothing to orphan.
    """

    def test_sigusr2_honoured_while_blocked_on_provider_lock(
            self, tmp_path, monkeypatch):
        import fcntl

        from app import run
        from app.provider.codex import CodexProvider

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setattr("app.utils.koan_tmp_dir", lambda: str(tmp_path))
        monkeypatch.setattr("app.utils.sweep_stray_tmp_dirs", lambda *a, **k: [])
        monkeypatch.setattr("app.page_cache.run_reclaim", lambda *a, **k: None)

        def unreachable_popen_cli(cmd, provider=None, cli_lock=None, **kwargs):
            raise AssertionError("mission was spawned despite a forced restart")

        monkeypatch.setattr("app.cli_exec.popen_cli", unreachable_popen_cli)

        # A peer holds codex's invocation lock. Released from a thread purely as
        # a safety valve so a regression fails loudly instead of hanging.
        holder = open(tmp_path / "codex-cli.lock", "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        released = threading.Event()

        def hold_then_release():
            released.wait(timeout=5)
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

        def send_force_restart():
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGUSR2)

        releaser = threading.Thread(target=hold_then_release, daemon=True)
        sender = threading.Thread(target=send_force_restart, daemon=True)
        previous = signal.signal(signal.SIGUSR2, run._on_sigusr2)
        started = time.monotonic()
        try:
            releaser.start()
            sender.start()
            with pytest.raises(SystemExit) as exc:
                run.run_claude_task(
                    cmd=["sleep", "30"],
                    stdout_file=str(tmp_path / "out.txt"),
                    stderr_file=str(tmp_path / "err.txt"),
                    cwd=str(tmp_path),
                    provider=CodexProvider(),
                )
        finally:
            elapsed = time.monotonic() - started
            # Join the sender BEFORE restoring the handler. If the body returns
            # early (the regression this test targets: the lock is not waited
            # on, so unreachable_popen_cli raises at ~0s), restoring first would
            # let the in-flight SIGUSR2 land on the default disposition and
            # terminate pytest itself — killing the session instead of
            # reporting a failure.
            sender.join(timeout=5)
            signal.signal(signal.SIGUSR2, previous)
            released.set()
            releaser.join(timeout=2)
            holder.close()

        assert exc.value.code == RESTART_EXIT_CODE
        # Honoured during the wait, not after the peer happened to let go.
        assert elapsed < 3

    def test_missing_provider_binary_releases_the_hoisted_lock(
            self, tmp_path, monkeypatch):
        """Exit 127 must not strand the per-uid provider lock.

        ``mission_scope`` pre-checks the binary *before* spawning, so on this
        path ``popen_cli`` never receives the hoisted lock and cannot release
        it. If ``run_claude_task`` returns without releasing it itself, the next
        mission's ``acquire_provider_lock`` polls LOCK_NB forever and the agent
        loop wedges.
        """
        from app import mission_scope, run
        from app.cli_exec import _ProviderInvocationLock, acquire_provider_lock
        from app.provider.codex import CodexProvider

        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setattr("app.utils.koan_tmp_dir", lambda: str(tmp_path))
        monkeypatch.setattr("app.utils.sweep_stray_tmp_dirs", lambda *a, **k: [])
        monkeypatch.setattr("app.page_cache.run_reclaim", lambda *a, **k: None)
        # Force the systemd-run branch: that is where _require_executable runs,
        # and it is the only one that raises before the spawn hand-over.
        monkeypatch.setattr(mission_scope, "systemd_run", lambda: ("/bin/true", []))

        def unreachable_popen_cli(cmd, **kwargs):
            raise AssertionError("spawned despite a missing provider binary")

        monkeypatch.setattr("app.cli_exec.popen_cli", unreachable_popen_cli)

        # Keep a strong reference to the lock the runner takes. Without one,
        # CPython would finalize the dead frame's file handle and drop the
        # flock implicitly — masking a missing explicit release.
        taken = []

        def recording_acquire(provider):
            lock = acquire_provider_lock(provider)
            taken.append(lock)
            return lock

        monkeypatch.setattr(
            "app.cli_exec.acquire_provider_lock", recording_acquire,
        )

        exit_code = run.run_claude_task(
            cmd=["koan-provider-that-does-not-exist"],
            stdout_file=str(tmp_path / "out.txt"),
            stderr_file=str(tmp_path / "err.txt"),
            cwd=str(tmp_path),
            provider=CodexProvider(),
        )

        assert exit_code == 127
        assert taken and taken[0].acquired, "runner never took the lock"

        # flock ownership is per open-file-description, so a second open in this
        # same process contends with a leaked one exactly like a peer Kōan would.
        retry = _ProviderInvocationLock(CodexProvider().invocation_lock_name())
        acquired = threading.Event()

        def _take_fresh_lock():
            retry.__enter__()
            acquired.set()

        worker = threading.Thread(target=_take_fresh_lock, daemon=True)
        worker.start()
        assert acquired.wait(5), "exit 127 leaked the lock; the next mission hangs"
        assert retry.acquired
        retry.release()
