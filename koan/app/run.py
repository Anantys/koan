"""Kōan — Main agent loop.

Manages the agent loop: mission picking, Claude CLI execution,
post-mission processing, pause/resume, signal handling, and
lifecycle notifications.

Usage:
    python -m app.run              # Normal start

Features:
- Double-tap CTRL-C protection across ALL phases (missions, rituals,
  sleep, startup, git sync). First press shows warning with current
  activity name; second press within 10s aborts.
- Automatic exception recovery with backoff (survives crashes)
- protected_phase() context manager for easy phase protection
- Restart wrapper: a restart signal (exit code 42) triggers os.execv so
  the interpreter reloads updated code from disk — without this, /update
  and auto-update would pull new code yet keep running the old modules
  already imported into this long-lived process.
- Process group isolation for Claude subprocess (SIGINT ignored)
- Colored log output with TTY detection
"""

import contextlib
import os
import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import List, Optional, Tuple

from app.constants import IDLE_LOOP_BREATH_SECONDS
from app.iteration_manager import plan_iteration
from app.loop_manager import check_pending_missions, interruptible_sleep
from app.pid_manager import acquire_pidfile, release_pidfile
from app.restart_manager import (
    check_restart,
    clear_restart,
    clear_runner_caps,
    declare_runner_caps,
    is_force_restart,
    RESTART_EXIT_CODE,
    RESTART_RUN_FILE,
)
from app.run_log import (  # noqa: F401 — re-exported for backward compat
    _ANSI_RESET,
    _CATEGORY_COLORS,
    _COLORS,
    _init_colors,
    _reset_terminal,
    _styled,
    bold_cyan,
    bold_green,
    log,
    suppress_logged,
)
from app.shutdown_manager import is_shutdown_requested, clear_shutdown
from app.signals import (
    CYCLE_FILE,
    CYCLE_RELEASE_FILE,
    PAUSE_FILE,
    PROJECT_FILE,
    RESET_COUNTER_FILE,
    SHUTDOWN_FILE,
    ABORT_FILE,
    STATUS_FILE,
    STOP_FILE,
)
from app.config import get_recovery_config
from app.messaging_level import is_debug
from app import mission_scope
from app.subprocess_runner import kill_process_group
from app.utils import atomic_write, koan_tmp_dir


# ---------------------------------------------------------------------------
# Recovery helpers
# ---------------------------------------------------------------------------

def _calculate_backoff(attempt: int, max_backoff: int) -> int:
    """Calculate linear backoff capped at max_backoff.

    Reads ``backoff_multiplier`` from ``recovery`` config section.
    Returns: attempt * multiplier, capped at max_backoff.
    """
    cfg = get_recovery_config()
    return min(cfg["backoff_multiplier"] * attempt, max_backoff)


def _should_notify_error(attempt: int) -> bool:
    """Determine if error notification should be sent.

    Notifies on first error and every ``error_notification_interval`` errors.
    """
    cfg = get_recovery_config()
    interval = cfg["error_notification_interval"]
    return attempt == 1 or attempt % interval == 0


def _provider_identity() -> Tuple[str, str]:
    """Return the active provider name and a human-friendly label.

    Centralizes the ``get_provider_name() + .title()`` lookup so notification
    text and quota/auth handlers stay consistent across mission, skill, and
    contemplative code paths.
    """
    from app.provider import get_provider_name

    name = get_provider_name()
    return name, name.title()


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------

def set_status(koan_root: str, message: str):
    """Write loop status for /status and dashboard."""
    try:
        atomic_write(Path(koan_root, STATUS_FILE), message)
    except Exception as e:
        log("error", f"Failed to write status: {e}")


def _build_startup_status(koan_root: str) -> str:
    """Build a human-readable status line for startup notification.

    Returns a status string like:
    - "✅ Active — ready to work"
    - "⏸️ Paused (quota) — resets 10am (Europe/Paris). Use /resume to unpause."
    - "⏸️ Paused (max_runs) — use /resume to unpause."
    """
    from app.pause_manager import get_pause_state

    if not Path(koan_root, PAUSE_FILE).exists():
        return "✅ Active — ready to work"

    state = get_pause_state(koan_root)
    if state and state.display:
        return f"⏸️ Paused ({state.reason}) — {state.display}. Use /resume to unpause."
    elif state:
        return f"⏸️ Paused ({state.reason}) — use /resume to unpause."
    else:
        return "⏸️ Paused — use /resume to unpause."


# ---------------------------------------------------------------------------
# Signal handling — double-tap CTRL-C
# ---------------------------------------------------------------------------

class SignalState:
    """Mutable state for SIGINT handler (double-tap pattern)."""
    task_running: bool = False
    first_ctrl_c: float = 0
    claude_proc: Optional[subprocess.Popen] = None
    timeout: int = 10
    phase: str = ""  # Human-readable description of current activity
    # Set while claude_proc is being published — see _sigusr2_deferred().
    defer_force_restart: bool = False
    pending_force_restart: str = ""


_sig = SignalState()


class protected_phase:
    """Context manager that activates double-tap CTRL-C protection.

    Usage:
        with protected_phase("Running morning ritual"):
            subprocess.run(...)

    First CTRL-C warns with the phase name.
    Second CTRL-C within timeout raises KeyboardInterrupt.
    """

    def __init__(self, phase_name: str):
        self.phase_name = phase_name
        self.prev_phase = ""
        self.prev_task_running = False

    def __enter__(self):
        self.prev_phase = _sig.phase
        self.prev_task_running = _sig.task_running
        _sig.phase = self.phase_name
        _sig.task_running = True
        _sig.first_ctrl_c = 0
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        _sig.phase = self.prev_phase
        _sig.task_running = self.prev_task_running
        _sig.first_ctrl_c = 0
        return False  # Don't suppress exceptions


def _kill_process_group(proc):
    """Delegate to :func:`app.subprocess_runner.kill_process_group`."""
    kill_process_group(proc)


def _on_sigint(signum, frame):
    """SIGINT handler: first press warns, second press aborts."""
    global _last_mission_aborted
    if not _sig.task_running:
        raise KeyboardInterrupt

    now = time.time()
    if _sig.first_ctrl_c > 0:
        elapsed = now - _sig.first_ctrl_c
        if elapsed <= _sig.timeout:
            # Second CTRL-C within timeout — abort
            print()
            log("koan", "Confirmed. Aborting...")
            # Record it the way /abort does (_on_sigusr1) *before* the kill:
            # this SIGKILL is ours, and mission_scope.teardown must not read
            # the resulting -9 exit as the memory cap firing — a cap hit is
            # never retried, so a hand-aborted mission would be mislabelled
            # and then permanently refused both retry paths.
            _last_mission_aborted = True
            _kill_process_group(_sig.claude_proc)
            _sig.first_ctrl_c = 0
            _sig.task_running = False
            raise KeyboardInterrupt

    # First CTRL-C (or timeout expired)
    _sig.first_ctrl_c = now
    print()
    phase_hint = f" ({_sig.phase})" if _sig.phase else ""
    log("koan", f"⚠️  Press CTRL-C again within {_sig.timeout}s to abort.{phase_hint}")


def _on_sigusr1(signum, frame):
    """SIGUSR1 handler: instant /abort from the bridge.

    The /abort skill writes ``.koan-abort`` and sends SIGUSR1 so the runner
    reacts within milliseconds instead of waiting up to ``proc.wait``'s 30 s
    poll cycle. Idempotent: a no-op when no Claude subprocess is running.
    """
    global _last_mission_aborted
    proc = _sig.claude_proc
    if proc is None or proc.poll() is not None:
        return

    _last_mission_aborted = True
    koan_root_path = os.environ.get("KOAN_ROOT", "")
    if koan_root_path:
        Path(koan_root_path, ABORT_FILE).unlink(missing_ok=True)
    log("koan", "Abort signal received — killing current mission")
    _kill_process_group(proc)


# How often the mission wait loop wakes to check abort / forced-restart signals.
MISSION_POLL_INTERVAL = 30

# Wall-clock time main_loop() started. Restart markers older than this are
# leftovers from a previous incarnation and must not force a restart. Stays
# 0.0 outside the daemon (tests, direct run_claude_task calls) → no filtering.
_runner_start_time = 0.0


@contextlib.contextmanager
def _sigusr2_deferred():
    """Hold off a forced restart until ``_sig.claude_proc`` is published.

    Closes the publish race in :func:`run_claude_task`: SIGUSR2 delivered
    between ``popen_cli()`` returning and ``_sig.claude_proc = proc`` would
    find no process, kill nothing, and re-exec — orphaning a provider process
    group started with ``start_new_session=True``.

    The block must stay *short*, or a forced restart is silently swallowed for
    its duration. :func:`run_claude_task` therefore takes the provider
    invocation lock (``cli_exec.acquire_provider_lock``) before entering: that
    is the one unbounded wait inside ``popen_cli``, and honouring a restart
    during it is safe precisely because nothing has been forked yet. What
    remains here is prompt-file setup plus ``fork``/``exec`` — milliseconds.

    The deferral is done in :func:`_on_sigusr2` rather than with
    ``pthread_sigmask``, because a signal mask is *per thread*: a
    process-directed ``kill()`` is accepted by any thread that has not blocked
    it (the runner always has some — the journal tail, the watchdog, the
    stagnation monitor), and CPython then runs the Python-level handler on the
    main thread regardless of the main thread's own mask. Recording the
    request and replaying it on exit is independent of that thread topology:
    the Python handler only ever runs on the main thread, which is the thread
    sitting inside this block.
    """
    _sig.pending_force_restart = ""
    _sig.defer_force_restart = True
    try:
        yield
    finally:
        _sig.defer_force_restart = False
        reason = _sig.pending_force_restart
        _sig.pending_force_restart = ""
        if reason:
            _force_restart_now(reason)


def _force_restart_now(reason: str):
    """Kill any in-flight mission and exit for re-launch — never returns.

    The forced-restart path (``/restart --force``). Raising ``SystemExit`` in
    the main thread unwinds through the mission's ``finally`` blocks (pidfile
    released, status cleared) up to :func:`main`, which re-execs on
    ``RESTART_EXIT_CODE``. The killed mission stays In Progress and is
    re-queued by crash recovery on the next startup — or failed by it, once
    the mission has exhausted ``max_crash_retries``.
    """
    log("koan", f"{reason} — killing current mission and restarting now")
    proc = _sig.claude_proc
    if proc is not None and proc.poll() is None:
        _kill_process_group(proc)
    elif _sig.task_running:
        # A mission is in flight but its subprocess is not published yet (or
        # already gone). Say so — a survivor would keep burning quota and
        # mutating the worktree after the re-exec, invisibly.
        log("warn", "Forced restart found no live mission subprocess to kill")
    raise SystemExit(RESTART_EXIT_CODE)


def _on_sigusr2(signum, frame):
    """SIGUSR2 handler: forced restart from the bridge (``/restart --force``).

    Unlike the between-missions restart check, this does not wait for the
    current mission. Sent by the /restart skill; the forced marker on disk
    is the fallback if the signal is lost.
    """
    reason = "Forced restart signal received"
    if _sig.defer_force_restart:
        # Mid-publication of the mission subprocess — restarting now would
        # orphan it. :func:`_sigusr2_deferred` replays this on block exit.
        _sig.pending_force_restart = reason
        return
    _force_restart_now(reason)


def _start_stagnation_monitor(stdout_file: str, proc, project_name: str):
    """Launch a StagnationMonitor for a running Claude subprocess.

    Returns ``None`` when stagnation detection is disabled (via config
    or per-project override) or if any setup error occurs — the monitor
    is strictly a best-effort safety net and must never block mission
    execution.
    """
    try:
        from app.config import get_stagnation_config
        from app.stagnation_monitor import StagnationMonitor
    except Exception as e:
        log("error", f"stagnation monitor import failed: {e}")
        return None

    try:
        cfg = get_stagnation_config(project_name)
    except Exception as e:
        log("error", f"stagnation config error: {e}")
        return None

    if not cfg.get("enabled", True):
        return None

    def _on_warn(count: int) -> None:
        log("koan", f"⚠️  Possible stagnation detected (identical output {count}x)")

    def _on_abort() -> None:
        log("error", "Stagnation confirmed — killing stuck Claude session")
        _kill_process_group(proc)

    try:
        monitor = StagnationMonitor(
            stdout_file=stdout_file,
            on_abort=_on_abort,
            on_warn=_on_warn,
            check_interval_seconds=cfg["check_interval_seconds"],
            abort_after_cycles=cfg["abort_after_cycles"],
            sample_lines=cfg["sample_lines"],
        )
        monitor.start()
        return monitor
    except Exception as e:
        log("error", f"stagnation monitor start failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Claude subprocess execution
# ---------------------------------------------------------------------------

def run_claude_task(
    cmd: list,
    stdout_file: str,
    stderr_file: str,
    cwd: str,
    instance_dir: str = "",
    project_name: str = "",
    run_num: int = 0,
    provider=None,
) -> int:
    """Run Claude CLI as a subprocess with SIGINT isolation and timeout.

    The child process ignores SIGINT (via preexec_fn) so the double-tap
    pattern works: first CTRL-C only warns the user, second kills the child.

    A watchdog timer kills the process if it exceeds the configured mission
    timeout (default 3600s). This prevents runaway sessions that block the
    entire agent loop.

    When *instance_dir* and *project_name* are provided and
    ``cli_output_journal`` is enabled, stdout is streamed to the project's
    daily journal file in real-time via a background tail thread.

    Returns the child exit code.
    """
    global _last_mission_timed_out, _last_mission_aborted
    global _last_mission_memory_cap
    global _stagnation_pattern_type, _stagnation_pattern_excerpt
    _last_mission_timed_out = False
    _last_mission_aborted = False
    _last_mission_memory_cap = ""
    _last_mission_stagnated.clear()
    _stagnation_pattern_type = ""
    _stagnation_pattern_excerpt = ""

    _sig.task_running = True
    _sig.first_ctrl_c = 0

    # Start journal streaming if configured
    journal_stream = None
    if instance_dir and project_name:
        from app.cli_journal_streamer import start_journal_stream
        journal_stream = start_journal_stream(
            stdout_file, instance_dir, project_name, run_num,
        )

    from app.cli_exec import acquire_provider_lock, popen_cli
    from app.config import (
        get_bash_foreground_timeout_ms,
        get_cli_provider_name,
        get_mission_timeout,
    )
    from app.utils import cleanup_mission_tmp_dir, create_mission_tmp_dir

    mission_timeout = get_mission_timeout()

    # Give the agent enough foreground headroom to BLOCK on a long-but-bounded
    # command rather than backgrounding it (backgrounded children are orphaned
    # when the one-shot session ends — see the cli-execution-model prompt).
    # BASH_DEFAULT_TIMEOUT_MS / BASH_MAX_TIMEOUT_MS are Claude-CLI-specific, so
    # only inject them for the Claude provider (inert but noise for others).
    mission_env = dict(os.environ)
    _provider_name = getattr(provider, "name", "") or get_cli_provider_name()
    _bash_ms = get_bash_foreground_timeout_ms()
    if _bash_ms > 0 and _provider_name == "claude":
        mission_env["BASH_DEFAULT_TIMEOUT_MS"] = str(_bash_ms)
        mission_env["BASH_MAX_TIMEOUT_MS"] = str(_bash_ms)

    # Per-mission TMPDIR: everything the agent creates via mktemp/$TMPDIR is
    # reaped in the outer finally below; crash leftovers are swept at startup
    # by reap_stale_mission_tmp_dirs(). Failure to create it is non-fatal:
    # the child then simply inherits the parent's TMPDIR (prior behavior).
    mission_tmp = None
    try:
        mission_tmp = create_mission_tmp_dir()
    except OSError as e:
        log("error", f"per-mission TMPDIR creation failed: {e}")

    exit_code = 1  # default if subprocess never completes
    # Read once up front so the outer finally can always clear the liveness
    # signal, even if an exception fires before the subprocess loop (#2086).
    koan_root_active = os.environ.get("KOAN_ROOT", "")
    # Bound before the try so the outer finally can always tear the mission's
    # containment down and cancel the watchdog, even when an exception fires
    # before the subprocess loop.
    scoped = None
    watchdog = None
    cleanup = None
    try:
        with open(stdout_file, "w") as out_f, open(stderr_file, "w") as err_f:
            # provider is the role-resolved instance (cli: section); popen_cli
            # uses it for stdin-rewrite + invocation lock so they match the
            # binary the command was built for. None → global provider.
            popen_kwargs = {"env": mission_env}
            if mission_tmp:
                # Route pytest's tmp factory into the reaped per-mission dir so
                # /tmp/pytest-of-* trees don't accumulate across missions (#2354).
                from app.utils import pytest_addopts_with_basetemp
                child_env = {**mission_env, "TMPDIR": mission_tmp}
                child_env["PYTEST_ADDOPTS"] = pytest_addopts_with_basetemp(
                    child_env.get("PYTEST_ADDOPTS", ""), mission_tmp
                )
                popen_kwargs["env"] = child_env
            # Take the provider invocation lock BEFORE arming the deferral: it
            # is a contended wait that a peer Kōan can hold for a whole mission,
            # and a forced restart during it must be honoured at once (no child
            # exists yet, so there is nothing to orphan). Ownership passes to
            # popen_cli's cleanup(); only a failure before that returns leaves
            # it to us.
            cli_lock = acquire_provider_lock(provider)
            lock_handed_over = False

            def _spawn_in_scope(argv, launcher, **kwargs):
                # popen_cli owns the provider's prompt-file stdin and the
                # invocation lock, so the scope launcher is handed to it and
                # prefixed there — after the prompt rewrite, never before.
                # The pre-acquired lock transfers on the FIRST attempt only:
                # launch_scoped retries unscoped after a spawn failure, and
                # popen_cli has already released it by then, so the retry must
                # take a fresh one instead of running unserialized.
                nonlocal cleanup, lock_handed_over
                handover = None if lock_handed_over else cli_lock
                lock_handed_over = True
                spawned, cleanup = popen_cli(
                    argv, provider=provider, launcher=launcher,
                    cli_lock=handover, **kwargs,
                )
                return spawned

            try:
                # Contain the mission in its own cgroup scope: the provider CLI
                # spawns build tools that outlive it (a Gradle daemon detaches to
                # PPID 1 in its own session), and only a cgroup catches those.
                # A forced restart is held until claude_proc is published, so
                # it can never re-exec past a just-spawned session.
                with _sigusr2_deferred():
                    scoped = mission_scope.launch_scoped(
                        cmd,
                        spawn=_spawn_in_scope,
                        koan_root=koan_root_active or None,
                        stdout=out_f,
                        stderr=err_f,
                        cwd=cwd,
                        **popen_kwargs,
                    )
                    proc = scoped.proc
                    _sig.claude_proc = proc
            except FileNotFoundError as e:
                # The provider binary vanished mid-session (the startup check +
                # planner gate handle the common case). Fail this mission
                # cleanly with an actionable message rather than a raw
                # FileNotFoundError: write it to stderr and return exit 127 so
                # the normal failure/fallback path handles it. A different
                # missing file is re-raised unchanged by missing_binary_message.
                from app.provider import missing_binary_message
                msg = missing_binary_message(e, cmd, _provider_name, "mission")
                log("error", msg)
                with contextlib.suppress(Exception):
                    err_f.write(msg + "\n")
                    err_f.flush()
                exit_code = 127
                return exit_code
            except BaseException:
                # popen_cli releases the lock on its own failures; this covers
                # the paths that never reach it (e.g. SystemExit raised by a
                # deferred forced restart replaying on block exit). release()
                # is idempotent.
                cli_lock.release()
                raise

            # Record the live provider PID so status consumers can report
            # observed runtime state instead of an inferred timestamp (#2086).
            if koan_root_active:
                from app.active_mission import write_active
                try:
                    write_active(
                        koan_root_active,
                        pid=proc.pid,
                        project=project_name,
                        run_num=run_num,
                        stdout_file=stdout_file,
                    )
                except OSError as e:
                    # A chronically failing write would make every status
                    # consumer report the running mission as idle/zombie —
                    # log it so the cause is diagnosable rather than invisible.
                    log("error", f"liveness signal write failed: {e}")

            from app.subprocess_runner import ProcessWatchdog

            if mission_timeout > 0:
                watchdog = ProcessWatchdog(
                    proc, mission_timeout,
                    on_timeout=lambda: log("error", f"Mission timed out ({mission_timeout}s) — killing process"),
                ).start()

            stagnation_monitor = _start_stagnation_monitor(
                stdout_file, proc, project_name,
            )

            try:
                # Wait for child, handling SIGINT interruptions gracefully.
                # Uses periodic timeout to detect watchdog kills — if
                # _kill_process_group fails silently, proc.wait() would
                # otherwise block forever.
                while True:
                    try:
                        proc.wait(timeout=MISSION_POLL_INTERVAL)
                        break
                    except subprocess.TimeoutExpired:
                        # Check for abort signal (user sent /abort)
                        koan_root_path = os.environ.get("KOAN_ROOT", "")
                        abort_path = Path(koan_root_path, ABORT_FILE) if koan_root_path else None
                        if abort_path and abort_path.exists():
                            log("koan", "Abort signal detected — aborting current mission")
                            abort_path.unlink(missing_ok=True)
                            _last_mission_aborted = True
                            _kill_process_group(proc)
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                log("error", f"Process {proc.pid} unkillable after abort — abandoning")
                            break
                        # Forced restart requested while the mission runs and
                        # SIGUSR2 never landed (stale PID file, signal lost).
                        if koan_root_path and is_force_restart(
                            koan_root_path, "run", since=_runner_start_time,
                        ):
                            _force_restart_now("Forced restart marker detected")
                        if watchdog and watchdog.fired:
                            # Watchdog already fired but process survived —
                            # make one last kill attempt from the main thread.
                            _kill_process_group(proc)
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                log("error", f"Process {proc.pid} unkillable — abandoning")
                            break
                    except (KeyboardInterrupt, InterruptedError):
                        # If task_running was cleared by on_sigint (double-tap),
                        # the child was terminated — wait for it to finish
                        if not _sig.task_running:
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                _kill_process_group(proc)
                            break
                        # Single CTRL-C — keep waiting
                        continue
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                if stagnation_monitor is not None:
                    stagnation_monitor.stop()
                    if stagnation_monitor.stagnated:
                        _last_mission_stagnated.set()
                        _stagnation_pattern_type = stagnation_monitor.pattern_type
                        _stagnation_pattern_excerpt = stagnation_monitor.pattern_excerpt
                if cleanup is not None:
                    cleanup()

        exit_code = proc.returncode
        if _last_mission_aborted:
            exit_code = 1
        elif watchdog and watchdog.fired:
            exit_code = 1
            _last_mission_timed_out = True
        elif _last_mission_stagnated.is_set():
            exit_code = 1
    finally:
        # Kill everything the mission left behind — the sweep that was missing
        # from the success path. Runs before the TMPDIR reap so nothing is still
        # writing into the scratch dir when it is removed. os.killpg alone is
        # not enough: a Gradle daemon re-parents to PID 1 with its own session
        # and has left the mission's process group by now, while a cgroup
        # catches every descendant however often it double-forks.
        if scoped is not None:
            # _last_mission_aborted covers both the /abort file branch and the
            # double-tap CTRL-C, which _on_sigint now records before it
            # SIGKILLs the group — otherwise a hand-aborted mission's -9 exit
            # is read as the memory cap firing and then refused a retry.
            _koan_killed = bool(
                _last_mission_aborted
                or (watchdog is not None and watchdog.fired)
                or _last_mission_stagnated.is_set()
            )
            try:
                scoped.teardown(koan_initiated_kill=_koan_killed)
            except Exception as e:
                log("error", f"mission scope teardown failed: {e}")
            if scoped.cap_exceeded:
                _last_mission_memory_cap = scoped.cap_message()
                log("error", f"Mission {_last_mission_memory_cap}")
        # Reap the mission's TMPDIR. In the rare "unkillable — abandoning"
        # path the child may still be alive; removing its scratch dir is safe
        # on POSIX (open fds survive) and the mission is already failed.
        if mission_tmp:
            cleanup_mission_tmp_dir(mission_tmp)
        # Safety net: sweep stray tmp trees test suites leave outside $TMPDIR
        # (pytest-of-*, test-koan*, jest_rs). Best-effort; never touches the
        # live scratch dir or other users' files, and age-gates removal so a
        # concurrent parallel session mid-`make test` isn't clobbered (#2354).
        try:
            from app.config import (
                get_cleanup_extra_tmp_globs,
                get_cleanup_min_tmp_age_seconds,
            )
            from app.utils import sweep_stray_tmp_dirs
            removed = sweep_stray_tmp_dirs(
                get_cleanup_extra_tmp_globs(),
                min_age_seconds=get_cleanup_min_tmp_age_seconds(),
            )
            if removed:
                total_mb = sum(b for _p, b in removed) / (1024 * 1024)
                log("health",
                    f"Swept {len(removed)} stray tmp tree(s), freed {total_mb:.0f} MB")
        except Exception as e:
            log("error", f"stray tmp sweep failed: {e}")
        # Universal page-cache reclaim (#2374): the CLI subprocess has exited,
        # so dropping clean pages here returns mission file I/O (git objects,
        # node_modules, venv, SQLite, logs) to the kernel and stops the billed
        # baseline ratcheting up. Best-effort — never masks the mission result.
        try:
            from app.page_cache import run_reclaim
            run_reclaim("post-mission")
        except Exception as e:
            log("error", f"post-mission page-cache reclaim failed: {e}")
        # Clear the liveness signal on every exit path — including exceptions
        # raised before the subprocess wait loop — so no stale .koan-active
        # survives to be misread as a live (then zombie) mission (#2086).
        if koan_root_active:
            from app.active_mission import clear_active
            with contextlib.suppress(OSError):
                clear_active(koan_root_active)
        # Always stop journal streaming, even on exception
        if journal_stream:
            from app.cli_journal_streamer import stop_journal_stream
            stop_journal_stream(
                journal_stream, exit_code, stderr_file,
                instance_dir, project_name, run_num,
            )
        # Reset signal state even on exception — otherwise _sig.task_running
        # stays True and CTRL-C requires a double-tap when no subprocess is running.
        _sig.claude_proc = None
        _sig.task_running = False
        _sig.first_ctrl_c = 0

    return exit_code


# ---------------------------------------------------------------------------
# Project configuration
# ---------------------------------------------------------------------------

def parse_projects() -> list:
    """Parse project configuration with validation.

    Delegates to get_known_projects() which checks:
    1. projects.yaml (if exists)
    2. KOAN_PROJECTS env var (fallback)

    Returns list of (name, path) tuples. Exits on error (only if no
    valid projects remain). Missing project directories are warned about
    and filtered out instead of crashing.
    """
    from app.utils import get_known_projects
    projects = get_known_projects()

    if not projects:
        log("error", "No projects configured. Create projects.yaml or set KOAN_PROJECTS env var.")
        sys.exit(1)

    if len(projects) > 50:
        log("error", f"Max 50 projects allowed. You have {len(projects)}.")
        sys.exit(1)

    valid = []
    for name, path in projects:
        if not Path(path).is_dir():
            log("warn", f"Project '{name}' path does not exist: {path} — skipping. "
                f"Remove it from projects.yaml to silence this warning.")
        else:
            valid.append((name, path))

    if not valid:
        log("error", "No valid project directories found. Check your projects.yaml paths.")
        sys.exit(1)

    return valid


# ---------------------------------------------------------------------------
# Startup sequence (delegated to startup_manager.py)
# ---------------------------------------------------------------------------

def run_startup(koan_root: str, instance: str, projects: list):
    """Run all startup tasks (crash recovery, health, sync, etc.).

    Delegates to app.startup_manager which decomposes the startup
    into independently testable steps.
    """
    from app.startup_manager import run_startup as _run_startup
    return _run_startup(koan_root, instance, projects)


# ---------------------------------------------------------------------------
# Notify helper
# ---------------------------------------------------------------------------

def _notify(instance: str, message: str):
    """Send a formatted notification to Telegram."""
    try:
        from app.notify import format_and_send
        format_and_send(message, instance_dir=instance)
    except Exception as e:
        log("error", f"Notification failed: {e}")


def _notify_raw(instance: str, message: str, dedup_window: float = 0.0):
    """Send a notification straight to Telegram, skipping the Claude-CLI
    personality reformatter (notify.format_and_send → format_outbox.
    format_message). Use this for terse status updates (startup progress,
    auto-update restarts) where the verbatim text and emoji matter and the
    extra Claude CLI call would defeat the point. send_telegram still
    handles priority filtering, flood protection, and retries.

    Pass dedup_window > 0 for idempotent lifecycle notices (#2426) so a restart
    loop doesn't re-announce them on every incarnation.
    """
    try:
        from app.notify import send_telegram
        send_telegram(message, dedup_window=dedup_window)
    except Exception as e:
        log("error", f"Raw notification failed: {e}")


def _quota_raw_snippet(stdout_text: str = "", stderr_text: str = "",
                       stdout_file: str = "", stderr_file: str = "") -> str:
    """Build a bounded debug snippet from quota-handler kwargs or output files.

    Reads the file form of ``handle_quota_exhaustion`` kwargs when only paths
    are available (the regular mission path); uses the pre-read text form when
    the caller already has it in memory (the skill path). Delegates the actual
    windowing to :func:`quota_handler.quota_debug_snippet`.
    """
    from app.quota_handler import quota_debug_snippet
    if stdout_file and not stdout_text:
        with contextlib.suppress(OSError):
            stdout_text = Path(stdout_file).read_text()
    if stderr_file and not stderr_text:
        with contextlib.suppress(OSError):
            stderr_text = Path(stderr_file).read_text()
    return quota_debug_snippet(stdout_text, stderr_text)


def _notify_quota_warning(instance: str, body: str, raw_output: str = "") -> None:
    """Send a quota-exhaustion warning as verbatim text (code block preserved).

    Quota warnings include the raw CLI output (reset time, stop reason, cost,
    usage) in a fenced code block for debugging. ``_notify`` runs the Claude-CLI
    personality reformatter, whose contract strips markdown fences — so the
    block would never render. Route through ``_notify_raw`` instead:
    ``send_telegram`` converts ``\\`\\`\\``` fences to ``<pre>`` for Telegram's
    HTML parse mode. ``body`` is already-final prose; a non-empty ``raw_output``
    is appended as a trailing fenced block.
    """
    snippet = (raw_output or "").strip()
    message = f"{body}\n\n```\n{snippet}\n```" if snippet else body
    _notify_raw(instance, message)


def _is_ci_check_mission(mission_title: str) -> bool:
    """Return True if *mission_title* is a CI-related mission.

    Matches both /ci_check skill missions and ci_dispatch-generated
    "Fix CI failure:" missions so all CI failure notifications use
    🚦 instead of the alarming ❌.
    """
    from app.skill_dispatch import parse_skill_mission
    _, cmd, args = parse_skill_mission(mission_title)
    if cmd == "ci_check":
        return True
    if not cmd and args.startswith("Fix CI failure:"):
        return True
    return False


def _mission_fail_icon(mission_title: str) -> str:
    """Emoji prefix for a mission-failure notification.

    CI-related missions use 🚦 (a status signal, not an alarm); everything
    else uses ❌. Centralised so every failure-notification path stays
    consistent — this was scattered across call sites and kept regressing
    (a /ci_check mission whose start transition or devcontainer setup
    failed still surfaced ❌). See issues #1883, #2027.
    """
    return "🚦" if _is_ci_check_mission(mission_title) else "❌"


_SKILL_COMPLETION = {
    "review": ("🔍", "Reviewed"),
    "fix": ("🐞", "Fixed"),
    "rebase": ("🔄", "Rebased"),
    "plan": ("🧠", "Planned"),
    "implement": ("🔨", "Implemented"),
}

# Tracked skills whose canonical completion line carries the PR URL that the
# runner's own outcome line would otherwise restate. For these the agent loop is
# the sole reporter (the runner's duplicate line is suppressed). /plan is tracked
# but deliberately excluded: it never opens a PR — it emits an *issue*/Jira URL or
# an inline plan body that the PR-only canonical extraction cannot recover — so
# its runner outcome line is the only place that content reaches the user and must
# never be suppressed (#2153 follow-up).
_PR_URL_SKILLS = {"review", "fix", "rebase", "implement"}


def _skill_command_name(mission_title: str) -> str:
    """Return the lowercased slash-command name of a mission, or "" if none."""
    t = (mission_title or "").strip()
    if not t.startswith("/"):
        return ""
    return t[1:].split(None, 1)[0].lower()


def _tracked_skill(mission_title: str):
    """Return (emoji, past_tense) if the mission is a tracked skill, else None."""
    cmd = _skill_command_name(mission_title)
    return _SKILL_COMPLETION.get(cmd) if cmd else None


def _canonical_line_carries_url(mission_title: str) -> bool:
    """True when the agent loop's canonical completion line carries the same URL
    the runner's outcome line would, so the runner line can be safely suppressed.

    Only the PR-producing tracked skills qualify. /plan is tracked but its URL
    (issue/Jira) or inline body cannot be recovered by the PR-only canonical
    extraction, so it is excluded — its runner line is the sole reporter (#2153)."""
    return _skill_command_name(mission_title) in _PR_URL_SKILLS


def _completion_pr_url(instance, project_name):
    try:
        from app.mission_runner import get_last_pr_url
        return get_last_pr_url(instance, project_name)
    except (ImportError, OSError, ValueError):
        return ""


def _notify_mission_normal(
    instance: str,
    project_name: str,
    run_num: int,
    max_runs: int,
    exit_code: int,
    mission_title: str,
    pr_url: str,
):
    """Normal-mode (quiet) end-of-mission emit. See _notify_mission_end."""
    skill = _tracked_skill(mission_title)

    # Failures always surface (short form).
    if exit_code != 0:
        emoji = (skill[0] + " ") if skill else ""
        prefix = _mission_fail_icon(mission_title)
        label = mission_title or "Run"
        _notify(instance, f"{prefix} [{project_name}] {emoji}Failed: {label}")
        return

    # Tracked skill success → concise "✅ [project] 🔍 Reviewed <pr-url>". Prefer
    # the URL captured during post-mission processing (before pending.md was
    # deleted); fall back to a best-effort re-read.
    if skill is not None:
        emoji, verb = skill
        # /plan is tracked but the canonical line cannot carry its issue/Jira URL
        # or inline body (PR-only extraction). The runner already emitted that
        # content via notify_outcome (not suppressed), so emitting a bare
        # "✅ [project] 🧠 Planned" here would just duplicate the row with less
        # information. Log only and let the runner's line stand (#2153).
        if not _canonical_line_carries_url(mission_title):
            from app.run_log import log_safe
            log_safe(
                "mission",
                f"[{project_name}] {verb} (runner emitted outcome line)",
            )
            return
        url = pr_url or _completion_pr_url(instance, project_name)
        _notify(instance, f"✅ [{project_name}] {emoji} {verb} {url}".rstrip())
        return

    # Genuinely autonomous background runs carry no mission title — suppress
    # those (log only). An operator-initiated mission (a user/Telegram-queued
    # task with a real title) still gets a minimal completion signal so
    # explicitly-requested work isn't silently dropped.
    title = (mission_title or "").strip()
    if not title:
        from app.run_log import log_safe
        log_safe(
            "mission",
            f"[{project_name}] Run {run_num}/{max_runs} done "
            "(normal mode, autonomous run suppressed)",
        )
        return
    _notify(instance, f"✅ [{project_name}] Done: {title}")


def _notify_mission_end(
    instance: str,
    project_name: str,
    run_num: int,
    max_runs: int,
    exit_code: int,
    mission_title: str = "",
    pr_url: str = "",
):
    """Send a notification when a mission or autonomous run completes.

    Honors messaging.level:
    - normal (default): one short line for tracked skill missions
      (✅ [project] 🔍 Reviewed <pr-url>); operator-initiated missions get a
      minimal one-line success (✅ [project] Done: <title>); genuinely
      autonomous background runs (no mission title) are logged only (no bridge
      push); failures always surface (short form).
    - debug: full lifecycle line + journal summary (legacy behavior).
    """
    if not is_debug():
        _notify_mission_normal(
            instance, project_name, run_num, max_runs,
            exit_code, mission_title, pr_url,
        )
        return

    # ---- debug mode: legacy verbose behavior ----
    if exit_code == 0:
        prefix = "✅"
        label = mission_title if mission_title else "Autonomous run"
        msg = f"{prefix} [{project_name}] Run {run_num}/{max_runs} — {label}"
        # Try to attach a brief summary from the journal
        try:
            from app.mission_summary import get_mission_summary
            summary = get_mission_summary(instance, project_name, max_chars=300)
            if summary:
                msg += f"\n\n{summary}"
        except Exception as e:
            log("error", f"Mission summary extraction failed: {e}")
    else:
        prefix = _mission_fail_icon(mission_title)
        label = mission_title if mission_title else "Run"
        msg = f"{prefix} [{project_name}] Run {run_num}/{max_runs} — Failed: {label}"
        # Try to attach error context from the journal
        try:
            from app.mission_summary import get_failure_context
            context = get_failure_context(instance, project_name, max_chars=300)
            if context:
                msg += f"\n\n{context}"
        except Exception as e:
            log("error", f"Failure context extraction failed: {e}")

    _notify(instance, msg)


# ---------------------------------------------------------------------------
# Startup delay (#1039)
# ---------------------------------------------------------------------------

DEFAULT_STARTUP_DELAY = 30  # seconds


def _startup_delay(koan_root: str) -> None:
    """Wait before the first iteration so /pause can be processed.

    When ``make start`` launches koan, the first mission can be picked up
    before the Telegram bridge has time to process a /pause command.  This
    interruptible delay (default 30 s, configurable via ``startup_delay``
    in config.yaml) closes the race window.

    The delay is skipped when:
    - The agent is already paused (.koan-pause exists).
    - ``startup_delay`` is set to ``0``.

    The delay is interrupted early if any lifecycle signal appears
    (.koan-pause, .koan-stop, .koan-shutdown, .koan-restart-run).
    """
    from app.utils import load_config

    delay = load_config().get("startup_delay", DEFAULT_STARTUP_DELAY)
    if delay <= 0:
        return

    # Already paused — skip directly into the main loop's pause handler
    if Path(koan_root, PAUSE_FILE).exists():
        log("koan", "Already paused at startup — skipping startup delay.")
        return

    log(
        "koan",
        f"Startup delay: waiting {delay}s before first mission "
        f"(send /pause now if needed).",
    )

    tick = 2  # check signals every 2 s
    elapsed = 0
    while elapsed < delay:
        time.sleep(min(tick, delay - elapsed))
        elapsed += tick

        # Any lifecycle signal → break out. Use the runner's own restart marker
        # (RESTART_RUN_FILE) — the legacy combined .koan-restart is deprecated
        # and no longer written.
        for sig in (PAUSE_FILE, STOP_FILE, SHUTDOWN_FILE, RESTART_RUN_FILE):
            if Path(koan_root, sig).exists():
                log("koan", f"Signal detected during startup delay ({sig}), proceeding.")
                return

    log("koan", "Startup delay complete — entering main loop.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_current_project(koan_root: str) -> str:
    """Read the current project name from .koan-project, safely.

    Returns the project name or "unknown" if the file cannot be read
    (missing, locked, or corrupt).
    """
    try:
        return Path(koan_root, PROJECT_FILE).read_text().strip() or "unknown"
    except (OSError, ValueError):
        return "unknown"


# ---------------------------------------------------------------------------
# Instance commit helper
# ---------------------------------------------------------------------------

def _commit_instance(instance: str, message: str = ""):
    """Commit instance changes and push.

    Delegates to :func:`app.mission_runner.commit_instance` which is the
    single implementation of the git add / commit / push sequence.
    """
    from app.mission_runner import commit_instance
    commit_instance(instance, message)


# ---------------------------------------------------------------------------
# Update handler (graceful update + restart)
# ---------------------------------------------------------------------------

def _build_memory_monitor(koan_root=None):
    """Construct a MemoryMonitor from config, or None when disabled (#2232).

    Persists the runtime enabled/disabled decision (and reason) via
    write_watchdog_status when koan_root is given, so observability reports what
    the loop is actually doing rather than raw config.
    """
    from app.config import get_memory_monitor_config
    from app.memory_monitor import write_watchdog_status
    conf = get_memory_monitor_config()

    def _record(enabled, threshold_mb, reason):
        if koan_root is not None:
            write_watchdog_status(
                koan_root, enabled=enabled, threshold_mb=threshold_mb, reason=reason,
            )

    if not conf.get("enabled"):
        _record(False, conf.get("threshold_mb"), "disabled in config")
        return None
    from app.memory_monitor import MemoryMonitor, read_rss_mb
    threshold_mb = conf["threshold_mb"]
    # read_rss_mb() returns 0.0 on read failure (a real RSS is always positive).
    # Without a readable baseline we can't tell a safe threshold from a
    # restart-looping one, so disable rather than create an unverifiable monitor.
    baseline = read_rss_mb()
    if baseline <= 0:
        log("koan",
            "Memory watchdog disabled: baseline RSS unreadable, cannot verify "
            "threshold is above baseline. Watchdog stays off this session.")
        _record(False, threshold_mb, "baseline RSS unreadable")
        return None
    # Misconfiguration guard: a threshold at or below the current baseline RSS
    # would trip every session and restart-loop forever. Disable instead.
    if threshold_mb <= 0 or threshold_mb <= baseline:
        log("koan",
            f"Memory watchdog disabled: threshold {threshold_mb} MB ≤ baseline "
            f"RSS {baseline:.0f} MB would restart-loop. Raise "
            "memory_monitor.threshold_mb above baseline.")
        _record(False, threshold_mb, "threshold <= baseline RSS")
        return None
    _record(True, threshold_mb, "")
    return MemoryMonitor(
        threshold_mb=threshold_mb,
        sustained_samples=conf["sustained_samples"],
        tracemalloc_enabled=conf.get("tracemalloc", False),
        min_runs_before_restart=conf.get("min_runs_before_restart", 1),
    )


def _write_memory_restart_journal(koan_root, rss_mb, threshold_mb, count, top_allocations):
    """Append a memory-restart record for post-hoc leak analysis."""
    from datetime import datetime, timezone
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "memory_restart",
        "rss_mb": round(rss_mb, 1),
        "threshold_mb": threshold_mb,
        "runs": count,
        "top_allocations": top_allocations,
    }
    path = Path(koan_root, "instance", ".memory-restarts.jsonl")
    with suppress_logged(log, "warning", "Memory-restart journal write failed", OSError):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def _handle_memory_restart(koan_root, instance, monitor, count):
    """Log, journal, notify, and exit with RESTART_EXIT_CODE to reclaim RSS."""
    rss = monitor.last_rss_mb
    log("koan",
        f"Memory threshold exceeded: {rss:.0f} MB ≥ {monitor.threshold_mb} MB "
        f"after {count} runs — restarting to reclaim.")
    top = monitor.top_allocations(limit=10) if monitor.tracemalloc_enabled else []
    _write_memory_restart_journal(koan_root, rss, monitor.threshold_mb, count, top)
    with suppress_logged(log, "warning", "Memory-restart notify failed", Exception):
        if top:
            detail = "\n".join(top[:5])
        elif monitor.tracemalloc_error:
            detail = f"(tracemalloc failed to start: {monitor.tracemalloc_error})"
        else:
            detail = "(tracemalloc disabled)"
        _notify(
            instance,
            f"♻️ Restarting to reclaim memory: RSS {rss:.0f} MB ≥ "
            f"{monitor.threshold_mb} MB after {count} runs.\n"
            f"Top allocations:\n{detail}",
        )
    sys.exit(RESTART_EXIT_CODE)


def _handle_update(koan_root: str, instance: str, count: int) -> bool:
    """Handle /update: pull upstream updates, then trigger restart.

    Called after the current mission completes. Pulls the latest code
    and requests a restart. If the pull fails, notifies and still restarts
    (the user explicitly asked for an update).

    Returns True if the update was performed (caller should restart),
    False if the update was refused due to safety checks.
    """
    from app.update_manager import check_update_safety, pull_upstream
    from app.restart_manager import request_restart
    from app.pause_manager import remove_pause

    safety_msg = check_update_safety(Path(koan_root))
    if safety_msg:
        log("koan", "Update refused: diverged from upstream")
        _notify(instance, safety_msg)
        return False

    result = pull_upstream(Path(koan_root))
    if not result.success:
        log("koan", f"Update failed: {result.error}")
        _notify(instance, f"🔄 Update failed ({result.error}), restarting anyway.")
    elif result.changed:
        log("koan", f"Update: {result.summary()}")
        _notify(instance, f"🔄 Update complete after {count} runs. {result.summary()} Restarting...")
    else:
        log("koan", "Update: already up to date, restarting.")
        _notify(instance, f"🔄 Update complete after {count} runs. Already up to date. Restarting...")

    remove_pause(koan_root)
    request_restart(koan_root)
    return True


def _handle_update_release(koan_root: str, instance: str, count: int) -> bool:
    """Handle /update_last_release: checkout latest release tag, then restart.

    Returns True if the update was performed (caller should restart),
    False if no tags found or safety check failed.
    """
    from app.update_manager import checkout_latest_tag
    from app.restart_manager import request_restart
    from app.pause_manager import remove_pause

    result = checkout_latest_tag(Path(koan_root))
    if not result.success:
        log("koan", f"Release update failed: {result.error}")
        _notify(instance, f"🔄 Release update failed ({result.error}), restarting anyway.")
    elif result.changed:
        log("koan", f"Release update: {result.summary()}")
        _notify(instance, f"🔄 Release update complete after {count} runs. {result.summary()} Restarting...")
    else:
        log("koan", "Release update: already on latest release tag.")
        _notify(instance, "🔄 Already on latest release tag. Restarting...")

    remove_pause(koan_root)
    request_restart(koan_root)
    return True


# ---------------------------------------------------------------------------
# Pause mode handler
# ---------------------------------------------------------------------------

_last_inbox_check: float = float("-inf")


def _check_inbox_during_pause(koan_root: str, instance: str) -> None:
    """Process /inbox signal while paused (throttled to once per hour).

    Checks each provider independently so one failure doesn't block the other.
    Signal is consumed only after fetching completes (success or per-provider error).
    """
    global _last_inbox_check

    from app.constants import PAUSE_INBOX_CHECK_INTERVAL
    from app.loop_manager import (
        _consume_check_notifications_signal,
        process_github_notifications,
        process_jira_notifications,
    )

    signal_path = Path(koan_root, ".koan-check-notifications")
    if not signal_path.exists():
        return

    now = time.monotonic()
    if now - _last_inbox_check < PAUSE_INBOX_CHECK_INTERVAL:
        return

    log("pause", "Inbox check requested — fetching notifications while paused")
    total = 0
    try:
        total += process_github_notifications(koan_root, instance, force=True)
    except Exception as e:
        log("error", f"GitHub inbox check during pause failed: {e}")
    try:
        total += process_jira_notifications(koan_root, instance, force=True)
    except Exception as e:
        log("error", f"Jira inbox check during pause failed: {e}")

    _last_inbox_check = now
    _consume_check_notifications_signal(koan_root)
    if total > 0:
        log("pause", f"Inbox: {total} mission(s) queued (will run after resume)")


def handle_pause(
    koan_root: str, instance: str, max_runs: int,
) -> Optional[str]:
    """Handle pause mode. Returns "resume" if resumed, None to stay paused.

    When paused, NO autonomous or contemplative work is performed.
    The agent only checks for resume conditions and sleeps.
    """
    timestamp = time.strftime('%H:%M')
    set_status(koan_root, f"Paused ({timestamp})")
    log("pause", f"Paused. Waiting for resume. ({timestamp})")

    # Check auto-resume
    try:
        from app.pause_manager import check_and_resume
        resume_msg = check_and_resume(koan_root)
        if resume_msg:
            log("pause", f"Auto-resume: {resume_msg}")
            _reset_usage_session(instance)
            _notify(instance, f"🔄 Kōan auto-resumed: {resume_msg}. Starting fresh (0/{max_runs} runs).")
            return "resume"
    except Exception as e:
        log("error", f"Auto-resume check failed: {e}")

    # Manual resume (pause file already removed — /resume handler already
    # resets session counters for quota pauses, but we reset here too as
    # a safety net for any resume path)
    if not Path(koan_root, PAUSE_FILE).exists():
        log("pause", "Manual resume detected")
        _reset_usage_session(instance)
        return "resume"

    # Sleep 5 min in 5s increments — check for resume/stop/restart/shutdown/update
    with protected_phase("Paused — waiting for resume"):
        for _ in range(60):
            if not Path(koan_root, PAUSE_FILE).exists():
                return "resume"
            if Path(koan_root, STOP_FILE).exists():
                log("pause", "Stop signal detected while paused")
                break
            if Path(koan_root, SHUTDOWN_FILE).exists():
                log("pause", "Shutdown signal detected while paused")
                break
            if Path(koan_root, CYCLE_FILE).exists():
                log("pause", "Update signal detected while paused")
                break
            if Path(koan_root, CYCLE_RELEASE_FILE).exists():
                log("pause", "Release update signal detected while paused")
                break
            if check_restart(koan_root, target="run"):
                break
            _check_inbox_during_pause(koan_root, instance)
            time.sleep(5)

    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop():
    """The Kōan main loop."""
    _init_colors()

    # Validate environment
    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        log("error", "KOAN_ROOT environment variable not set.")
        sys.exit(1)

    instance = os.path.join(koan_root, "instance")
    if not Path(instance).is_dir():
        log("error", "No instance/ directory found. Run: cp -r instance.example instance")
        sys.exit(1)

    # Run pending data migrations (e.g. French→English header conversion)
    from app.migration_runner import run_pending_migrations
    applied = run_pending_migrations()
    if applied:
        log("init", f"Applied {len(applied)} migration(s)")

    # Set PYTHONPATH
    os.environ["PYTHONPATH"] = os.path.join(koan_root, "koan")

    # Parse projects (projects.yaml > KOAN_PROJECTS)
    projects = parse_projects()

    # Record startup time — also published module-wide so the mission wait
    # loop can ignore restart markers from a previous incarnation.
    global _runner_start_time
    start_time = _runner_start_time = time.time()

    # Acquire PID (flock-based exclusive lock)
    pidfile_lock = acquire_pidfile(Path(koan_root), "run")

    # Clear stale signal files from a previous session.
    # If `make stop` or `/stop` ran while run.py was NOT running, the signal
    # file persists and would cause an immediate exit on next startup.
    Path(koan_root, STOP_FILE).unlink(missing_ok=True)
    Path(koan_root, SHUTDOWN_FILE).unlink(missing_ok=True)
    Path(koan_root, CYCLE_FILE).unlink(missing_ok=True)
    Path(koan_root, CYCLE_RELEASE_FILE).unlink(missing_ok=True)
    Path(koan_root, ABORT_FILE).unlink(missing_ok=True)
    Path(koan_root, RESET_COUNTER_FILE).unlink(missing_ok=True)
    clear_restart(koan_root, target="run")

    # Install SIGINT handler
    signal.signal(signal.SIGINT, _on_sigint)

    # Install SIGUSR1 handler — instant /abort from the bridge.
    # Avoids the up-to-30s wait for the ABORT_FILE poll cycle inside
    # run_claude_task(). The file is still written for durability so a
    # missed signal (runner restarting, etc.) is recovered on next poll.
    signal.signal(signal.SIGUSR1, _on_sigusr1)

    # Install SIGUSR2 handler — /restart --force. Kills the in-flight mission
    # and exits with RESTART_EXIT_CODE instead of waiting for it to finish.
    # The forced marker on disk is polled as a fallback if the signal is lost.
    # Only once the handler exists do we advertise the capability: SIGUSR2's
    # default disposition is terminate, so /restart --force must never signal a
    # runner that predates this handler (it would orphan the provider session).
    signal.signal(signal.SIGUSR2, _on_sigusr2)
    declare_runner_caps(koan_root, os.getpid())

    # Initialize project state
    if projects:
        atomic_write(Path(koan_root, PROJECT_FILE), projects[0][0])
        os.environ["KOAN_CURRENT_PROJECT"] = projects[0][0]
        os.environ["KOAN_CURRENT_PROJECT_PATH"] = projects[0][1]

    count = 0
    consecutive_errors = 0
    consecutive_idle = 0
    consecutive_nonproductive = 0
    idle_notified = False
    MAX_CONSECUTIVE_IDLE = 30  # ~30 min at 60s interval → auto-pause
    # Throttle kicks in only after several back-to-back non-productive
    # iterations so that one-off dedup skips / transient errors don't eat
    # an extra second each.
    NONPRODUCTIVE_THROTTLE_THRESHOLD = 3
    try:
        # Startup sequence
        max_runs, interval, branch_prefix = run_startup(koan_root, instance, projects)

        # Probe for optional rtk binary (https://github.com/rtk-ai/rtk).
        # When present, the prompt builder injects an awareness section so
        # Claude prefers ``rtk <cmd>`` over the raw command for 60-90 % less
        # tool output.  Detection is cheap, cached, and never mutates state.
        try:
            from app.rtk_detector import detect_rtk
            log("init", detect_rtk().summary_line())
        except Exception as e:
            log("error", f"rtk detection failed: {e}")

        git_sync_interval = int(os.environ.get("KOAN_GIT_SYNC_INTERVAL", "5"))

        # --- Startup delay (#1039) ---
        # Give the user a window to send /pause before the first mission runs.
        # Without this, a mission can be picked up immediately after startup,
        # racing with the Telegram bridge processing of /pause.
        _startup_delay(koan_root)

        memory_monitor = _build_memory_monitor(koan_root)

        while True:
            # --- Stop check ---
            stop_file = Path(koan_root, STOP_FILE)
            if stop_file.exists():
                log("koan", "Stop requested.")
                stop_file.unlink(missing_ok=True)
                current = _read_current_project(koan_root)
                _notify(instance, f"Kōan stopped on request after {count} runs. Last project: {current}.")
                break

            # --- Update check (finish mission → update → restart) ---
            cycle_file = Path(koan_root, CYCLE_FILE)
            if cycle_file.exists():
                log("koan", "Update requested. Updating and restarting...")
                cycle_file.unlink(missing_ok=True)
                if _handle_update(koan_root, instance, count):
                    sys.exit(RESTART_EXIT_CODE)

            # --- Release update check (checkout latest tag → restart) ---
            cycle_release_file = Path(koan_root, CYCLE_RELEASE_FILE)
            if cycle_release_file.exists():
                log("koan", "Release update requested. Checking out latest tag...")
                cycle_release_file.unlink(missing_ok=True)
                if _handle_update_release(koan_root, instance, count):
                    sys.exit(RESTART_EXIT_CODE)

            # --- Shutdown check (stops both agent loop and bridge) ---
            if is_shutdown_requested(koan_root, start_time):
                log("koan", "Shutdown requested. Exiting.")
                clear_shutdown(koan_root)
                current = _read_current_project(koan_root)
                _notify(instance, f"Kōan shutdown after {count} runs. Last project: {current}.")
                break

            # --- Restart check ---
            if check_restart(koan_root, since=start_time, target="run"):
                log("koan", "Restart requested. Exiting for re-launch...")
                clear_restart(koan_root, target="run")
                sys.exit(RESTART_EXIT_CODE)

            # --- Memory watchdog (#2232) ---
            # All knobs are frozen at startup in _build_memory_monitor() — a
            # live config edit takes effect on the next restart, consistently
            # for every knob (no per-iteration YAML re-read).
            if memory_monitor is not None:
                if (
                    memory_monitor.sample()
                    and count >= memory_monitor.min_runs_before_restart
                ):
                    _handle_memory_restart(koan_root, instance, memory_monitor, count)

            # --- Pause mode ---
            if Path(koan_root, PAUSE_FILE).exists():
                result = handle_pause(koan_root, instance, max_runs)
                if result == "resume":
                    count = 0
                    consecutive_errors = 0
                    consecutive_idle = 0
                    consecutive_nonproductive = 0
                    idle_notified = False
                    from app.feature_tips import mark_active
                    mark_active()
                    _mark_startup_resume()
                continue

            # --- Reset counter check ---
            reset_file = Path(koan_root, RESET_COUNTER_FILE)
            if reset_file.exists():
                reset_file.unlink(missing_ok=True)
                old_count = count
                count = 0
                consecutive_errors = 0
                consecutive_idle = 0
                consecutive_nonproductive = 0
                idle_notified = False
                from app.feature_tips import mark_active
                mark_active()
                _mark_startup_resume()
                log("koan", f"Run counter reset (was {old_count}/{max_runs}, now 0/{max_runs}).")
                _notify(instance, f"🔄 Run counter reset: {old_count} → 0 (max {max_runs}).")

            # --- Iteration body (exception-protected) ---
            try:
                productive = _run_iteration(
                    koan_root=koan_root,
                    instance=instance,
                    projects=projects,
                    count=count,
                    max_runs=max_runs,
                    interval=interval,
                    git_sync_interval=git_sync_interval,
                )
                consecutive_errors = 0
                if productive is True:
                    count += 1
                    consecutive_idle = 0
                    consecutive_nonproductive = 0
                    idle_notified = False
                    from app.feature_tips import mark_active
                    mark_active()
                elif productive == "idle":
                    consecutive_idle += 1
                    consecutive_nonproductive = 0
                    if not idle_notified:
                        idle_notified = True
                        try:
                            from app.schedule_manager import is_scheduled_active
                            schedule_active = is_scheduled_active()
                        except (ImportError, Exception):
                            schedule_active = False
                        if schedule_active:
                            _notify(
                                instance,
                                "💤 No work available — but schedule is active, "
                                "staying awake for missions.",
                            )
                        else:
                            _notify(
                                instance,
                                "💤 No work available — waiting for pending reviews "
                                "or new missions. Auto-pause in ~30 min.",
                            )
                    if consecutive_idle >= MAX_CONSECUTIVE_IDLE:
                        # Check if a schedule window is active — if so, the
                        # human configured deep_hours or work_hours and the
                        # agent should stay active, not auto-pause.
                        with suppress_logged(log, "warning", "Schedule active check failed", Exception):
                            from app.schedule_manager import is_scheduled_active
                            if is_scheduled_active():
                                if consecutive_idle == MAX_CONSECUTIVE_IDLE:
                                    log("koan", "Idle timeout reached but schedule is active — staying awake")
                                continue

                        from app.config import get_auto_pause
                        if get_auto_pause():
                            idle_min = consecutive_idle * interval // 60
                            log("koan", f"Idle for {idle_min} min — auto-pausing.")
                            from app.pause_manager import create_pause
                            create_pause(koan_root, "idle_timeout")
                            _notify(
                                instance,
                                f"⏸️ Auto-paused after {idle_min} min idle. "
                                "Use /resume when ready.",
                            )
                        else:
                            consecutive_idle = 0  # Reset so we don't log every iteration
                else:
                    # Non-productive but not idle (error recovery, dedup, etc.)
                    # Don't count toward idle timeout. Throttle only after
                    # several back-to-back occurrences so one-off skips aren't
                    # penalized, but a persistent failure (e.g. dedup skipping
                    # a stuck mission) can't tight-loop and flood Telegram.
                    consecutive_nonproductive += 1
                    if consecutive_nonproductive >= NONPRODUCTIVE_THROTTLE_THRESHOLD:
                        time.sleep(1)
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except Exception as e:
                consecutive_errors += 1
                _handle_iteration_error(
                    e, consecutive_errors, koan_root, instance,
                )

    except KeyboardInterrupt:
        current = _read_current_project(koan_root)
        _notify(instance, f"Kōan interrupted after {count} runs. Last project: {current}.")
    finally:
        # Fire session_end hook (fire-and-forget, exception-safe)
        try:
            from app.hooks import fire_hook
            fire_hook("session_end", instance_dir=instance, total_runs=count)
        except Exception as e:
            print(f"[hooks] session_end hook error: {e}", file=sys.stderr)
        # Kill any active parallel sessions before exiting
        if _live_sessions:
            log("koan", f"Killing {len(_live_sessions)} active parallel session(s) on shutdown")
            try:
                from app.session_manager import kill_session
                registry = _get_session_registry(instance)
                for _s in list(_live_sessions.values()):
                    try:
                        kill_session(_s, registry)
                        registry.remove(_s.id)
                    except Exception as _ke:
                        log("error", f"kill_session error: {_ke}")
                _live_sessions.clear()
            except Exception as e:
                log("error", f"parallel session cleanup error: {e}")
        global _session_registry
        _session_registry = None
        # Cleanup
        Path(koan_root, STATUS_FILE).unlink(missing_ok=True)
        # Withdraw the SIGUSR2 capability before the PID can be recycled.
        clear_runner_caps(koan_root)
        release_pidfile(pidfile_lock, Path(koan_root), "run")
        log("koan", f"Shutdown. {count} runs executed.")
        _reset_terminal()


# ---------------------------------------------------------------------------
# Iteration helpers (extracted from _run_iteration for readability)
# ---------------------------------------------------------------------------


def _sleep_between_runs(
    koan_root: str,
    instance: str,
    interval: int,
    run_num: int = 0,
    max_runs: int = 0,
    context: str = "",
):
    """Sleep between runs, waking early if new missions arrive.

    Checks for pending missions first — skips sleep entirely if found.
    """
    if check_pending_missions(instance):
        log("koan", "Pending missions found — skipping sleep")
        if run_num:
            set_status(koan_root, f"Run {run_num}/{max_runs} — done, next run starting")
        return

    status_suffix = f" ({time.strftime('%H:%M')})"
    if context:
        set_status(koan_root, f"{context}{status_suffix}")
    else:
        set_status(koan_root, f"Idle — sleeping {interval}s{status_suffix}")
    log("koan", f"Sleeping {interval}s (checking for new missions every 10s)...")
    # Idle page-cache reclaim (#2374) fires inside interruptible_sleep itself.
    with protected_phase("Sleeping between runs"):
        wake = interruptible_sleep(interval, koan_root, instance)
    if wake == "mission":
        log("koan", "New mission detected during sleep — waking up early")
        if run_num:
            set_status(koan_root, f"Run {run_num}/{max_runs} — done, new mission detected")


def _next_notification_due_in(
    github_enabled: bool,
    jira_enabled: bool,
) -> int:
    """Return the earliest known notification poll due time."""
    due_times = []
    if github_enabled:
        try:
            from app.loop_manager import get_github_notification_check_due_in
            due = get_github_notification_check_due_in()
            if due > 0:
                due_times.append(due)
        except Exception as e:
            log("warning", f"GitHub notification due-time check failed: {e}")
    if jira_enabled:
        try:
            from app.loop_manager import get_jira_notification_check_due_in
            due = get_jira_notification_check_due_in()
            if due > 0:
                due_times.append(due)
        except Exception as e:
            log("warning", f"Jira notification due-time check failed: {e}")
    return min(due_times) if due_times else 0


def _resolve_idle_wait_interval(
    configured_interval: int,
    github_enabled: bool,
    jira_enabled: bool,
) -> int:
    """Pick a nonzero idle wait without changing normal configured sleeps."""
    try:
        interval = max(0, int(configured_interval))
    except (TypeError, ValueError):
        interval = 0
    if interval > 0:
        return interval

    notification_due = _next_notification_due_in(github_enabled, jira_enabled)
    if notification_due > 0:
        return max(IDLE_LOOP_BREATH_SECONDS, notification_due)
    return IDLE_LOOP_BREATH_SECONDS


# Re-notify a sustained contemplative outage at most this often, so a 529
# overload streak lasting hours produces one clear message per episode
# instead of spamming every iteration.
_CONTEMP_FAILURE_NOTIFY_COOLDOWN = 6 * 3600
_CONTEMP_FAILURE_NOTIFY_FILE = ".contemplative-failure-notify.json"


def _parse_result_object(stdout_text):
    """Return the final ``{"type":"result"}`` dict from CLI stdout, or None.

    Scoped to the result object so a failed ``tool_result`` inside a stream-json
    session can never be mistaken for a session-level failure — the invariant
    holds by construction, not by coincidence. Handles both the single-object
    default output and the last result line of a stream-json transcript.
    """
    text = (stdout_text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    # Stream-json: the result object is the last {"type":"result"} line.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def _classify_contemplative_failure(exit_code, stdout_text, stderr_text):
    """Classify a contemplative session outcome.

    Returns ``(failed, signature, reason)``:
    - ``failed``: the session produced no usable reflection.
    - ``signature``: stable throttle key (e.g. ``"overload:529"``).
    - ``reason``: short human label for the notification.
    """
    stdout_text = stdout_text or ""
    stderr_text = stderr_text or ""
    combined = f"{stdout_text}\n{stderr_text}"

    result_obj = _parse_result_object(stdout_text)
    # ``is_error`` is the proven field the codebase already trusts
    # (mission_runner.check_json_success). Keying failure on it — scoped to the
    # parsed result object — means a gateway that returns {"is_error": true}
    # without ``api_error_status`` still surfaces, and a failed ``tool_result``
    # inside a stream-json session can never be misread as a session failure.
    is_error = bool(result_obj and result_obj.get("is_error") is True)
    # ``api_error_status`` is an optional label source for the HTTP code only;
    # it is NOT the failure trigger, since not every gateway/proxy emits it.
    api_status = ""
    if result_obj:
        raw_status = result_obj.get("api_error_status")
        if isinstance(raw_status, int) and raw_status > 0:
            api_status = str(raw_status)

    failed = exit_code != 0 or is_error
    if not failed:
        return False, "", ""

    from app.cli_errors import ErrorCategory, classify_cli_error

    category = classify_cli_error(exit_code, stdout_text, stderr_text)
    lowered = combined.lower()

    if api_status or "temporarily overloaded" in lowered:
        code = api_status or "5xx"
        return True, f"overload:{code}", f"provider gateway overloaded (HTTP {code})"
    if category == ErrorCategory.QUOTA:
        return True, "quota", "API quota exhausted"
    if category == ErrorCategory.AUTH:
        return True, "auth", "not authenticated (provider login needed)"
    if category == ErrorCategory.RETRYABLE:
        return True, "transient", "transient provider/network error"
    if exit_code != 0:
        return True, f"exit:{exit_code}", f"CLI exited with code {exit_code}"
    return True, "unknown", "session produced no output"


def _notify_contemplative_failure(
    instance, project_name, exit_code, stdout_file, stderr_file
):
    """Send ONE clear, throttled message when a contemplative session fails.

    Without this the contemplative path swallows the CLI exit code entirely,
    so a failed session (e.g. a provider 529) leaves the agent to emit generic
    'Run failed / went sideways' text with no real cause. Re-notifies at most
    every ``_CONTEMP_FAILURE_NOTIFY_COOLDOWN`` per failure type during a
    sustained outage, and clears the tracker on success.
    """
    try:
        stdout_text = Path(stdout_file).read_text(errors="replace") if stdout_file else ""
    except OSError:
        stdout_text = ""
    try:
        stderr_text = Path(stderr_file).read_text(errors="replace") if stderr_file else ""
    except OSError:
        stderr_text = ""

    failed, signature, reason = _classify_contemplative_failure(
        exit_code, stdout_text, stderr_text
    )
    tracker_path = Path(instance) / _CONTEMP_FAILURE_NOTIFY_FILE

    if not failed:
        # Session recovered — reset so the next outage notifies immediately.
        with contextlib.suppress(OSError):
            tracker_path.unlink(missing_ok=True)
        return

    now = int(time.time())
    should_notify = True
    try:
        data = json.loads(tracker_path.read_text())
        if (
            data.get("signature") == signature
            and now - int(data.get("last_notify", 0)) < _CONTEMP_FAILURE_NOTIFY_COOLDOWN
        ):
            should_notify = False
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass

    prefix = f"[{project_name}] " if project_name else ""
    if signature.startswith("overload"):
        msg = (
            f"🪷 {prefix}Contemplative session couldn't run — {reason}. "
            "Transient server-side issue; retrying next iteration. No action needed."
        )
    elif signature == "quota":
        msg = (
            f"🪷 {prefix}Contemplative session paused — API quota exhausted. "
            "Runs auto-resume once the quota resets."
        )
    elif signature == "auth":
        msg = (
            f"🪷 {prefix}Contemplative session couldn't run — provider not "
            "authenticated. A re-login may be needed."
        )
    else:
        msg = f"🪷 {prefix}Contemplative session ended early: {reason}."

    if should_notify:
        _notify(instance, msg)
        try:
            from app.utils import atomic_write_json

            atomic_write_json(
                tracker_path, {"signature": signature, "last_notify": now}
            )
        except OSError as e:
            log("warn", f"Could not persist contemplative-failure tracker: {e}")
    log("warn", f"Contemplative session failed ({signature}): {reason}")


def _handle_contemplative(
    plan: dict,
    run_num: int,
    max_runs: int,
    koan_root: str,
    instance: str,
    interval: int,
):
    """Run a contemplative session and sleep afterwards."""
    project_name = plan["project_name"]
    log("pause", "Decision: CONTEMPLATIVE mode (random reflection)")
    print("  Action: Running contemplative session instead of autonomous work")
    print()
    _notify(instance, f"🪷 Run {run_num}/{max_runs} — Contemplative mode on {project_name}")

    log("pause", "Running contemplative session...")
    contemp_start = int(time.time())
    try:
        from app.contemplative_runner import build_contemplative_command
        cmd = build_contemplative_command(
            instance=instance,
            project_name=project_name,
            session_info=f"Run {run_num}/{max_runs} on {project_name}. Mode: {plan['autonomous_mode']}.",
        )
        fd_out, stdout_file = tempfile.mkstemp(prefix="koan-contemp-out-", dir=koan_tmp_dir())
        os.close(fd_out)
        fd_err, stderr_file = tempfile.mkstemp(prefix="koan-contemp-err-", dir=koan_tmp_dir())
        os.close(fd_err)
        cli_error = None
        contemp_exit = 1
        try:
            contemp_exit = run_claude_task(
                cmd, stdout_file, stderr_file, cwd=koan_root,
                instance_dir=instance, project_name=project_name, run_num=run_num,
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            cli_error = traceback.format_exc()
            log("warn", f"Contemplative CLI failed: {e}")
        duration_seconds = int(time.time()) - contemp_start
        # Log contemplative usage before temp files are cleaned up
        try:
            from app.mission_runner import _log_activity_usage
            _log_activity_usage(
                instance, project_name, stdout_file,
                "contemplative", "",
                duration_seconds=duration_seconds,
            )
        except Exception as e:
            log("warn", f"Failed to log contemplative usage: {e}")
        # Record session outcome so contemplative sessions feed into
        # staleness detection, Thompson Sampling, and success-rate metrics.
        try:
            from app.mission_runner import (
                _read_pending_content,
                _read_stdout_summary,
                _record_session_outcome,
            )
            pending_content = _read_pending_content(instance)
            if not pending_content.strip():
                pending_content = _read_stdout_summary(stdout_file)
            _record_session_outcome(
                instance, project_name,
                plan.get("autonomous_mode", "unknown"),
                max(1, duration_seconds // 60),
                pending_content,
                mission_type="contemplative",
            )
        except Exception as e:
            log("warn", f"Failed to record contemplative outcome: {e}")
        # Surface a clear reason when the session failed instead of leaving
        # the user with generic 'Run failed...' text. Read before cleanup.
        try:
            _notify_contemplative_failure(
                instance, project_name, contemp_exit, stdout_file, stderr_file
            )
        except Exception as e:
            log("warn", f"Contemplative failure notification failed: {e}")
        _cleanup_temp(stdout_file, stderr_file)
        if cli_error:
            log("error", f"Contemplative error:\n{cli_error}")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log("error", f"Contemplative error: {e}\n{traceback.format_exc()}")
    log("pause", "Contemplative session ended.")

    # Commit any journal/memory changes from the contemplative session.
    # Without this, writings are lost if the agent crashes before the
    # next successful iteration commits.
    _commit_instance(instance)

    if check_pending_missions(instance):
        log("koan", "Pending missions found after contemplation — skipping sleep")
    else:
        set_status(koan_root, f"Idle — post-contemplation sleep ({time.strftime('%H:%M')})")
        log("pause", f"Contemplative session complete. Sleeping {interval}s...")
        # Idle page-cache reclaim (#2374) fires inside interruptible_sleep itself.
        with protected_phase("Sleeping between runs"):
            wake = interruptible_sleep(interval, koan_root, instance)
        if wake == "mission":
            log("koan", "New mission detected during sleep — waking up early")


def _handle_wait_pause(
    plan: dict,
    count: int,
    koan_root: str,
    instance: str,
):
    """Enter pause mode when budget is exhausted (WAIT action)."""
    from app.usage_tracker import _get_budget_mode
    if _get_budget_mode() == "disabled":
        log("quota", "WAIT pause suppressed — budget gating is disabled")
        return

    project_name = plan["project_name"]
    log("quota", "Decision: WAIT mode (budget exhausted)")
    print(f"  Reason: {plan['decision_reason']}")
    print("  Action: Entering pause mode (will auto-resume when quota resets)")
    print()
    try:
        from app.send_retrospective import create_retrospective
        create_retrospective(Path(instance), project_name)
    except Exception as e:
        log("error", f"Retrospective sending failed: {e}\n{traceback.format_exc()}")

    # Commit retrospective before entering pause — otherwise these
    # journal/memory writes are lost if the machine reboots while paused.
    _commit_instance(instance)

    reset_ts, reset_display = _compute_quota_reset_ts(instance)
    from app.pause_manager import create_pause
    create_pause(koan_root, "quota", reset_ts, reset_display)

    quota_details = plan['decision_reason']
    if plan["display_lines"]:
        quota_details += "\n" + "\n".join(plan["display_lines"])

    _notify(instance, (
        f"⏸️ Kōan paused: budget exhausted after {count} runs on [{project_name}].\n"
        f"{quota_details}\n"
        f"Auto-resume when session resets or use /resume."
    ))


def _run_preflight_check(
    plan: dict,
    koan_root: str,
    instance: str,
    count: int,
) -> bool:
    """Run pre-flight quota check before mission/autonomous execution.

    Returns True if quota is exhausted (caller should abort), False to proceed.
    """
    project_path = plan["project_path"]
    project_name = plan["project_name"]
    try:
        from app.preflight import preflight_quota_check
        pf_ok, pf_error = preflight_quota_check(
            project_path=project_path,
            instance_dir=instance,
            project_name=project_name,
        )
        if not pf_ok:
            log("quota", "Pre-flight probe detected quota exhaustion")
            pf_reset_ts, pf_reset_display = _compute_preflight_reset_ts(pf_error)
            from app.pause_manager import create_pause
            create_pause(koan_root, "quota", pf_reset_ts, pf_reset_display)
            label = plan["mission_title"] if plan["mission_title"] else "autonomous run"
            _notify(instance, (
                f"⏸️ Pre-flight quota check failed before [{project_name}] {label}.\n"
                f"Pausing until quota resets. Use /resume to restart manually."
            ))
            return True
    except Exception as e:
        log("error", f"Pre-flight quota check error: {e}")
    return False


# ---------------------------------------------------------------------------
# Mission execution state (shared with mission_executor.py)
# ---------------------------------------------------------------------------

# Set by run_claude_task when the watchdog timer kills a runaway session.
_last_mission_timed_out = False
_last_mission_aborted = False
# Human phrase for a mission the cgroup scope OOM-killed, e.g.
# "exceeded memory cap (5.9G of 5.75G)"; "" when the mission fit. Read by
# mission_runner (hook context) and mission_executor (never retry a cap hit).
_last_mission_memory_cap = ""
# Uses threading.Event for explicit cross-thread signaling between the
# stagnation daemon (writer) and the main loop's _finalize_mission (reader).
_last_mission_stagnated = threading.Event()
_stagnation_pattern_type = ""
_stagnation_pattern_excerpt = ""

# Startup phase for first-iteration Telegram visibility. Replaces the old
# _startup_notified / _boot_notified boolean pair: their only valid combinations
# were (first+boot), (first only), and (neither), which map 1:1 to these states.
#   "boot"    — the very first iteration since process start: emit boot banners
#               (empty-state "Notifications clear", update hint) plus cold-start ping.
#   "resume"  — first iteration after /resume or a counter reset (but not boot):
#               cold-start ping only; boot-only banners stay silent.
#   "running" — steady state: quiet (no first-iteration pings).
_startup_phase = "boot"


def _mark_startup_resume() -> None:
    """Downgrade to the 'resume' phase after /resume or a counter reset.

    Leaves the phase at 'boot' when the first boot iteration has not run yet
    (start-paused, then resumed before any iteration) so boot-only banners
    still fire exactly once. Only a completed boot iteration ('running')
    downgrades to 'resume'.
    """
    global _startup_phase
    if _startup_phase == "running":
        _startup_phase = "resume"


_warned_missing_projects: set = set()

# ---------------------------------------------------------------------------
# Parallel session state (populated/consumed by _parallel_reap_sessions
# and _parallel_dispatch_session; only used when max_parallel_sessions > 1)
# ---------------------------------------------------------------------------
# session_id -> Session object (with _proc attached — NOT persisted to JSON)
_live_sessions: dict = {}
# Lazy SessionRegistry singleton — initialised on first parallel-mode access
_session_registry = None


def _get_session_registry(instance: str):
    """Return the lazy-initialised SessionRegistry singleton."""
    global _session_registry
    if _session_registry is None:
        from app.session_manager import SessionRegistry
        _session_registry = SessionRegistry(instance)
    return _session_registry


def _parallel_reap_sessions(
    instance: str,
    koan_root: str,
    run_num: int,
    max_runs: int,
) -> bool:
    """Poll active parallel sessions; process any that have completed.

    Returns True if at least one session was reaped.
    """
    if not _live_sessions:
        return False

    from app.session_manager import poll_sessions
    from app.missions import complete_mission_by_session, fail_mission_by_session
    from app.mission_runner import run_post_mission

    registry = _get_session_registry(instance)
    active = list(_live_sessions.values())
    completed = poll_sessions(active, registry)
    if not completed:
        return False

    quota_hit = False
    for result in completed:
        session = result.session
        _live_sessions.pop(session.id, None)

        log("koan", f"[parallel] Session {session.id} done (exit={result.exit_code}, "
            f"project={session.project_name})")

        # Post-mission pipeline
        try:
            post = run_post_mission(
                instance_dir=instance,
                project_name=session.project_name,
                project_path=session.worktree_path,
                run_num=run_num,
                exit_code=result.exit_code,
                stdout_file=session.stdout_file,
                stderr_file=session.stderr_file,
                mission_title=session.mission_text,
                autonomous_mode=getattr(session, "autonomous_mode", "implement"),
                start_time=int(session.started_at),
                status_callback=lambda step: set_status(koan_root, f"[parallel] {step}"),
                # This session's own cap verdict (read from its ScopedProcess
                # in poll_sessions). The sequential loop's
                # _last_mission_memory_cap belongs to a different mission and
                # would be both a false positive here and a false negative for
                # a session the scope really did OOM-kill.
                memory_cap_detail=result.memory_cap_detail,
            )
        except Exception as e:
            log("error", f"[parallel] post-mission failed for {session.id}: {e}")
            post = {"success": False, "quota_exhausted": False}

        # Persist missions.md state transition via locked read-modify-write
        try:
            from app.utils import modify_missions_file
            missions_path = Path(instance) / "missions.md"
            if result.exit_code == 0:
                modify_missions_file(missions_path, lambda c: complete_mission_by_session(c, session.id))
            else:
                modify_missions_file(missions_path, lambda c: fail_mission_by_session(c, session.id))
        except Exception as e:
            log("error", f"[parallel] missions.md update failed for {session.id}: {e}")
            try:
                dead_letter = Path(instance) / ".failed-transitions.json"
                import json
                entries = []
                if dead_letter.exists():
                    entries = json.loads(dead_letter.read_text())
                entries.append({
                    "session_id": session.id,
                    "mission_text": session.mission_text,
                    "exit_code": result.exit_code,
                    "project": session.project_name,
                })
                atomic_write(dead_letter, json.dumps(entries, indent=2))
            except Exception as dle:
                log("error", f"[parallel] dead-letter write also failed: {dle}")

        # End-of-mission notification
        try:
            _notify_mission_end(
                instance, session.project_name, run_num, max_runs,
                result.exit_code, session.mission_text,
                pr_url=post.get("pr_url", ""),
            )
        except Exception as e:
            log("error", f"[parallel] notification failed for {session.id}: {e}")

        if post.get("quota_exhausted"):
            quota_hit = True

    registry.clear_completed()
    _commit_instance(instance)

    if quota_hit:
        log("quota", "[parallel] Quota exhausted — no new sessions will be dispatched")
        _notify(instance, "⚠️ API quota exhausted in a parallel session. "
                "Existing sessions will finish; no new sessions until quota resets.")

    return True


def _parallel_dispatch_sessions(
    primary_mission: str,
    primary_project: str,
    primary_project_path: str,
    instance: str,
    koan_root: str,
    run_num: int,
    max_runs: int,
    autonomous_mode: str,
    projects: list,
    last_project: str,
) -> bool:
    """Spawn the primary session and fill remaining free slots.

    Returns True if at least one session was dispatched.
    """
    from app.session_manager import spawn_session, get_max_parallel_sessions
    from app.missions import start_mission_parallel, pick_missions, extract_project_tag
    from app.git_sync import run_git

    registry = _get_session_registry(instance)
    max_slots = get_max_parallel_sessions()
    active_count = len(registry.get_active())

    if active_count >= max_slots:
        log("koan", f"[parallel] All {max_slots} slots occupied — waiting for completions")
        return False

    missions_to_dispatch = [(primary_mission, primary_project, primary_project_path)]

    # Fill remaining slots using pick_missions() which reads all N at once,
    # avoiding the duplicate-pick bug from calling _pick_mission() in a loop
    # (it's read-only and returns the same first pending each time).
    extra = max_slots - active_count - 1
    if extra > 0:
        # Exclude projects with active sessions + the primary project
        active_projects = {s.project_name.lower() for s in registry.get_active()}
        active_projects.add(primary_project.lower())
        try:
            missions_path = Path(instance) / "missions.md"
            content = missions_path.read_text()
            extras = pick_missions(content, n=extra, exclude_projects=list(active_projects))
        except Exception as e:
            log("error", f"[parallel] pick_missions failed: {e}")
            extras = []

        # Build project path lookup
        path_by_name = {name.lower(): path for name, path in (projects or [])}

        for mission_text in extras:
            proj_name = extract_project_tag(mission_text)
            if not proj_name:
                log("warn", f"[parallel] Skipping mission without project tag: {mission_text[:60]}")
                continue
            proj_path = path_by_name.get(proj_name.lower(), "")
            if not proj_path:
                log("warn", f"[parallel] Skipping mission — no path for project '{proj_name}': {mission_text[:60]}")
                continue
            if registry.get_by_project(proj_name):
                continue
            missions_to_dispatch.append((mission_text, proj_name, proj_path))

    dispatched = 0
    for mission_text, project_name, project_path in missions_to_dispatch:
        if registry.get_by_project(project_name):
            log("koan", f"[parallel] {project_name} already has an active session — skipping")
            continue

        # Determine base branch — skip dispatch if detection fails entirely
        base_branch = None
        try:
            branch = run_git(project_path, "rev-parse", "--abbrev-ref", "HEAD")
            if branch:
                base_branch = branch.strip()
        except Exception as e:
            log("warn", f"[parallel] rev-parse HEAD failed for {project_name}: {e}")
        if not base_branch:
            try:
                ref = run_git(project_path, "symbolic-ref", "refs/remotes/origin/HEAD")
                if ref:
                    base_branch = ref.strip().removeprefix("refs/remotes/origin/")
            except Exception as e:
                log("warn", f"[parallel] symbolic-ref failed for {project_name}: {e}")
        if not base_branch:
            log("warn", f"[parallel] Could not detect base branch for {project_name} — skipping")
            continue

        try:
            session = spawn_session(
                mission_text=mission_text,
                project_name=project_name,
                project_path=project_path,
                instance_dir=instance,
                registry=registry,
                autonomous_mode=autonomous_mode,
                base_branch=base_branch,
            )
        except Exception as e:
            log("error", f"[parallel] spawn failed for [{project_name}]: {e}")
            continue

        # Keep in-memory reference (preserves _proc for poll_sessions)
        _live_sessions[session.id] = session

        # Transition mission Pending → In Progress in missions.md
        try:
            from app.utils import modify_missions_file
            missions_path = Path(instance) / "missions.md"
            modify_missions_file(missions_path, lambda c: start_mission_parallel(c, mission_text, session.id))
        except Exception as e:
            log("error", f"[parallel] missions.md start failed for {session.id} — killing session: {e}")
            try:
                from app.session_manager import kill_session
                kill_session(session, registry)
                _live_sessions.pop(session.id, None)
            except Exception as ke:
                log("error", f"[parallel] kill_session cleanup failed: {ke}")
            continue

        log("koan", f"[parallel] Spawned {session.id} [{project_name}]: {mission_text[:60]}")
        _notify(instance, f"🚀 [{project_name}] Parallel session started: {mission_text[:60]}")
        dispatched += 1

    return dispatched > 0


# ---------------------------------------------------------------------------
# Mission execution lifecycle (extracted to mission_executor.py)
# ---------------------------------------------------------------------------

from app.mission_executor import (  # noqa: F401 — re-exported for backward compat
    _get_git_head,
    _handle_skill_dispatch,
    _maybe_retry_mission,
    _MISSION_MAX_RETRIES,
    _MISSION_RETRY_DELAY,
    _run_iteration,
)



# ---------------------------------------------------------------------------
# Error recovery
# ---------------------------------------------------------------------------

def _handle_iteration_error(
    error: Exception,
    consecutive_errors: int,
    koan_root: str,
    instance: str,
):
    """Handle an exception from _run_iteration.

    Logs the error, backs off with increasing sleep, and enters
    pause mode after ``max_consecutive_errors`` to avoid thrashing.
    """
    cfg = get_recovery_config()
    max_errors = cfg["max_consecutive_errors"]
    tb = traceback.format_exc()
    log("error", f"Iteration failed ({consecutive_errors}/{max_errors}): {error}")
    log("error", f"Traceback:\n{tb}")
    set_status(koan_root, f"Error recovery ({consecutive_errors}/{max_errors})")

    # Notify on first error and periodically
    if _should_notify_error(consecutive_errors):
        _notify(instance, (
            f"⚠️ Run loop error ({consecutive_errors}/{max_errors}): "
            f"{type(error).__name__}: {error}"
        ))

    if consecutive_errors >= max_errors:
        log("error", f"Too many consecutive errors ({consecutive_errors}). Entering pause mode.")
        _notify(instance, (
            f"🛑 Kōan entering pause mode after {consecutive_errors} consecutive errors.\n"
            f"Last error: {type(error).__name__}: {error}\n"
            f"Use /resume to restart."
        ))
        from app.pause_manager import create_pause
        create_pause(koan_root, "errors")
        return

    # Backoff with increasing delay
    backoff = _calculate_backoff(consecutive_errors, cfg["max_backoff_iteration"])
    log("koan", f"Recovering in {backoff}s...")
    time.sleep(backoff)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_quota_reset_ts(instance: str):
    """Compute quota reset timestamp and display string.

    Returns (reset_ts: int, reset_display: str). Delegates the buffer
    math (QUOTA_RESET_BUFFER_SECONDS) to
    :func:`app.quota_handler.compute_resume_info` so the buffer policy
    lives in exactly one place. Falls back to QUOTA_RETRY_SECONDS from
    now if estimation fails.
    """
    reset_ts = None
    reset_display = ""
    try:
        from app.usage_estimator import cmd_reset_time, _estimate_reset_time, _load_state
        from app.quota_handler import compute_resume_info
        usage_state_path = Path(instance, "usage_state.json")
        raw_reset_ts = cmd_reset_time(usage_state_path)
        state = _load_state(usage_state_path)
        reset_display = f"session reset in ~{_estimate_reset_time(state.get('session_start', ''), 5)}"
        if raw_reset_ts is not None:
            # compute_resume_info applies the canonical buffer; we keep the
            # estimator-derived display string instead of its resume message.
            reset_ts, _ = compute_resume_info(raw_reset_ts, reset_display)
    except Exception as e:
        log("error", f"Reset time estimation failed: {e}")
    if reset_ts is None:
        from app.pause_manager import QUOTA_RETRY_SECONDS
        reset_ts = int(time.time()) + QUOTA_RETRY_SECONDS
    return reset_ts, reset_display


def _compute_preflight_reset_ts(error_output: str):
    """Compute quota reset timestamp from preflight probe error output.

    Returns (reset_ts: int, reset_display: str). Adds the quota reset buffer
    to known reset times and falls back to QUOTA_RETRY_SECONDS from now if
    extraction fails.
    """
    reset_ts = None
    reset_display = ""
    try:
        from app.quota_handler import extract_reset_info, parse_reset_time, compute_resume_info
        reset_info = extract_reset_info(error_output or "")
        reset_ts, reset_display = parse_reset_time(reset_info)
        reset_ts, _ = compute_resume_info(reset_ts, reset_display)
    except Exception as e:
        log("error", f"Pre-flight reset time extraction failed: {e}")
    if reset_ts is None:
        from app.pause_manager import QUOTA_RETRY_SECONDS
        reset_ts = int(time.time()) + QUOTA_RETRY_SECONDS
    return reset_ts, reset_display


# ---------------------------------------------------------------------------
# Shared quota / auth error handling
# ---------------------------------------------------------------------------
# run.py had 3 nearly-identical code paths for auth/quota errors:
#   1. skill dispatch CLI error   (_handle_skill_dispatch)
#   2. regular mission CLI error  (_run_iteration)
#   3. exit-0 quota probe         (both paths)
# Factoring the shared logic here eliminates the synchronization burden.


def _handle_auth_error(
    *,
    provider_label: str,
    koan_root: str,
    instance: str,
    mission_title: str,
) -> None:
    """Requeue mission, enter auth pause, and notify on auth failure."""
    log("error", f"{provider_label} is logged out — requeueing mission to Pending")
    if not _requeue_mission_in_file(instance, mission_title):
        # Surface a dropped write: the pause still fires (provider is genuinely
        # logged out) but the mission stays In Progress, contradicting the
        # "moved back to Pending" notice below.
        log("error", "Auth re-queue write failed — mission may still be In Progress")
    from app.pause_manager import create_pause
    create_pause(koan_root, "auth")
    _notify(instance, (
        f"🔐 {provider_label} is logged out. Please re-authenticate the provider CLI.\n\n"
        "The current mission has been moved back to Pending. "
        "Use /resume after logging in."
    ))


def _handle_quota_error(
    *,
    provider_name: str,
    provider_label: str,
    koan_root: str,
    instance: str,
    project_name: str,
    mission_title: str,
    run_num: int,
    hqe_kwargs: dict,
) -> None:
    """Requeue mission, detect reset time, pause, and notify on quota exhaustion.

    *hqe_kwargs* are forwarded to :func:`handle_quota_exhaustion` — callers
    pass either ``stdout_text``/``stderr_text`` (skill path) or
    ``stdout_file``/``stderr_file`` (regular mission path).
    """
    log("quota", "API quota exhausted — requeueing mission to Pending")
    if not _requeue_mission_in_file(instance, mission_title):
        # Surface a dropped write: the quota pause still fires but the mission
        # stays In Progress, contradicting the "moved back to Pending" notice.
        log("error", "Quota re-queue write failed — mission may still be In Progress")
    from app.quota_handler import handle_quota_exhaustion, QUOTA_CHECK_UNRELIABLE
    quota_result = handle_quota_exhaustion(
        koan_root=koan_root,
        instance_dir=instance,
        project_name=project_name,
        run_count=run_num,
        provider_name=provider_name,
        **hqe_kwargs,
    )
    reset_display = ""
    if quota_result and quota_result is not QUOTA_CHECK_UNRELIABLE:
        reset_display = quota_result[0]
    else:
        reset_ts, reset_display = _compute_quota_reset_ts(instance)
        from app.pause_manager import create_pause
        create_pause(koan_root, "quota", reset_ts, reset_display)
    _notify_quota_warning(instance, (
        f"⏸️ API quota exhausted.{(' ' + reset_display) if reset_display else ''}\n"
        f"Mission '{mission_title[:60]}' moved back to Pending.\n"
        f"Use /resume after quota resets."
    ), raw_output=_quota_raw_snippet(
        stdout_text=hqe_kwargs.get("stdout_text", ""),
        stderr_text=hqe_kwargs.get("stderr_text", ""),
        stdout_file=hqe_kwargs.get("stdout_file", ""),
        stderr_file=hqe_kwargs.get("stderr_file", ""),
    ))


def _classify_and_handle_cli_error(
    exit_code: int,
    stdout_text: str,
    stderr_text: str,
    *,
    provider_name: str,
    provider_label: str,
    koan_root: str,
    instance: str,
    project_name: str,
    mission_title: str,
    run_num: int,
    hqe_kwargs: dict,
    trust_stdout: bool = True,
) -> bool:
    """Classify a non-zero CLI exit and handle AUTH / QUOTA errors.

    Shared by both the skill dispatch and regular mission paths.

    Args:
        exit_code: CLI process exit code.
        stdout_text / stderr_text: CLI output for error classification.
        hqe_kwargs: Forwarded to :func:`handle_quota_exhaustion` (text or file).
        trust_stdout: When False, stdout is treated as DATA and excluded from
            classification — only stderr (the trusted CLI channel) is scanned.
            Skill dispatches set this: their stdout is a summarized agent
            transcript that legitimately quotes CI logs and source identifiers
            (e.g. ``/ci_check`` always prints ``"quota_exhausted": false``),
            which otherwise tripped a false QUOTA classification and paused the
            daemon. Genuine skill quota propagates via the structured
            ``quota_exhausted`` result field, not via transcript scanning.

    Returns:
        True if an auth/quota error was handled (caller should return True).
    """
    if exit_code == 0:
        return False

    from app.cli_errors import ErrorCategory, classify_cli_error
    category = classify_cli_error(
        exit_code,
        stdout_text if trust_stdout else "",
        stderr_text,
        provider_name=provider_name,
    )
    # When stdout is DATA (a skill's summarized agent transcript) the broad
    # human-prose quota patterns are excluded above — they match content the
    # transcript merely quotes (CI logs, Kōan's own ``quota_exhausted`` field).
    # The CLI *runtime's* own signals, however, are safe to honor even in a
    # transcript: the "hit your session limit" abort line and a rejected
    # ``rate_limit_event`` are emitted by the runtime, not quotable prose.
    if category != ErrorCategory.QUOTA and not trust_stdout:
        from app.quota_handler import cli_runtime_quota_signal

        if cli_runtime_quota_signal(stdout_text):
            category = ErrorCategory.QUOTA
    if category != ErrorCategory.AUTH and not trust_stdout:
        if _cli_runtime_auth_signal(
            stdout_text=stdout_text,
            provider_name=provider_name,
            exit_code=exit_code,
        ):
            category = ErrorCategory.AUTH
    if category == ErrorCategory.AUTH:
        _handle_auth_error(
            provider_label=provider_label,
            koan_root=koan_root,
            instance=instance,
            mission_title=mission_title,
        )
        return True
    if category == ErrorCategory.QUOTA:
        _handle_quota_error(
            provider_name=provider_name,
            provider_label=provider_label,
            koan_root=koan_root,
            instance=instance,
            project_name=project_name,
            mission_title=mission_title,
            run_num=run_num,
            hqe_kwargs=hqe_kwargs,
        )
        return True
    return False


def _cli_runtime_auth_signal(
    *,
    stdout_text: str,
    provider_name: str,
    exit_code: int,
) -> bool:
    """Detect provider auth failures from stdout-safe runtime lines.

    Skill stdout is normally DATA, so broad stdout auth scans can false-positive
    on quoted CI logs or source text. Codex, however, reports real stream auth
    failures on stdout. Keep the trusted surface narrow: raw provider JSON
    events, Koan's ``[cli]`` stream summaries, and CLI failure summaries.
    """
    if exit_code == 0 or not stdout_text or not provider_name:
        return False

    from app.provider.base import PROVIDER_ERROR_EVENT_TYPES

    runtime_lines: list[str] = []
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[cli]") or "CLI invocation failed:" in stripped:
            runtime_lines.append(stripped)
            continue
        if not stripped.startswith("{"):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            event = json.loads(stripped)
            if (
                isinstance(event, dict)
                and str(event.get("type") or "") in PROVIDER_ERROR_EVENT_TYPES
            ):
                runtime_lines.append(stripped)

    if not runtime_lines:
        return False

    joined = "\n".join(runtime_lines)

    # Check shared auth patterns against filtered runtime lines.
    # These lines are [cli]-prefixed summaries and JSON error events —
    # Koan-generated, not agent prose — so _AUTH_RE is safe here.
    from app.cli_errors import _AUTH_RE
    if _AUTH_RE.search(joined):
        return True

    try:
        from app.provider import get_provider_by_name

        provider = get_provider_by_name(provider_name)
        return provider.detect_auth_failure(
            stdout_text=joined,
            stderr_text="",
            exit_code=exit_code,
        )
    except KeyError as e:
        print(f"[run] unknown provider {provider_name!r}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(
            f"[run] runtime auth detector failed for {provider_name!r}: {e}",
            file=sys.stderr,
        )
        return False


def _probe_exit0_quota(
    *,
    provider_name: str,
    provider_label: str,
    koan_root: str,
    instance: str,
    mission_title: str,
    run_num: int,
    hqe_kwargs: dict,
    project_name: str = "",
) -> bool:
    """Probe for quota exhaustion when CLI exited successfully (exit 0).

    Some provider wrappers emit quota payloads with exit 0.  Without this
    check the mission would be finalized to Done before any pause fires.

    Returns True if quota was detected and handled.
    """
    from app.quota_handler import handle_quota_exhaustion, QUOTA_CHECK_UNRELIABLE
    probe = handle_quota_exhaustion(
        koan_root=koan_root,
        instance_dir=instance,
        project_name=project_name,
        run_count=run_num,
        provider_name=provider_name,
        **hqe_kwargs,
    )
    if probe is None or probe is QUOTA_CHECK_UNRELIABLE:
        return False
    reset_display, resume_msg = probe
    log("quota", f"Exit-0 quota probe matched. {reset_display}")
    _requeue_mission_in_file(instance, mission_title)
    _commit_instance(instance, f"koan: quota exhausted {time.strftime('%Y-%m-%d-%H:%M')}")
    _notify_quota_warning(instance, (
        f"⏸️ {provider_label} quota exhausted.{(' ' + reset_display) if reset_display else ''}\n"
        f"Mission '{mission_title[:60]}' moved back to Pending.\n"
        f"{resume_msg} or use /resume to restart manually."
    ), raw_output=_quota_raw_snippet(
        stdout_text=hqe_kwargs.get("stdout_text", ""),
        stderr_text=hqe_kwargs.get("stderr_text", ""),
        stdout_file=hqe_kwargs.get("stdout_file", ""),
        stderr_file=hqe_kwargs.get("stderr_file", ""),
    ))
    return True


def _handle_pipeline_quota_flag(
    *,
    provider_label: str,
    koan_root: str,
    instance: str,
    mission_title: str,
    count: int,
    quota_info,
    raw_output: str = "",
) -> bool:
    """Handle the ``quota_exhausted`` flag from :func:`run_post_mission`.

    ``handle_quota_exhaustion()`` inside ``run_post_mission`` already wrote
    the journal entry and created the pause state with accurate timing.
    This function handles the notification + requeue + fallback pause when
    ``quota_info`` is missing or incomplete.

    Returns True if quota was handled.
    """
    if quota_info and isinstance(quota_info, (list, tuple)) and len(quota_info) >= 2:
        reset_display, resume_msg = quota_info[0], quota_info[1]
    else:
        reset_display, resume_msg = "", "Auto-resume in ~5h"
        reset_ts, _disp = _compute_quota_reset_ts(instance)
        from app.pause_manager import create_pause
        create_pause(koan_root, "quota", reset_ts, reset_display or _disp)
    log("quota", f"Quota reached. {reset_display}")

    if mission_title:
        log("quota", "Requeueing mission to Pending (quota is transient)")
        _requeue_mission_in_file(instance, mission_title)

    _commit_instance(instance, f"koan: quota exhausted {time.strftime('%Y-%m-%d-%H:%M')}")
    _notify_quota_warning(instance, (
        f"⚠️ {provider_label} quota exhausted. {reset_display}\n\n"
        f"Mission '{mission_title[:60]}' moved back to Pending.\n"
        f"Kōan paused after {count} runs. {resume_msg} or use /resume to restart manually."
    ), raw_output=raw_output)
    return True


def _reset_usage_session(instance: str):
    """Reset internal usage session counters after resume.

    Ensures the usage estimator starts fresh so it doesn't
    re-pause immediately with stale high usage from the
    exhausted session.
    """
    try:
        from app.usage_estimator import cmd_reset_session
        usage_state = Path(instance, "usage_state.json")
        usage_md = Path(instance, "usage.md")
        cmd_reset_session(usage_state, usage_md)
        log("health", "Usage session counters reset after resume")
    except Exception as e:
        log("error", f"Usage session reset failed: {e}")


def _clear_if_cap_hit(instance: str, mission_title: str, project_name: str = "") -> bool:
    """Clear retry counter when a capped mission is being restarted by the human.

    The retry counter is preserved while a mission is in Failed state so the
    human can inspect the count. This function clears it when start_mission()
    signals a deliberate human retry: we detect a "human retry" by checking
    whether any per-system cap was hit (stagnation count >= max_retry, or
    crash_count >= max_crash_retries, or total_attempts >= max_total_retries).

    For ongoing stagnation-retry requeus (count < cap) the counter is NOT
    cleared — the stagnation cap check in _finalize_mission depends on it.

    *project_name* must match the value _finalize_mission uses so the cap
    detection here reads the same per-project overrides; passing "" (global
    config) when the mission has project-specific caps would diverge — a
    human retry could be silently ignored.

    Returns True when nothing needed clearing or the counter was cleared
    successfully; returns False when a cap was hit but clearing the counter
    failed (the caller should warn prominently — the mission will re-escalate
    to Failed on the next finalize).

    Only ImportError is caught (retry tracking simply unavailable). Schema
    mismatches in get_retry_info/get_stagnation_config are allowed to propagate
    so genuine bugs surface rather than being silently swallowed; the caller
    guards the call so such an error cannot abort an already-started mission.
    """
    try:
        from app.stagnation_monitor import clear_retry_count, get_retry_info
        from app.config import get_stagnation_config
    except ImportError as e:
        # Retry tracking unavailable — there is nothing to clear.
        log("error", f"Retry counter clear skipped (import failed): {e}")
        return True

    # Hot path: this runs on every mission start, including brand-new
    # missions that have never been retried. Read the tracker once and bail
    # before loading config when there is no entry (all counts zero) — those
    # missions can never have hit a cap, so there is nothing to clear.
    info = get_retry_info(instance, mission_title)
    stag_count = info["count"]
    crash_count = info["crash_count"]
    total = info["total_attempts"]
    if not (stag_count or crash_count or total):
        return True

    cfg = get_stagnation_config(project_name)
    # get_stagnation_config() always returns every key, so read them directly:
    # fallback defaults here would silently drift from the centralized config
    # defaults if those ever change.
    max_stag = cfg["max_retry_on_stagnation"]
    max_crash = cfg["max_crash_retries"]
    max_total = cfg["max_total_retries"]

    stag_capped = max_stag > 0 and stag_count >= max_stag
    crash_capped = crash_count >= max_crash
    total_capped = max_total > 0 and total >= max_total

    if stag_capped or crash_capped or total_capped:
        try:
            clear_retry_count(instance, mission_title)
        except Exception as e:
            log("error", f"Retry counter clear failed for capped mission: {e}")
            return False
    return True


def _start_mission_in_file(instance: str, mission_title: str, project_name: str = "") -> bool:
    """Move mission from Pending to In Progress via locked write.

    Returns True if the transition was confirmed (mission visible in In Progress
    after the write), False if the mission was not found or the transition could
    not be verified. A False return is logged as a WARNING — the caller should
    treat the mission as if it never started.
    """
    try:
        from app.missions import parse_sections, start_mission
        from app.utils import modify_missions_file
        missions_path = Path(instance, "missions.md")
        if not missions_path.exists():
            return False

        # start_mission() runs a sanity flush: any stale In Progress missions
        # are moved to Failed (with a [flushed] tag) before the new one starts.
        # Under normal operation this never fires because recover.py clears
        # stale entries at startup — so when it DOES fire, surface it.
        # Capture the pre-flush In Progress inside the lock to stay race-safe.
        stale_flushed: list = []

        def _transform(content: str) -> str:
            stale_flushed[:] = parse_sections(content).get("in_progress", [])
            return start_mission(content, mission_title)

        after = modify_missions_file(missions_path, _transform)
        in_progress = parse_sections(after).get("in_progress", [])
        # Confirm the transition using the SAME canonical identity that the
        # removal step (_remove_item_by_text) uses, not a raw substring. The
        # complexity classifier rewrites the on-disk Pending line by injecting
        # ``[complexity:X]`` *between* the mission body and the ⏳ timestamp
        # (tag_complexity_in_pending) AFTER ``mission_title`` was captured. A
        # naive ``mission_title in entry`` check then fails because the in-memory
        # title (``… 📬 ⏳(…)``) is no longer a contiguous substring of the stored
        # line (``… 📬 [complexity:X] ⏳(…)``), even though the move succeeded —
        # leaving an orphaned "zombie" In Progress entry. canonical_mission_key()
        # strips the complexity tag and lifecycle timestamps from both sides, so
        # the same logical mission matches regardless of when the tag was added.
        import re
        from app.missions import canonical_mission_key
        clean_title = re.sub(r"\s+", " ", canonical_mission_key(mission_title))
        for entry in in_progress:
            entry_text = re.sub(r"\s+", " ", canonical_mission_key(entry))
            if clean_title and clean_title in entry_text:
                # Clear counter only if a cap was previously hit (human deliberate
                # retry) — stagnation-retry requeus must keep their count intact
                # so the stagnation cap check in _finalize_mission still fires.
                # The transition above already succeeded, so a failure to clear
                # the counter must NOT abort the start; surface it as a prominent
                # warning instead, since the mission will otherwise re-escalate to
                # Failed on the next finalize.
                try:
                    if not _clear_if_cap_hit(instance, mission_title, project_name):
                        log("warning",
                            f"Retry counter NOT cleared for '{clean_title[:60]}' — "
                            "clear failed; mission may re-escalate to Failed on next finalize.")
                except Exception as e:
                    # _clear_if_cap_hit only catches ImportError internally and lets
                    # schema mismatches (e.g. a missing get_retry_info key) propagate —
                    # those are real bugs, so log at error level with a traceback
                    # rather than burying them in a warning.
                    import traceback
                    log("error",
                        f"Retry counter clear errored for '{clean_title[:60]}' ({e}); "
                        "mission may re-escalate to Failed on next finalize.\n"
                        f"{traceback.format_exc()}")

                # Only surface the sanity flush once the transition is
                # confirmed: start_mission() early-returns (skipping the
                # flush) when the mission isn't in Pending, so stale_flushed
                # being non-empty does NOT by itself mean a flush happened.
                if stale_flushed:
                    titles = ", ".join(
                        s.split("\n")[0].strip().removeprefix("- ")[:50]
                        for s in stale_flushed
                    )
                    log("warning", (
                        f"Sanity flush: {len(stale_flushed)} stale In Progress "
                        f"mission(s) moved to Failed by start_mission() — "
                        f"recover.py missed them "
                        f"(see _flush_in_progress_to_failed): {titles}"
                    ))
                # Raise the GitHub "Running" indicator now that the transition
                # is confirmed. Best-effort — never block the mission start.
                try:
                    from app.mission_status import start_indicator
                    start_indicator(instance, mission_title, project_name)
                except Exception as e:
                    log("error", f"running-indicator start error: {e}")
                return True
        log("warning", f"Mission transition unconfirmed — '{clean_title[:60]}' "
            "not found in In Progress after start_mission(). "
            "Possible text normalisation mismatch or race condition.")
        return False
    except Exception as e:
        log("error", f"Could not start mission in missions.md: {e}")
        return False


def _update_mission_in_file(
    instance: str,
    mission_title: str,
    *,
    failed: bool = False,
    cause_tag: str = "",
) -> bool:
    """Move mission from Pending/In Progress to Done/Failed via locked write.

    *cause_tag* is only honored when *failed* is True; it is appended to
    the missions.md entry (e.g. ``[stagnation]``) so the failure reason
    is visible without digging through journals.

    Returns True if the mission was actually moved, False otherwise (e.g.
    the mission text could not be matched in Pending/In Progress). A False
    return means the mission is still in the queue and will be re-picked —
    callers should surface this rather than let it loop silently.

    History trimming is intentionally NOT done here. Pruning old
    Done/Failed entries is a separate maintenance concern run as its own
    locked step (:func:`_prune_missions_history`) after the move commits, so
    a pruning bug can never corrupt or roll back the finalization itself.
    """
    try:
        from app.missions import complete_mission_checked, fail_mission_checked
        from app.utils import modify_missions_file
        missions_path = Path(instance, "missions.md")
        if not missions_path.exists():
            return False

        # The move functions report found-status directly, captured via a
        # closure flag. This is more robust than comparing before/after
        # content, and history pruning is decoupled to its own step
        # (_prune_missions_history below) so it cannot interfere here.
        found = [False]

        if failed:
            def transform(content):
                new_content, ok = fail_mission_checked(
                    content, mission_title, cause_tag=cause_tag,
                )
                found[0] = ok
                return new_content
        else:
            def transform(content):
                new_content, ok = complete_mission_checked(content, mission_title)
                found[0] = ok
                return new_content

        modify_missions_file(missions_path, transform)
        if not found[0]:
            log("warning", f"Mission not found (no change): {mission_title[:80]}")
            return False
        # Move committed — trim history as a decoupled, best-effort step.
        _prune_missions_history(instance)
        return True
    except Exception as e:
        label = "fail" if failed else "complete"
        log("error", f"Could not {label} mission in missions.md: {e}")
        return False


def _prune_missions_history(instance: str) -> None:
    """Trim old Done/Failed entries from missions.md as a standalone step.

    Decoupled from mission finalization: runs as its own locked
    read-modify-write so a pruning error cannot corrupt or roll back the
    Done/Failed move that just committed. Uses the missions lock (unlike the
    startup-time :func:`app.startup_manager.prune_missions_done`, which is
    safe to run unlocked only because nothing else writes during startup) so
    it cannot race the bridge inserting new missions. Best-effort: any error
    is logged and swallowed.
    """
    try:
        from app.config import (
            get_missions_done_keep,
            get_missions_failed_keep,
            get_missions_max_lines,
        )
        from app.missions import enforce_size_bound
        from app.utils import modify_missions_file
        missions_path = Path(instance, "missions.md")
        if not missions_path.exists():
            return

        pruned = [0]

        def _transform(content):
            new_content, count = enforce_size_bound(
                content,
                max_lines=get_missions_max_lines(),
                done_keep=get_missions_done_keep(),
                failed_keep=get_missions_failed_keep(),
            )
            pruned[0] = count
            return new_content

        modify_missions_file(missions_path, _transform)
        if pruned[0] > 0:
            log("health", f"Pruned {pruned[0]} old Done/Failed items from missions.md")
    except Exception as e:
        log("error", f"Missions history pruning failed: {e}")


def _requeue_mission_in_file(instance: str, mission_title: str, append_tag: str = "") -> bool:
    """Move mission from In Progress back to Pending via locked write.

    When *append_tag* is set, it is appended to the re-queued mission text (a
    prior ``[verify-failed: …]`` tag is replaced rather than stacked).

    Returns ``True`` when the write succeeded, ``False`` otherwise. Callers that
    bump retry counters / notify on a re-queue must check this so they do not
    report a re-queue that never landed (leaving the mission stuck In Progress).
    """
    try:
        from app.missions import requeue_mission
        from app.utils import modify_missions_file
        missions_path = Path(instance, "missions.md")
        if not missions_path.exists():
            return False
        modify_missions_file(
            missions_path,
            lambda c: requeue_mission(c, mission_title, append_tag=append_tag),
        )
        return True
    except Exception as e:
        log("error", f"Could not requeue mission in missions.md: {e}")
        return False


def _finalize_mission(
    instance: str,
    mission_title: str,
    project_name: str,
    exit_code: int,
    *,
    failure_reason: Optional[str] = None,
    failure_detail: Optional[str] = None,
    verify_requeue: bool = False,
    verify_summary: str = "",
) -> bool:
    """Complete or fail a mission and record execution history.

    When the last mission was killed by the stagnation monitor, the
    module-level flag ``_last_mission_stagnated`` is read and cleared
    here. Stagnation handling is gated by ``max_retry_on_stagnation``
    in the stagnation config:

    - if the per-mission retry count is below the cap, the mission is
      re-queued to Pending (not failed), the counter is incremented,
      and a "retry" Telegram notification is sent;
    - once the cap is reached, the mission is marked Failed with a
      ``[stagnation]`` tag. The counter is preserved so the human can
      inspect it while the mission sits in Failed. It is cleared when
      the human deliberately retries via ``_start_mission_in_file``.

    On success, all retry counters are cleared. On failure (stagnation
    cap or crash) the counters are left intact for diagnostic visibility
    while the mission remains in Failed state.

    Returns ``True`` when the mission was re-queued to Pending (verify-failure
    or stagnation retry) instead of being completed/failed — callers must
    treat this as "not actually finished" and skip end-of-mission notifications
    that announce completion.
    """
    failed = exit_code != 0
    cause_tag = ""
    stagnated = False
    if failed and _last_mission_stagnated.is_set():
        stagnated = True
        _last_mission_stagnated.clear()

    # Verify-failure re-queue (exit 0, but post-mission verification said the
    # mission did not actually do what its title implies). Runs before the
    # success-path counter clear so a re-queued mission keeps its retry state.
    if verify_requeue and exit_code == 0 and not stagnated:
        from app.config import get_stagnation_config, get_verify_requeue_max
        from app.stagnation_monitor import (
            get_total_attempts,
            get_verify_count,
            increment_verify_count,
        )

        max_verify = get_verify_requeue_max()
        max_total = get_stagnation_config(project_name)["max_total_retries"]
        already = get_verify_count(instance, mission_title)
        total = get_total_attempts(instance, mission_title)
        total_cap_hit = max_total > 0 and total >= max_total
        if max_verify > 0 and already < max_verify and not total_cap_hit:
            # Persist the counter first. If it can't be written, do NOT re-queue:
            # the on-disk count would stay below the cap and the mission could be
            # re-queued unboundedly. Fall through to normal completion instead.
            new_count = increment_verify_count(instance, mission_title)
            if new_count < 0:
                log("error", "Verify re-queue aborted: could not persist counter")
            else:
                # Brackets in the summary would corrupt the [verify-failed: …]
                # tag (the strip regex stops at the first ]); neutralize them.
                safe_summary = verify_summary.replace("[", "(").replace("]", ")")
                tag = (
                    f"[verify-failed: {safe_summary[:120]}]"
                    if safe_summary else "[verify-failed]"
                )
                # Only announce / record the re-queue once the missions.md write
                # is confirmed, so the user is never told a re-queue landed when
                # the mission is actually still stuck In Progress.
                if _requeue_mission_in_file(instance, mission_title, append_tag=tag):
                    log("koan", (
                        f"Verify re-queue {new_count}/{max_verify} — "
                        f"requeueing mission: {mission_title[:60]}"
                    ))
                    _notify_verify_requeue(
                        mission_title, project_name, new_count, max_verify, verify_summary,
                    )
                    try:
                        from app.mission_history import record_execution
                        record_execution(instance, mission_title, project_name, exit_code)
                    except (OSError, ValueError) as e:
                        log("error", f"Mission history recording error: {e}")
                    return True
                log("error", "Verify re-queue write failed; completing normally")
        # Cap reached (or re-queue could not be applied) → fall through to normal
        # completion (Done). verify_blocking already prevented auto-merge, so a
        # human reviews the draft PR.

    if stagnated:
        from app.config import get_stagnation_config
        from app.stagnation_monitor import (
            get_retry_count,
            get_total_attempts,
            increment_retry_count,
        )

        pattern = _stagnation_pattern_type or "unknown"
        excerpt = _stagnation_pattern_excerpt or ""

        cfg = get_stagnation_config(project_name)
        # Direct key access (no .get() fallback): get_stagnation_config() always
        # returns every key, so a missing key is a real schema bug that should
        # surface loudly rather than silently defaulting — and a `0` fallback for
        # max_retry_on_stagnation would be semantically wrong (0 disables retries;
        # the actual default is 3). Consistent with _clear_if_cap_hit.
        max_retry = cfg["max_retry_on_stagnation"]
        max_total = cfg["max_total_retries"]
        already = get_retry_count(instance, mission_title)
        total = get_total_attempts(instance, mission_title)
        total_cap_hit = max_total > 0 and total >= max_total
        if max_retry > 0 and already < max_retry and not total_cap_hit:
            new_count = increment_retry_count(
                instance, mission_title,
                pattern_type=pattern, pattern_excerpt=excerpt,
            )
            log("koan", (
                f"Stagnation retry {new_count}/{max_retry} ({pattern}) — "
                f"requeueing mission: {mission_title[:60]}"
            ))
            # Only announce / record the retry once the missions.md write is
            # confirmed, so the user is never told a re-queue landed while the
            # mission is actually still stuck In Progress. On write failure,
            # fall through to the Failed path below rather than silently losing
            # the mission.
            if _requeue_mission_in_file(instance, mission_title):
                _notify_stagnation_retry(
                    mission_title, project_name, new_count, max_retry,
                    pattern_type=pattern, pattern_excerpt=excerpt,
                )
                try:
                    from app.mission_history import record_execution
                    record_execution(instance, mission_title, project_name, exit_code)
                except (OSError, ValueError) as e:
                    log("error", f"Mission history recording error: {e}")
                return True
            log("error", "Stagnation re-queue write failed; marking mission Failed")

        # Retry cap reached (or retries disabled): mark Failed with cause tag.
        # Counter is preserved — cleared when the human retries the mission.
        if total_cap_hit:
            cause_tag = f"stagnation:{pattern}:total_cap({total}/{max_total})"
        else:
            cause_tag = f"stagnation:{pattern}"
        _notify_stagnation(mission_title, project_name, pattern, excerpt)

        # Capture experience for stagnation-cap failures (outcome='reverted').
        # Placed after the retry-cap check so it only fires when the mission
        # is actually being marked Failed, not on requeue (which returns early
        # at the increment_retry_count branch above).
        try:
            from app.experience_capture import capture_experience
            capture_experience(
                instance_dir=instance,
                project_name=project_name,
                mission_title=mission_title,
                exit_code=exit_code,
                outcome="reverted",
                root_cause=f"Agent stuck in loop: {pattern}",
                approach="(abandoned — stagnation cap hit)",
            )
        except Exception as e:
            log("error", f"Experience capture for stagnation failed: {e}")
    else:
        # On success, clear all retry counters so the next run starts fresh.
        # On failure, leave counters intact so the human can see why the
        # mission ended up in Failed while inspecting .mission-retries.json.
        if exit_code == 0:
            try:
                from app.stagnation_monitor import clear_retry_count
                clear_retry_count(instance, mission_title)
            except Exception as e:
                log("error", f"Stagnation retry counter cleanup error: {e}")

    _update_mission_in_file(
        instance, mission_title, failed=failed, cause_tag=cause_tag,
    )
    # Lower the GitHub "Running" indicator now that the mission has reached a
    # terminal state. The stagnation-requeue path returns early above (mission
    # goes back to Pending), so it deliberately leaves the indicator up.
    # Best-effort — never block finalization.
    try:
        from app.mission_status import resolve_indicator
        resolve_indicator(instance, mission_title, success=(not failed))
    except Exception as e:
        log("error", f"running-indicator resolve error: {e}")
    # Record the authoritative terminal outcome for the REST API. Best-effort:
    # a log hiccup must not block finalization. record_outcome is imported
    # locally (matches the record_execution convention below) — tests patch it
    # at its source module, app.mission_outcome.record_outcome.
    try:
        from app.mission_outcome import classify_failure, record_outcome
        status = "failed" if failed else "done"
        reason = failure_reason if failed else None
        if failed and reason is None:
            reason = classify_failure(exit_code, stagnated=stagnated,
                                      cause_tag=cause_tag)
        recorded = record_outcome(instance, mission_title, status,
                                  reason_category=reason,
                                  detail=failure_detail or (cause_tag or None))
        # A dropped authoritative write silently reverts the REST API to the
        # known-buggy absence-inference heuristic (#2285). Surface it distinctly
        # from a hard exception so a persistently failing outcome log is visible.
        if not recorded:
            log("error",
                f"Authoritative outcome write dropped for '{mission_title}' "
                f"(status={status}); GET /v1/missions falls back to inference")
    except sqlite3.DatabaseError as e:
        # Only the recoverable DB failure class degrades quietly. A programming
        # error (ImportError from a bad path, TypeError from signature drift)
        # must propagate so a broken outcome-recording path surfaces loudly
        # instead of permanently reverting the REST API to inference (#2285).
        log("error", f"Outcome recording DB error: {e}")
    try:
        from app.mission_history import record_execution
        record_execution(instance, mission_title, project_name, exit_code)
    except (OSError, ValueError) as e:
        log("error", f"Mission history recording error: {e}")
    return False


def _notify_stagnation(
    mission_title: str,
    project_name: str,
    pattern_type: str = "",
    pattern_excerpt: str = "",
) -> None:
    """Send a Telegram message announcing a stagnation abort."""
    try:
        from app.notify import NotificationPriority, send_telegram
        short_title = mission_title[:120]
        project_prefix = f"[{project_name}] " if project_name else ""
        cause = f" ({pattern_type})" if pattern_type else ""
        message = (
            f"🛑 {project_prefix}Mission stopped — Claude was stuck in a loop"
            f"{cause}. Marked as Failed in missions.md.\n\n"
            f"Mission: {short_title}"
        )
        if pattern_excerpt:
            message += f"\n\nContext: {pattern_excerpt[:200]}"
        send_telegram(message, priority=NotificationPriority.WARNING)
    except Exception as e:
        log("error", f"Stagnation notification failed: {e}")


def _notify_stagnation_retry(
    mission_title: str,
    project_name: str,
    attempt: int,
    max_attempts: int,
    pattern_type: str = "",
    pattern_excerpt: str = "",
) -> None:
    """Send a Telegram message announcing a stagnation-triggered requeue."""
    try:
        from app.notify import NotificationPriority, send_telegram
        short_title = mission_title[:120]
        project_prefix = f"[{project_name}] " if project_name else ""
        cause = f" ({pattern_type})" if pattern_type else ""
        message = (
            f"🔁 {project_prefix}Mission stagnated{cause} — "
            f"requeueing for retry {attempt}/{max_attempts}.\n\n"
            f"Mission: {short_title}"
        )
        if pattern_excerpt:
            message += f"\n\nContext: {pattern_excerpt[:200]}"
        send_telegram(message, priority=NotificationPriority.WARNING)
    except Exception as e:
        log("error", f"Stagnation retry notification failed: {e}")


def _notify_verify_requeue(
    mission_title: str,
    project_name: str,
    attempt: int,
    max_attempts: int,
    summary: str = "",
) -> None:
    """Send a Telegram message announcing a verification-failure requeue."""
    try:
        from app.notify import NotificationPriority, send_telegram
        prefix = f"[{project_name}] " if project_name else ""
        message = (
            f"🔁 {prefix}Verification failed — re-queueing for attempt "
            f"{attempt}/{max_attempts}.\n\nMission: {mission_title[:120]}"
        )
        if summary:
            message += f"\n\nWhat failed: {summary[:200]}"
        send_telegram(message, priority=NotificationPriority.WARNING)
    except Exception as e:
        log("error", f"Verify re-queue notification failed: {e}")


def _get_koan_branch(koan_root: str) -> str:
    """Get the current branch of the koan repository.

    Returns the branch name, or "" on error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=koan_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _restore_koan_branch(koan_root: str, expected_branch: str):
    """Restore the koan repo to the expected branch if it drifted.

    Skills like /rebase and /recreate do git checkouts on their
    project_path.  When project_path is the koan repo itself, a
    crash in the skill can leave the working tree on the wrong
    branch, breaking all subsequent module lookups.
    """
    if not expected_branch:
        return
    current = _get_koan_branch(koan_root)
    if current and current != expected_branch:
        from app.debug import debug_log
        debug_log(
            f"[run] koan branch drifted: {current} -> restoring {expected_branch}"
        )
        log("git", f"Restoring koan branch: {current} -> {expected_branch}")
        try:
            subprocess.run(
                ["git", "checkout", expected_branch],
                cwd=koan_root,
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            log("error", f"Failed to restore koan branch: {e}")


def _pump_skill_stdout(stdout, *, out_fh, pending_fh, tail, liveness=None):
    """Stream a skill subprocess's stdout to disk line-by-line.

    Each line is written to *out_fh* (the per-mission stdout capture that
    run_post_mission reads) and mirrored to *pending_fh* (pending.md, for
    ``/live``). Only a bounded *tail* deque is retained in RAM, so a verbose
    skill session cannot accumulate the full transcript in memory.

    Returns the still-open *pending_fh*, or ``None`` if a write error disabled
    the pending sink (out_fh writes continue regardless).
    """
    for line in stdout:
        if liveness is not None:
            liveness.heartbeat()
        stripped = line.rstrip("\n")
        tail.append(stripped)
        print(stripped)
        out_fh.write(f"{stripped}\n")
        if pending_fh is not None:
            try:
                pending_fh.write(f"{stripped}\n")
                pending_fh.flush()
            except OSError:
                pending_fh = None
    return pending_fh


def _read_back_or_tail(stdout_file, tail):
    """Read the on-disk transcript; fall back to the in-RAM tail on error."""
    try:
        return Path(stdout_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "\n".join(tail)


def _run_skill_mission(
    skill_cmd: list,
    koan_root: str,
    instance: str,
    project_name: str,
    project_path: str,
    run_num: int,
    mission_title: str,
    autonomous_mode: str,
    mission_tier: str = "",
) -> dict:
    """Execute a skill-dispatched mission directly via subprocess.

    Streams stdout/stderr line-by-line to pending.md so /live can show
    real-time progress during skill dispatch.

    Returns a dict with:
        exit_code (int): Process exit code (0 = success).
        stdout (str): Captured stdout text.
        stderr (str): Captured stderr text.
        quota_exhausted (bool): Whether quota exhaustion was detected in
            the post-mission pipeline.
        quota_info (tuple|None): (reset_display, resume_message) if exhausted.
    """
    from app.debug import debug_log

    global _last_mission_memory_cap, _last_mission_aborted
    _last_mission_memory_cap = ""
    # Per-mission state, reset the way run_claude_task resets it: this path
    # reads the flag at teardown to tell Kōan's own SIGKILL from the cap
    # firing, so an abort left over from a previous mission would otherwise
    # suppress this mission's cap verdict for the rest of the process.
    _last_mission_aborted = False

    mission_start = int(time.time())
    koan_pkg_dir = os.path.join(koan_root, "koan")
    pending_path = Path(instance) / "journal" / "pending.md"

    # Record the koan repo's HEAD before execution.  Skills like
    # /rebase and /recreate do git checkouts on project_path which
    # may be the koan repo itself — if they crash without restoring
    # the branch, subsequent runs break.
    koan_branch_before = _get_koan_branch(koan_root)

    from app.config import get_skill_timeout
    skill_timeout = get_skill_timeout()

    debug_log(f"[run] skill exec: cmd={' '.join(skill_cmd)}")
    debug_log(f"[run] skill exec: cwd={koan_pkg_dir} timeout={skill_timeout}s")
    tail = deque(maxlen=200)
    proc = None

    # Create temp files for post-mission processing up front.
    # stderr is redirected to a file instead of a pipe to eliminate
    # deadlock risk: if a background drain thread dies (e.g.
    # UnicodeDecodeError), the pipe fills and both processes stall.
    fd_out, stdout_file = tempfile.mkstemp(prefix="koan-out-", dir=koan_tmp_dir())
    os.close(fd_out)
    fd_err, stderr_file = tempfile.mkstemp(prefix="koan-err-", dir=koan_tmp_dir())
    os.close(fd_err)
    fd_usage, stream_usage_file = tempfile.mkstemp(prefix="koan-stream-usage-", dir=koan_tmp_dir())
    os.close(fd_usage)
    from app.skill_dispatch import mission_command_name, mission_model_key
    _mission_command = mission_command_name(mission_title)
    _mission_model_key = mission_model_key(_mission_command, instance)
    # Explicitly set PYTHONPATH so the subprocess can always resolve
    # app.* modules even if the working tree changes (e.g. skill does
    # a git checkout on the koan repo itself).
    skill_env = {
        **os.environ,
        "PYTHONPATH": koan_pkg_dir,
        "KOAN_STREAM_USAGE_FILE": stream_usage_file,
        "KOAN_MISSION_STARTED_AT": str(mission_start),
        "KOAN_MISSION_COMMAND": _mission_command,
    }
    if _mission_model_key:
        skill_env["KOAN_MISSION_MODEL_KEY"] = _mission_model_key
    # For PR-producing tracked skills (/review, /fix, /rebase, /implement) the
    # agent loop emits the canonical "✅ [project] 🔍 Reviewed <url>" completion
    # line after the runner finishes (see _notify_mission_end). In normal mode the
    # runner's own notify_outcome() line would advertise the same URL, producing a
    # duplicate row in chat (#2153). Signal the subprocess to log-only its outcome
    # line; debug mode keeps both for full verbosity. /plan is deliberately
    # excluded — its canonical line cannot carry the issue/Jira URL or inline
    # plan body, so suppressing the runner line would drop that content entirely.
    if _canonical_line_carries_url(mission_title) and not is_debug():
        skill_env["KOAN_SUPPRESS_RUNNER_OUTCOME"] = "1"
    stderr_fh = None
    out_fh = None
    scoped = None
    # True once one of the timeout/exception branches below has already killed
    # the process group, so teardown does not misread our own SIGKILL as the
    # memory cap firing.
    koan_killed = False
    try:
        stderr_fh = open(stderr_file, "w")
        # Same containment as the generic mission path: /review, /fix and
        # /implement drive real build tools, and a Gradle daemon started here
        # detaches to PPID 1 where no process group can reach it. The inner
        # provider spawn in provider/__init__.py deliberately shares this
        # runner's process group, so it inherits this cgroup too.
        def _spawn_skill(argv, launcher, **kwargs):
            # Spawn through this module's own subprocess binding so the scope
            # wrapping stays invisible to callers (and to the existing
            # @patch("app.run.subprocess.Popen") test targets).
            return subprocess.Popen(list(launcher) + list(argv), **kwargs)

        scoped = mission_scope.launch_scoped(
            skill_cmd,
            spawn=_spawn_skill,
            koan_root=koan_root or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_fh,
            cwd=koan_pkg_dir,
            env=skill_env,
            text=True,
        )
        proc = scoped.proc
        # Register for double-tap CTRL-C termination.
        _sig.claude_proc = proc

        from app.subprocess_runner import ProcessWatchdog, LivenessWatchdog

        watchdog = ProcessWatchdog(proc, skill_timeout).start()

        from app.config import get_first_output_timeout, get_rebase_first_output_timeout
        # Resolve the canonical command so the rebase override applies to all
        # dispatch paths: /rebase (GitHub), /core.rebase (Telegram), /rb alias.
        if _mission_command == "rebase":
            first_output_timeout = get_rebase_first_output_timeout()
        else:
            first_output_timeout = get_first_output_timeout()
        liveness = None
        if first_output_timeout > 0:
            liveness = LivenessWatchdog(
                proc, first_output_timeout,
                on_timeout=lambda: log(
                    "error",
                    f"No output for {first_output_timeout}s "
                    f"— killing stuck process (elapsed: {int(time.time() - mission_start)}s)",
                ),
            ).start()

        # Stream stdout line-by-line into stdout_file (the post-mission
        # source) and pending.md (/live), keeping only a bounded tail in RAM
        # so a verbose skill session cannot buffer the whole transcript.
        pending_fh = None
        try:
            pending_fh = open(pending_path, "a")
        except OSError as e:
            debug_log(f"[run] cannot open pending.md for streaming: {e}")
        out_fh = open(stdout_file, "w", encoding="utf-8")
        try:
            pending_fh = _pump_skill_stdout(
                proc.stdout, out_fh=out_fh, pending_fh=pending_fh,
                tail=tail, liveness=liveness,
            )
        finally:
            if pending_fh is not None:
                pending_fh.close()
            watchdog.cancel()
            if liveness is not None:
                liveness.cancel()

        proc.wait(timeout=30)
        if watchdog.fired or (liveness and liveness.fired):
            out_fh.close()
            out_fh = None
            raise subprocess.TimeoutExpired(skill_cmd, skill_timeout)
        exit_code = proc.returncode
        # Provider stream mode can persist token usage to a sidecar file.
        # Append that JSON payload to the on-disk capture so token_parser can
        # account for skill-dispatch sessions in run_post_mission.
        with suppress_logged(log, "warning", "Skill stream usage read failed", OSError, json.JSONDecodeError):
            raw_usage = Path(stream_usage_file).read_text().strip()
            if raw_usage:
                usage_payload = json.loads(raw_usage)
                if isinstance(usage_payload, dict):
                    usage_json = json.dumps(usage_payload, separators=(",", ":"))
                    out_fh.write(f"{usage_json}\n")
        out_fh.flush()
        out_fh.close()
        out_fh = None
        # Full transcript is genuinely needed by the caller (_extract_pr_url,
        # the "— skipping" check, error classification): read it back from
        # disk once instead of holding every line in RAM for the whole run.
        skill_stdout = _read_back_or_tail(stdout_file, tail)
        # Read stderr from file after process exits.
        stderr_fh.close()
        stderr_fh = None
        try:
            with open(stderr_file) as f:
                skill_stderr = f.read()
        except OSError:
            skill_stderr = ""
        if skill_stderr.strip():
            print(skill_stderr, file=sys.stderr)
        debug_log(
            f"[run] skill exec: exit_code={exit_code} "
            f"stdout_len={len(skill_stdout)} stderr_len={len(skill_stderr)}"
        )
        if exit_code != 0:
            if skill_stdout:
                debug_log(f"[run] skill stdout: {skill_stdout[:2000]}")
            if skill_stderr:
                debug_log(f"[run] skill stderr: {skill_stderr[:2000]}")
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        koan_killed = True
        liveness_fired = liveness and liveness.fired
        timeout_kind = "liveness" if liveness_fired else "watchdog"
        timeout_val = first_output_timeout if liveness_fired else skill_timeout
        log("error", f"Skill runner timed out ({timeout_kind}: {timeout_val}s)")
        debug_log(f"[run] skill exec: TIMEOUT ({timeout_kind}: {timeout_val}s)")
        # Log last lines of captured output so the journal shows *where*
        # the run stalled, not just that it timed out.
        tail_lines = list(tail)[-20:]
        if tail_lines:
            tail_preview = "\n".join(tail_lines)
            log("info", f"Last output before timeout:\n{tail_preview}")
            debug_log(f"[run] timeout tail ({len(tail_lines)} lines):\n{tail_preview}")
        else:
            log("info", "No stdout captured before timeout")
            debug_log("[run] timeout: no stdout lines captured")
        # Log stderr — may contain API errors that explain the hang
        with suppress_logged(log, "warning", "Timeout stderr read failed", OSError):
            _timeout_stderr = Path(stderr_file).read_text().strip()
            if _timeout_stderr:
                debug_log(f"[run] timeout stderr:\n{_timeout_stderr[:2000]}")
        exit_code = 1
        if out_fh is not None:
            with suppress_logged(log, "debug", "Skill stdout flush failed", OSError):
                out_fh.flush()
        skill_stdout = _read_back_or_tail(stdout_file, tail)
        skill_stderr = ""
    except Exception as e:
        if proc is not None:
            _kill_process_group(proc)
            koan_killed = True
        log("error", f"Skill runner failed: {e}\n{traceback.format_exc()}")
        debug_log(f"[run] skill exec: EXCEPTION {e}")
        exit_code = 1
        if out_fh is not None:
            with suppress_logged(log, "debug", "Skill stdout flush failed", OSError):
                out_fh.flush()
        skill_stdout = _read_back_or_tail(stdout_file, tail)
        skill_stderr = ""
    finally:
        # Tear the mission's cgroup down before anything else in this block: the
        # branch restore below runs git on the same worktree the skill was using,
        # so no leaked build daemon should still be holding it.
        if scoped is not None:
            # A double-tap CTRL-C SIGKILLs this process group from _on_sigint,
            # which records it as an abort — the only trace of a kill Kōan
            # delivered on that path, and it must not read as the cap firing.
            koan_killed = koan_killed or _last_mission_aborted
            try:
                scoped.teardown(koan_initiated_kill=koan_killed)
            except Exception as e:
                log("error", f"mission scope teardown failed: {e}")
            if scoped.cap_exceeded:
                _last_mission_memory_cap = scoped.cap_message()
                log("error", f"Skill mission {_last_mission_memory_cap}")
        if out_fh is not None:
            with suppress_logged(log, "debug", "Skill stdout file close failed", OSError):
                out_fh.close()
        if proc is not None and proc.stdout is not None:
            with suppress_logged(log, "debug", "Skill proc stdout close failed", OSError):
                proc.stdout.close()
        if stderr_fh is not None:
            stderr_fh.close()
        _sig.claude_proc = None
        _reset_terminal()
        # Restore koan repo branch if it was changed by the skill.
        _restore_koan_branch(koan_root, koan_branch_before)

    # Write stdout to its temp file for post-mission processing.
    # stderr is already in stderr_file from the subprocess redirect.
    # Wrap in try/finally so temp files are cleaned up even if the write
    # or post-mission processing raises an unexpected exception (consistent
    # with the contemplative and regular mission paths).
    skill_result = {
        "exit_code": exit_code,
        "stdout": skill_stdout,
        "stderr": skill_stderr,
        "quota_exhausted": False,
        "quota_info": None,
    }
    try:
        # stdout_file is already populated by _pump_skill_stdout (plus the
        # usage-JSON line); no re-write from the in-memory string is needed.
        _skill_prefix = f"Run {run_num}"
        set_status(koan_root, f"{_skill_prefix} — finalizing")
        from app.mission_runner import run_post_mission
        _skill_provider_name, _ = _provider_identity()
        post_result = run_post_mission(
            instance_dir=instance,
            project_name=project_name,
            project_path=project_path,
            run_num=run_num,
            exit_code=exit_code,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            mission_title=mission_title,
            autonomous_mode=autonomous_mode or "implement",
            start_time=mission_start,
            status_callback=lambda step: set_status(
                koan_root, f"{_skill_prefix} — {step}"
            ),
            mission_tier=mission_tier,
            provider_name=_skill_provider_name,
            is_skill_dispatch=True,
            # This mission's own cap result, set in the finally above.
            memory_cap_detail=_last_mission_memory_cap,
        )
        if isinstance(post_result, dict) and post_result.get("quota_exhausted"):
            skill_result["quota_exhausted"] = True
            skill_result["quota_info"] = post_result.get("quota_info")
    except Exception as e:
        log("error", f"Post-mission error: {e}")
    finally:
        _cleanup_temp(stdout_file, stderr_file, stream_usage_file)
    duration = int(time.time()) - mission_start
    debug_log(f"[run] skill exec: done in {duration}s, exit_code={exit_code}")
    return skill_result


def _cleanup_temp(*files):
    """Remove temporary files."""
    for f in files:
        with suppress_logged(log, "debug", f"Temp file cleanup failed ({f})", OSError):
            Path(f).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Entry point with restart wrapper
# ---------------------------------------------------------------------------

def main():
    """Entry point with restart wrapper (replaces bash outer loop).

    Handles four exit modes:
    - Normal exit (break)
    - CTRL-C (KeyboardInterrupt → break)
    - Restart signal (SystemExit(42) → restart)
    - Unexpected crash (Exception → restart with backoff)
    """
    crash_count = 0
    while True:
        try:
            main_loop()
            break  # Normal exit
        except KeyboardInterrupt:
            break
        except SystemExit as e:
            if e.code == RESTART_EXIT_CODE:
                # Restart signal — re-exec the interpreter so updated code on
                # disk is actually loaded. A plain in-process `continue` re-runs
                # the loop with the STALE modules already imported into this
                # long-lived process, so `/update` and auto-update would pull
                # new code to disk yet keep executing the old code in memory
                # until a full manual restart. main_loop()'s finally has already
                # released the pidfile by the time we get here, so the re-exec'd
                # image re-acquires it cleanly; cwd, env and the stdout/stderr
                # log fds are preserved across execv.
                crash_count = 0
                print("[koan] Restarting (re-exec to load updated code)...")
                _reset_terminal()
                sys.stdout.flush()
                sys.stderr.flush()
                try:
                    os.execv(sys.executable, [sys.executable, *sys.argv])
                except OSError as exc:
                    # Re-exec failed — fall back to an in-process restart so the
                    # daemon stays alive (it just won't pick up updated code).
                    print(
                        f"[koan] Re-exec failed ({exc}); restarting in-process "
                        "without reloading code.",
                        file=sys.stderr,
                    )
                    time.sleep(1)
                    continue
            raise
        except Exception:
            crash_count += 1
            tb = traceback.format_exc()
            cfg = get_recovery_config()
            max_crashes = cfg["max_main_crashes"]
            print(f"[koan] Unexpected crash ({crash_count}/{max_crashes}): {tb}", file=sys.stderr)

            if crash_count >= max_crashes:
                print(f"[koan] Too many crashes ({max_crashes}). Giving up.", file=sys.stderr)
                _reset_terminal()
                sys.exit(1)

            backoff = _calculate_backoff(crash_count, cfg["max_backoff_main"])
            print(f"[koan] Restarting in {backoff}s...", file=sys.stderr)
            time.sleep(backoff)

    _reset_terminal()


if __name__ == "__main__":
    main()
