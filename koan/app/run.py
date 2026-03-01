"""Kōan — Main agent loop.

Manages the agent loop: mission picking, Claude CLI execution,
post-mission processing, pause/resume, signal handling, and
lifecycle notifications.

Usage:
    python -m app.run              # Normal start
    python -m app.run --restart    # Re-exec after restart signal (exit 42)

Features:
- Double-tap CTRL-C protection across ALL phases (missions, rituals,
  sleep, startup, git sync). First press shows warning with current
  activity name; second press within 10s aborts.
- Automatic exception recovery with backoff (survives crashes)
- protected_phase() context manager for easy phase protection
- Restart wrapper (exit code 42 → re-exec)
- Process group isolation for Claude subprocess (SIGINT ignored)
- Colored log output with TTY detection
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from app.iteration_manager import plan_iteration
from app.loop_manager import check_pending_missions, interruptible_sleep
from app.pid_manager import acquire_pidfile, release_pidfile
from app.restart_manager import check_restart, clear_restart, RESTART_EXIT_CODE
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
)
from app.shutdown_manager import is_shutdown_requested, clear_shutdown
from app.utils import atomic_write


# ---------------------------------------------------------------------------
# Recovery configuration
# ---------------------------------------------------------------------------

# Maximum consecutive iteration errors before entering pause mode.
MAX_CONSECUTIVE_ERRORS = 10

# Maximum crashes in main() before giving up.
MAX_MAIN_CRASHES = 5

# Backoff parameters (in seconds).
BACKOFF_MULTIPLIER = 10
MAX_BACKOFF_MAIN = 60
MAX_BACKOFF_ITERATION = 300

# Notification throttling: notify on first error, then every N errors.
ERROR_NOTIFICATION_INTERVAL = 5


# ---------------------------------------------------------------------------
# Recovery helpers
# ---------------------------------------------------------------------------

def _calculate_backoff(attempt: int, max_backoff: int) -> int:
    """Calculate linear backoff capped at max_backoff.

    Returns: attempt * BACKOFF_MULTIPLIER, capped at max_backoff.
    """
    return min(BACKOFF_MULTIPLIER * attempt, max_backoff)


def _should_notify_error(attempt: int) -> bool:
    """Determine if error notification should be sent.

    Notifies on first error and every ERROR_NOTIFICATION_INTERVAL errors.
    """
    return attempt == 1 or attempt % ERROR_NOTIFICATION_INTERVAL == 0


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------

def set_status(koan_root: str, message: str):
    """Write loop status for /status and dashboard."""
    try:
        atomic_write(Path(koan_root, ".koan-status"), message)
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

    if not Path(koan_root, ".koan-pause").exists():
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
    """Terminate a subprocess and its entire process group.

    When a subprocess is started with ``start_new_session=True``, it becomes
    the leader of a new process group.  A simple ``proc.terminate()`` only
    sends SIGTERM to the leader — children survive.  This helper sends
    SIGTERM to the whole group, then SIGKILL if still alive after 3 s.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Process didn't die even after SIGKILL — give up to
                # avoid blocking the caller.  The OS will reap the
                # zombie eventually.
                print(
                    f"[run] warning: pid {proc.pid} did not exit after SIGKILL",
                    file=sys.stderr,
                )
    except (ProcessLookupError, PermissionError, OSError):
        # Process already gone or we lack permissions — nothing to do.
        pass


def _on_sigint(signum, frame):
    """SIGINT handler: first press warns, second press aborts."""
    if not _sig.task_running:
        raise KeyboardInterrupt

    now = time.time()
    if _sig.first_ctrl_c > 0:
        elapsed = now - _sig.first_ctrl_c
        if elapsed <= _sig.timeout:
            # Second CTRL-C within timeout — abort
            print()
            log("koan", "Confirmed. Aborting...")
            _kill_process_group(_sig.claude_proc)
            _sig.first_ctrl_c = 0
            _sig.task_running = False
            raise KeyboardInterrupt

    # First CTRL-C (or timeout expired)
    _sig.first_ctrl_c = now
    print()
    phase_hint = f" ({_sig.phase})" if _sig.phase else ""
    log("koan", f"⚠️  Press CTRL-C again within {_sig.timeout}s to abort.{phase_hint}")


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
) -> int:
    """Run Claude CLI as a subprocess with SIGINT isolation.

    The child process ignores SIGINT (via preexec_fn) so the double-tap
    pattern works: first CTRL-C only warns the user, second kills the child.

    When *instance_dir* and *project_name* are provided and
    ``cli_output_journal`` is enabled, stdout is streamed to the project's
    daily journal file in real-time via a background tail thread.

    Returns the child exit code.
    """
    _sig.task_running = True
    _sig.first_ctrl_c = 0

    # Start journal streaming if configured
    journal_stream = None
    if instance_dir and project_name:
        from app.cli_journal_streamer import start_journal_stream
        journal_stream = start_journal_stream(
            stdout_file, instance_dir, project_name, run_num,
        )

    from app.cli_exec import popen_cli

    exit_code = 1  # default if subprocess never completes
    try:
        with open(stdout_file, "w") as out_f, open(stderr_file, "w") as err_f:
            proc, cleanup = popen_cli(
                cmd,
                stdout=out_f,
                stderr=err_f,
                cwd=cwd,
                start_new_session=True,
            )
            _sig.claude_proc = proc

            try:
                # Wait for child, handling SIGINT interruptions gracefully
                while True:
                    try:
                        proc.wait()
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
                cleanup()

        exit_code = proc.returncode
    finally:
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

    Returns list of (name, path) tuples. Exits on error.
    """
    from app.utils import get_known_projects
    projects = get_known_projects()

    if not projects:
        log("error", "No projects configured. Create projects.yaml or set KOAN_PROJECTS env var.")
        sys.exit(1)

    if len(projects) > 50:
        log("error", f"Max 50 projects allowed. You have {len(projects)}.")
        sys.exit(1)

    for name, path in projects:
        if not Path(path).is_dir():
            log("error", f"Project '{name}' path does not exist: {path}")
            sys.exit(1)

    return projects


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


def _notify_mission_end(
    instance: str,
    project_name: str,
    run_num: int,
    max_runs: int,
    exit_code: int,
    mission_title: str = "",
):
    """Send a notification when a mission or autonomous run completes.

    Always sends — both on success and failure — so the human always
    gets a status update. Uses unicode prefix: ✅ for success, ❌ for failure.
    On success, appends a brief journal summary when available.
    """
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
        prefix = "❌"
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
# Helpers
# ---------------------------------------------------------------------------


def _read_current_project(koan_root: str) -> str:
    """Read the current project name from .koan-project, safely.

    Returns the project name or "unknown" if the file cannot be read
    (missing, locked, or corrupt).
    """
    try:
        return Path(koan_root, ".koan-project").read_text().strip() or "unknown"
    except (OSError, ValueError):
        return "unknown"


# ---------------------------------------------------------------------------
# Instance commit helper
# ---------------------------------------------------------------------------

def _commit_instance(instance: str, message: str = ""):
    """Commit instance changes and push."""
    if not message:
        message = f"koan: {time.strftime('%Y-%m-%d-%H:%M')}"
    try:
        add_result = subprocess.run(
            ["git", "add", "-A"], cwd=instance, capture_output=True, timeout=10,
        )
        if add_result.returncode != 0:
            log("error", f"git add failed (rc={add_result.returncode}): "
                f"{add_result.stderr.decode(errors='replace')[:200]}")
            return

        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=instance, capture_output=True, timeout=10,
        )
        if diff.returncode == 0:
            return  # Nothing staged

        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=instance, capture_output=True, timeout=30,
        )
        if commit_result.returncode != 0:
            log("error", f"git commit failed (rc={commit_result.returncode}): "
                f"{commit_result.stderr.decode(errors='replace')[:200]}")
            return

        # Detect the current branch instead of assuming "main"
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=instance, capture_output=True, timeout=5,
        )
        branch = branch_result.stdout.decode().strip() if branch_result.returncode == 0 else ""
        if not branch or branch == "HEAD":
            log("error", "Skipping push: detached HEAD or unknown branch")
            return

        push_result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=instance, capture_output=True, timeout=30,
        )
        if push_result.returncode != 0:
            log("error", f"git push failed (rc={push_result.returncode}): "
                f"{push_result.stderr.decode(errors='replace')[:200]}")
    except Exception as e:
        log("error", f"Instance commit/push failed: {e}")


# ---------------------------------------------------------------------------
# Pause mode handler
# ---------------------------------------------------------------------------

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
    if not Path(koan_root, ".koan-pause").exists():
        log("pause", "Manual resume detected")
        _reset_usage_session(instance)
        return "resume"

    # Sleep 5 min in 5s increments — check for resume/stop/restart/shutdown
    with protected_phase("Paused — waiting for resume"):
        for _ in range(60):
            if not Path(koan_root, ".koan-pause").exists():
                return "resume"
            if Path(koan_root, ".koan-stop").exists():
                log("pause", "Stop signal detected while paused")
                break
            if Path(koan_root, ".koan-shutdown").exists():
                log("pause", "Shutdown signal detected while paused")
                break
            if check_restart(koan_root):
                break
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

    # Record startup time
    start_time = time.time()

    # Acquire PID (flock-based exclusive lock)
    pidfile_lock = acquire_pidfile(Path(koan_root), "run")

    # Clear stale signal files from a previous session.
    # If `make stop` or `/stop` ran while run.py was NOT running, the signal
    # file persists and would cause an immediate exit on next startup.
    Path(koan_root, ".koan-stop").unlink(missing_ok=True)
    Path(koan_root, ".koan-shutdown").unlink(missing_ok=True)
    clear_restart(koan_root)

    # Install SIGINT handler
    signal.signal(signal.SIGINT, _on_sigint)

    # Initialize project state
    if projects:
        atomic_write(Path(koan_root, ".koan-project"), projects[0][0])
        os.environ["KOAN_CURRENT_PROJECT"] = projects[0][0]
        os.environ["KOAN_CURRENT_PROJECT_PATH"] = projects[0][1]

    count = 0
    consecutive_errors = 0
    try:
        # Startup sequence
        max_runs, interval, branch_prefix = run_startup(koan_root, instance, projects)

        git_sync_interval = int(os.environ.get("KOAN_GIT_SYNC_INTERVAL", "5"))

        while True:
            # --- Stop check ---
            stop_file = Path(koan_root, ".koan-stop")
            if stop_file.exists():
                log("koan", "Stop requested.")
                stop_file.unlink(missing_ok=True)
                current = _read_current_project(koan_root)
                _notify(instance, f"Kōan stopped on request after {count} runs. Last project: {current}.")
                break

            # --- Shutdown check (stops both agent loop and bridge) ---
            if is_shutdown_requested(koan_root, start_time):
                log("koan", "Shutdown requested. Exiting.")
                clear_shutdown(koan_root)
                current = _read_current_project(koan_root)
                _notify(instance, f"Kōan shutdown after {count} runs. Last project: {current}.")
                break

            # --- Restart check ---
            if check_restart(koan_root, since=start_time):
                log("koan", "Restart requested. Exiting for re-launch...")
                clear_restart(koan_root)
                sys.exit(RESTART_EXIT_CODE)

            # --- Pause mode ---
            if Path(koan_root, ".koan-pause").exists():
                result = handle_pause(koan_root, instance, max_runs)
                if result == "resume":
                    count = 0
                    consecutive_errors = 0
                continue

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
                if productive:
                    count += 1
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
        # Cleanup
        Path(koan_root, ".koan-status").unlink(missing_ok=True)
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
    with protected_phase("Sleeping between runs"):
        wake = interruptible_sleep(interval, koan_root, instance)
    if wake == "mission":
        log("koan", "New mission detected during sleep — waking up early")
        if run_num:
            set_status(koan_root, f"Run {run_num}/{max_runs} — done, new mission detected")


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
    try:
        from app.contemplative_runner import build_contemplative_command
        cmd = build_contemplative_command(
            instance=instance,
            project_name=project_name,
            session_info=f"Run {run_num}/{max_runs} on {project_name}. Mode: {plan['autonomous_mode']}.",
        )
        fd_out, stdout_file = tempfile.mkstemp(prefix="koan-contemp-out-")
        os.close(fd_out)
        fd_err, stderr_file = tempfile.mkstemp(prefix="koan-contemp-err-")
        os.close(fd_err)
        try:
            run_claude_task(
                cmd, stdout_file, stderr_file, cwd=koan_root,
                instance_dir=instance, project_name=project_name, run_num=run_num,
            )
        finally:
            _cleanup_temp(stdout_file, stderr_file)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log("error", f"Contemplative error: {e}")
    log("pause", "Contemplative session ended.")

    if check_pending_missions(instance):
        log("koan", "Pending missions found after contemplation — skipping sleep")
    else:
        set_status(koan_root, f"Idle — post-contemplation sleep ({time.strftime('%H:%M')})")
        log("pause", f"Contemplative session complete. Sleeping {interval}s...")
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
    project_name = plan["project_name"]
    log("quota", "Decision: WAIT mode (budget exhausted)")
    print(f"  Reason: {plan['decision_reason']}")
    print("  Action: Entering pause mode (will auto-resume when quota resets)")
    print()
    try:
        from app.send_retrospective import create_retrospective
        create_retrospective(Path(instance), project_name)
    except Exception as e:
        log("error", f"Retrospective sending failed: {e}")

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


def _handle_skill_dispatch(
    mission_title: str,
    project_name: str,
    project_path: str,
    koan_root: str,
    instance: str,
    run_num: int,
    max_runs: int,
    autonomous_mode: str,
    interval: int,
) -> tuple:
    """Try to dispatch a mission as a skill command.

    Returns:
        (handled: bool, mission_title: str) — if handled is True the caller
        should return immediately; if False the caller should proceed to Claude
        using the returned mission_title (which may have been translated by a
        cli_skill mapping).
    """
    from app.debug import debug_log as _debug_log
    preview = f"{mission_title[:100]}..." if len(mission_title) > 100 else mission_title
    _debug_log(f"[run] checking skill dispatch for: {preview}")

    from app.skill_dispatch import dispatch_skill_mission, is_skill_mission
    skill_cmd = dispatch_skill_mission(
        mission_text=mission_title,
        project_name=project_name,
        project_path=project_path,
        koan_root=koan_root,
        instance_dir=instance,
    )
    if skill_cmd:
        _debug_log(f"[run] skill dispatch matched: {' '.join(skill_cmd[:5])}")
        log("mission", "Decision: SKILL DISPATCH (direct runner)")
        print(f"  Mission: {mission_title}")
        print(f"  Project: {project_name}")
        print(f"  Runner: {' '.join(skill_cmd[:4])}...")
        print()
        set_status(koan_root, f"Run {run_num}/{max_runs} — skill dispatch on {project_name}")
        _notify(instance, f"🚀 [{project_name}] Run {run_num}/{max_runs} — Skill: {mission_title}")

        # Create pending.md so /live can show progress during skill dispatch
        from app.loop_manager import create_pending_file
        try:
            create_pending_file(
                instance_dir=instance,
                project_name=project_name,
                run_num=run_num,
                max_runs=max_runs,
                autonomous_mode=autonomous_mode or "implement",
                mission_title=mission_title,
            )
        except Exception as e:
            log("error", f"Failed to create pending.md for skill dispatch: {e}")

        exit_code = 1
        try:
            with protected_phase(f"Skill: {mission_title[:50]}"):
                exit_code = _run_skill_mission(
                    skill_cmd=skill_cmd,
                    koan_root=koan_root,
                    instance=instance,
                    project_name=project_name,
                    project_path=project_path,
                    run_num=run_num,
                    mission_title=mission_title,
                    autonomous_mode=autonomous_mode,
                )
            if exit_code == 0:
                log("mission", f"Run {run_num}/{max_runs} — [{project_name}] skill completed")
        except KeyboardInterrupt:
            log("error", "Skill dispatch interrupted by user")
            _finalize_mission(instance, mission_title, project_name, 1)
            raise
        except Exception as e:
            log("error", f"Skill dispatch exception: {e}")

        _notify_mission_end(
            instance, project_name, run_num, max_runs,
            exit_code, mission_title,
        )
        _finalize_mission(instance, mission_title, project_name, exit_code)
        _commit_instance(instance)

        _sleep_between_runs(koan_root, instance, interval)
        return True, mission_title

    # Check for cli_skill translation before failing unrecognized /commands
    if is_skill_mission(mission_title):
        from pathlib import Path as _Path
        from app.skill_dispatch import translate_cli_skill_mission
        translated = translate_cli_skill_mission(
            mission_text=mission_title,
            koan_root=_Path(koan_root),
            instance_dir=_Path(instance),
        )
        if translated is not None:
            _debug_log(
                f"[run] cli_skill translation: '{mission_title[:80]}' -> '{translated[:80]}'"
            )
            log("mission", "Decision: CLI SKILL (provider slash command)")
            # Return untranslated=False so caller falls through to Claude with translated title
            return False, translated

        _debug_log(f"[run] skill mission unhandled, failing: {mission_title[:200]}")

        # Differentiate "unknown command" from "known command, bad arguments"
        from app.skill_dispatch import parse_skill_mission, validate_skill_args
        _, cmd_name, cmd_args = parse_skill_mission(mission_title)
        arg_error = validate_skill_args(cmd_name, cmd_args) if cmd_name else None
        if arg_error:
            log("warning", f"Skill mission invalid args: {arg_error}")
            _notify(instance, f"⚠️ [{project_name}] {arg_error}")
        else:
            log("warning", f"Skill mission has no runner, failing: {mission_title[:80]}")
            _notify(instance, f"⚠️ [{project_name}] Unknown skill command: {mission_title[:80]}")
        _finalize_mission(instance, mission_title, project_name, exit_code=1)
        _commit_instance(instance)
        return True, mission_title

    return False, mission_title


# ---------------------------------------------------------------------------
# Iteration body (extracted for exception isolation)
# ---------------------------------------------------------------------------

def _run_iteration(
    koan_root: str,
    instance: str,
    projects: list,
    count: int,
    max_runs: int,
    interval: int,
    git_sync_interval: int,
):
    """Execute a single iteration of the main loop.

    Called from main_loop() within a try/except block that catches
    unexpected exceptions without killing the process.

    Returns:
        True if this was a productive iteration (mission, autonomous, or
        contemplative session that consumed API budget).  False for idle
        iterations (wait states, errors, dedup skips, preflight failures).
        The caller only increments ``count`` on productive iterations so
        that ``max_runs`` reflects actual work done, not loop cycles.

    Exceptions:
        KeyboardInterrupt: Propagates to caller (user abort)
        SystemExit: Propagates to caller (restart signal)
        Exception: Caught by caller for recovery
    """
    run_num = count + 1
    set_status(koan_root, f"Run {run_num}/{max_runs} — preparing")
    print()
    print(bold_cyan(f"=== Run {run_num}/{max_runs} — {time.strftime('%Y-%m-%d %H:%M:%S')} ==="))

    # Refresh project list (picks up workspace changes since startup)
    from app.utils import get_known_projects
    refreshed = get_known_projects()
    if refreshed:
        projects = refreshed

    # Check GitHub notifications before planning (converts @mentions to missions
    # so plan_iteration() sees them immediately instead of waiting for sleep)
    from app.loop_manager import process_github_notifications
    try:
        gh_missions = process_github_notifications(koan_root, instance)
        if gh_missions > 0:
            log("github", f"Pre-iteration: {gh_missions} mission(s) created from GitHub notifications")
    except Exception as e:
        log("error", f"Pre-iteration GitHub notification check failed: {e}")

    # Plan iteration (delegated to iteration_manager)
    last_project = _read_current_project(koan_root)
    plan = plan_iteration(
        instance_dir=instance,
        koan_root=koan_root,
        run_num=run_num,
        count=count,
        projects=projects,
        last_project=last_project,
    )

    # --- Iteration decision summary (always visible in logs) ---
    log("koan", f"Iteration plan: action={plan['action']}, "
        f"project={plan['project_name']}, mode={plan['autonomous_mode']}, "
        f"budget={plan['available_pct']}%"
        f"{', mission=' + plan['mission_title'][:60] if plan['mission_title'] else ''}")
    if plan.get("error"):
        log("error", f"Iteration plan error: {plan['error']}")

    # Display usage
    log("quota", "Usage Status:")
    if plan["display_lines"]:
        for line in plan["display_lines"]:
            print(f"  {line}")
    else:
        print("  [No usage data available - using fallback mode]")
    print(f"  Safety margin: 10% → Available: {plan['available_pct']}%")
    print()

    # Log recurring injections
    for line in plan.get("recurring_injected", []):
        log("mission", line)

    # --- Handle special actions ---
    action = plan["action"]
    project_name = plan["project_name"]
    project_path = plan["project_path"]

    if action == "error":
        error_msg = plan.get("error", "Unknown error")
        mission_title = plan.get("mission_title", "")
        log("error", error_msg)
        # Move the mission to Failed so it doesn't block the queue.
        # Without this, the same mission gets picked every iteration,
        # causing a retry loop until MAX_CONSECUTIVE_ERRORS triggers pause.
        if mission_title:
            _update_mission_in_file(instance, mission_title, failed=True)
            _notify(instance, f"❌ Mission failed: {error_msg}")
            _commit_instance(instance)
        else:
            _notify(instance, f"⚠️ Iteration error: {error_msg}")
        return False  # error handling — not productive

    if action == "contemplative":
        _handle_contemplative(plan, run_num, max_runs, koan_root, instance, interval)
        return True  # contemplative sessions consume API budget

    # Idle wait actions — all follow the same sleep-and-check pattern
    _IDLE_WAIT_CONFIG = {
        "focus_wait": lambda p: (
            f"Focus mode active ({p.get('focus_remaining', 'unknown')} remaining) — no missions pending, sleeping",
            f"Focus mode — waiting for missions ({p.get('focus_remaining', 'unknown')} remaining)",
        ),
        "schedule_wait": lambda _: (
            "Work hours active — waiting for missions (exploration suppressed)",
            f"Work hours — waiting for missions ({time.strftime('%H:%M')})",
        ),
        "exploration_wait": lambda _: (
            "All projects have exploration disabled — waiting for missions",
            f"Exploration disabled — waiting for missions ({time.strftime('%H:%M')})",
        ),
        "pr_limit_wait": lambda _: (
            "PR limit reached for all projects — waiting for reviews",
            f"PR limit reached — waiting for reviews ({time.strftime('%H:%M')})",
        ),
    }
    if action in _IDLE_WAIT_CONFIG:
        log_msg, status_msg = _IDLE_WAIT_CONFIG[action](plan)
        log("koan", log_msg)
        set_status(koan_root, status_msg)
        with protected_phase(status_msg):
            wake = interruptible_sleep(interval, koan_root, instance)
        if wake == "mission":
            log("koan", f"New mission detected during {action} — waking up")
        return False  # idle wait — not productive

    if action == "wait_pause":
        _handle_wait_pause(plan, count, koan_root, instance)
        return False  # budget exhausted — not productive

    # --- Pre-flight quota check ---
    if action in ("mission", "autonomous"):
        if _run_preflight_check(plan, koan_root, instance, count):
            return False  # quota exhausted pre-flight — not productive

    # --- Execute mission or autonomous run ---
    mission_title = plan["mission_title"]
    autonomous_mode = plan["autonomous_mode"]
    focus_area = plan["focus_area"]
    available_pct = plan["available_pct"]

    # --- Dedup guard ---
    if mission_title:
        try:
            from app.mission_history import should_skip_mission
            if should_skip_mission(instance, mission_title, max_executions=3):
                log("mission", f"Skipping repeated mission (3+ attempts): {mission_title[:60]}")
                _update_mission_in_file(instance, mission_title, failed=True)
                _notify(instance, f"⚠️ Mission failed 3+ times, moved to Failed: {mission_title[:60]}")
                _commit_instance(instance)
                return False  # dedup skip — not productive
        except (OSError, ValueError) as e:
            log("error", f"Dedup guard error: {e}")

    # Set project state
    atomic_write(Path(koan_root, ".koan-project"), project_name)
    os.environ["KOAN_CURRENT_PROJECT"] = project_name
    os.environ["KOAN_CURRENT_PROJECT_PATH"] = project_path

    print(bold_green(f">>> Current project: {project_name}") + f" ({project_path})")
    print()

    # --- Prepare project git state ---
    from app.git_prep import prepare_project_branch
    try:
        prep = prepare_project_branch(project_path, project_name, koan_root)
        if prep.stashed:
            log("git", f"Stashed uncommitted changes in {project_name}")
        if not prep.success:
            log("error", f"Git prep failed for {project_name}: {prep.error}")
        else:
            log("git", f"Ready on {prep.base_branch} from {prep.remote_used}")
    except Exception as e:
        log("error", f"Git prep error for {project_name}: {e}")

    # --- Mark mission as In Progress ---
    # Save the original title before skill dispatch may translate it.
    # _finalize_mission must use the original title because that's the
    # needle recorded in missions.md "In Progress" section.
    original_mission_title = mission_title
    if mission_title:
        _start_mission_in_file(instance, mission_title)

    # --- Check for skill-dispatched mission ---
    if mission_title:
        handled, mission_title = _handle_skill_dispatch(
            mission_title, project_name, project_path, koan_root,
            instance, run_num, max_runs, autonomous_mode, interval,
        )
        if handled:
            return True  # skill dispatch — productive

    # Lifecycle notification
    if mission_title:
        log("mission", "Decision: MISSION mode (assigned)")
        print(f"  Mission: {mission_title}")
        print(f"  Project: {project_name}")
        print()
        _notify(instance, f"🚀 [{project_name}] Run {run_num}/{max_runs} — Mission taken: {mission_title}")
    else:
        mode_upper = autonomous_mode.upper()
        log("mission", f"Decision: {mode_upper} mode (estimated cost: 5.0% session)")
        print(f"  Reason: {plan['decision_reason']}")
        print(f"  Project: {project_name}")
        print(f"  Focus: {focus_area}")
        print()
        _notify(instance, f"🚀 [{project_name}] Run {run_num}/{max_runs} — Autonomous: {autonomous_mode} mode")

    # Build prompt
    from app.prompt_builder import build_agent_prompt
    prompt = build_agent_prompt(
        instance=instance,
        project_name=project_name,
        project_path=project_path,
        run_num=run_num,
        max_runs=max_runs,
        autonomous_mode=autonomous_mode or "implement",
        focus_area=focus_area or "General autonomous work",
        available_pct=available_pct or 50,
        mission_title=mission_title,
    )

    # Create pending.md
    from app.loop_manager import create_pending_file
    try:
        create_pending_file(
            instance_dir=instance,
            project_name=project_name,
            run_num=run_num,
            max_runs=max_runs,
            autonomous_mode=autonomous_mode or "implement",
            mission_title=mission_title,
        )
    except Exception as e:
        log("error", f"Failed to create pending.md: {e}")

    # Execute Claude
    if mission_title:
        set_status(koan_root, f"Run {run_num}/{max_runs} — executing mission on {project_name}")
    else:
        set_status(koan_root, f"Run {run_num}/{max_runs} — {autonomous_mode.upper()} on {project_name}")

    mission_start = int(time.time())
    fd_out, stdout_file = tempfile.mkstemp(prefix="koan-out-")
    os.close(fd_out)
    fd_err, stderr_file = tempfile.mkstemp(prefix="koan-err-")
    os.close(fd_err)
    claude_exit = 1  # default to failure; overwritten on successful execution
    try:
        # Build CLI command (provider-agnostic with per-project overrides)
        from app.mission_runner import build_mission_command
        from app.debug import debug_log as _debug_log
        cmd = build_mission_command(
            prompt=prompt,
            autonomous_mode=autonomous_mode,
            extra_flags="",
            project_name=project_name,
        )

        cmd_display = [c[:100] + '...' if len(c) > 100 else c for c in cmd[:6]]
        _debug_log(f"[run] cli: cmd={' '.join(cmd_display)}... cwd={project_path}")
        claude_exit = run_claude_task(
            cmd, stdout_file, stderr_file, cwd=project_path,
            instance_dir=instance, project_name=project_name, run_num=run_num,
        )
        _debug_log(f"[run] cli: exit_code={claude_exit}")

        # Parse and display output
        try:
            from app.mission_runner import parse_claude_output
            with open(stdout_file) as f:
                raw = f.read()
            text = parse_claude_output(raw)
            print(text)
        except Exception as e:
            try:
                with open(stdout_file) as f:
                    print(f.read())
            except Exception as e2:
                log("error", f"Failed to read CLI output: {e}, {e2}")
        _reset_terminal()

        # Complete/fail mission in missions.md (safety net — idempotent if Claude already did it)
        # Done BEFORE post-mission pipeline so quota exhaustion can't skip it.
        # Use original_mission_title because that's the needle in "In Progress".
        # cli_skill translation may have changed mission_title to a different string.
        if original_mission_title:
            _finalize_mission(instance, original_mission_title, project_name, claude_exit)

        # Post-mission pipeline
        set_status(koan_root, f"Run {run_num}/{max_runs} — post-mission processing")
        try:
            from app.mission_runner import run_post_mission
            post_result = run_post_mission(
                instance_dir=instance,
                project_name=project_name,
                project_path=project_path,
                run_num=run_num,
                exit_code=claude_exit,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
                mission_title=mission_title,
                autonomous_mode=autonomous_mode or "implement",
                start_time=mission_start,
            )

            if post_result.get("pending_archived"):
                log("health", "pending.md archived to journal (Claude didn't clean up)")
            if post_result.get("auto_merge_branch"):
                log("git", f"Auto-merge checked for {post_result['auto_merge_branch']}")

            if post_result.get("quota_exhausted"):
                # quota_info is a (reset_display, resume_message) tuple
                quota_info = post_result.get("quota_info")
                if quota_info and isinstance(quota_info, (list, tuple)) and len(quota_info) >= 2:
                    reset_display, resume_msg = quota_info[0], quota_info[1]
                else:
                    reset_display, resume_msg = "", "Auto-resume in ~5h"
                log("quota", f"Quota reached. {reset_display}")

                # Create pause state so the main loop actually stops
                reset_ts, _disp = _compute_quota_reset_ts(instance)
                from app.pause_manager import create_pause
                create_pause(koan_root, "quota", reset_ts, reset_display or _disp)

                _commit_instance(instance, f"koan: quota exhausted {time.strftime('%Y-%m-%d-%H:%M')}")
                _notify(instance, (
                    f"⚠️ Claude quota exhausted. {reset_display}\n\n"
                    f"Kōan paused after {count} runs. {resume_msg} or use /resume to restart manually."
                ))
                return True  # ran Claude before quota hit — productive
        except Exception as e:
            log("error", f"Post-mission processing error: {e}")
    finally:
        _cleanup_temp(stdout_file, stderr_file)

    # Report result — always notify on completion (success or failure)
    if claude_exit == 0:
        log("mission", f"Run {run_num}/{max_runs} — [{project_name}] completed successfully")
    _notify_mission_end(
        instance, project_name, run_num, max_runs,
        claude_exit, mission_title,
    )

    # Commit instance
    _commit_instance(instance)

    # Periodic git sync
    if (count + 1) % git_sync_interval == 0:
        with protected_phase("Git sync"):
            log("git", f"Periodic git sync (run {count + 1})...")
            from app.git_sync import GitSync
            for name, path in projects:
                try:
                    gs = GitSync(instance, name, path)
                    gs.sync_and_report()
                except Exception as e:
                    log("error", f"Periodic git sync failed for {name}: {e}")

    # Max runs check
    if count + 1 >= max_runs:
        log("koan", f"Max runs ({max_runs}) reached. Running evening ritual before pause.")
        with protected_phase("Evening ritual"):
            try:
                from app.rituals import run_ritual
                run_ritual("evening", Path(instance))
            except Exception as e:
                log("error", f"Evening ritual failed: {e}")
        log("pause", "Entering pause mode (auto-resume in 5h).")
        from app.pause_manager import create_pause
        create_pause(koan_root, "max_runs")
        _notify(instance, (
            f"⏸️ Kōan paused: {max_runs} runs completed. "
            "Auto-resume in 5h or use /resume to restart."
        ))
        return True  # completed final productive run

    # Sleep between runs (skip if pending missions)
    _sleep_between_runs(koan_root, instance, interval, run_num, max_runs)

    return True  # productive iteration completed


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
    pause mode after MAX_CONSECUTIVE_ERRORS to avoid thrashing.
    """
    tb = traceback.format_exc()
    log("error", f"Iteration failed ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {error}")
    log("error", f"Traceback:\n{tb}")
    set_status(koan_root, f"Error recovery ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})")

    # Notify on first error and periodically
    if _should_notify_error(consecutive_errors):
        _notify(instance, (
            f"⚠️ Run loop error ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): "
            f"{type(error).__name__}: {error}"
        ))

    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
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
    backoff = _calculate_backoff(consecutive_errors, MAX_BACKOFF_ITERATION)
    log("koan", f"Recovering in {backoff}s...")
    time.sleep(backoff)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_quota_reset_ts(instance: str):
    """Compute quota reset timestamp and display string.

    Returns (reset_ts: int, reset_display: str). Falls back to
    QUOTA_RETRY_SECONDS from now if estimation fails.
    """
    reset_ts = None
    reset_display = ""
    try:
        from app.usage_estimator import cmd_reset_time, _estimate_reset_time, _load_state
        usage_state_path = Path(instance, "usage_state.json")
        reset_ts = cmd_reset_time(usage_state_path)
        state = _load_state(usage_state_path)
        reset_display = f"session reset in ~{_estimate_reset_time(state.get('session_start', ''), 5)}"
    except Exception as e:
        log("error", f"Reset time estimation failed: {e}")
    if reset_ts is None:
        from app.pause_manager import QUOTA_RETRY_SECONDS
        reset_ts = int(time.time()) + QUOTA_RETRY_SECONDS
    return reset_ts, reset_display


def _compute_preflight_reset_ts(error_output: str):
    """Compute quota reset timestamp from preflight probe error output.

    Returns (reset_ts: int, reset_display: str). Falls back to
    QUOTA_RETRY_SECONDS from now if extraction fails.
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


def _start_mission_in_file(instance: str, mission_title: str):
    """Move mission from Pending to In Progress via locked write."""
    try:
        from app.missions import start_mission
        from app.utils import modify_missions_file
        missions_path = Path(instance, "missions.md")
        if not missions_path.exists():
            return
        modify_missions_file(missions_path, lambda c: start_mission(c, mission_title))
    except Exception as e:
        log("error", f"Could not start mission in missions.md: {e}")


def _update_mission_in_file(instance: str, mission_title: str, *, failed: bool = False):
    """Move mission from Pending/In Progress to Done/Failed via locked write."""
    try:
        from app.missions import complete_mission, fail_mission
        from app.utils import modify_missions_file
        missions_path = Path(instance, "missions.md")
        if not missions_path.exists():
            return
        transform = fail_mission if failed else complete_mission
        before = [None]

        def tracked(content):
            before[0] = content
            return transform(content, mission_title)

        after = modify_missions_file(missions_path, tracked)
        if before[0] is not None and after == before[0]:
            log("warning", f"Mission not found (no change): {mission_title[:80]}")
    except Exception as e:
        label = "fail" if failed else "complete"
        log("error", f"Could not {label} mission in missions.md: {e}")


def _finalize_mission(instance: str, mission_title: str, project_name: str, exit_code: int):
    """Complete or fail a mission and record execution history."""
    _update_mission_in_file(instance, mission_title, failed=(exit_code != 0))
    try:
        from app.mission_history import record_execution
        record_execution(instance, mission_title, project_name, exit_code)
    except (OSError, ValueError) as e:
        log("error", f"Mission history recording error: {e}")


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


def _run_skill_mission(
    skill_cmd: list,
    koan_root: str,
    instance: str,
    project_name: str,
    project_path: str,
    run_num: int,
    mission_title: str,
    autonomous_mode: str,
) -> int:
    """Execute a skill-dispatched mission directly via subprocess.

    Streams stdout/stderr line-by-line to pending.md so /live can show
    real-time progress during skill dispatch.

    Returns the process exit code (0 = success).
    """
    from app.debug import debug_log

    mission_start = int(time.time())
    koan_pkg_dir = os.path.join(koan_root, "koan")
    pending_path = Path(instance) / "journal" / "pending.md"

    # Explicitly set PYTHONPATH so the subprocess can always resolve
    # app.* modules even if the working tree changes (e.g. skill does
    # a git checkout on the koan repo itself).
    skill_env = {**os.environ, "PYTHONPATH": koan_pkg_dir}

    # Record the koan repo's HEAD before execution.  Skills like
    # /rebase and /recreate do git checkouts on project_path which
    # may be the koan repo itself — if they crash without restoring
    # the branch, subsequent runs break.
    koan_branch_before = _get_koan_branch(koan_root)

    from app.config import get_skill_timeout
    skill_timeout = get_skill_timeout()

    debug_log(f"[run] skill exec: cmd={' '.join(skill_cmd)}")
    debug_log(f"[run] skill exec: cwd={koan_pkg_dir} timeout={skill_timeout}s")
    stdout_lines = []
    proc = None
    timed_out = False

    # Create temp files for post-mission processing up front.
    # stderr is redirected to a file instead of a pipe to eliminate
    # deadlock risk: if a background drain thread dies (e.g.
    # UnicodeDecodeError), the pipe fills and both processes stall.
    fd_out, stdout_file = tempfile.mkstemp(prefix="koan-out-")
    os.close(fd_out)
    fd_err, stderr_file = tempfile.mkstemp(prefix="koan-err-")
    os.close(fd_err)
    stderr_fh = None
    try:
        stderr_fh = open(stderr_file, "w")
        proc = subprocess.Popen(
            skill_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_fh,
            cwd=koan_pkg_dir,
            env=skill_env,
            text=True,
            start_new_session=True,
        )
        # Register for double-tap CTRL-C termination.
        _sig.claude_proc = proc

        # Watchdog timer: kills the process group if the skill exceeds
        # skill_timeout.  Without this, the ``for line in proc.stdout``
        # loop below blocks indefinitely if the subprocess hangs without
        # closing its stdout pipe — ``proc.wait(timeout=...)`` is never
        # reached because the iterator never finishes.
        def _watchdog():
            nonlocal timed_out
            timed_out = True
            _kill_process_group(proc)

        timer = threading.Timer(skill_timeout, _watchdog)
        timer.daemon = True
        timer.start()

        # Stream stdout line-by-line, appending each to pending.md
        # so /live shows real-time progress.
        try:
            for line in proc.stdout:
                stripped = line.rstrip("\n")
                stdout_lines.append(stripped)
                print(stripped)
                try:
                    with open(pending_path, "a") as f:
                        f.write(f"{stripped}\n")
                except OSError:
                    pass
        finally:
            timer.cancel()

        proc.wait(timeout=30)
        if timed_out:
            # Watchdog killed the process — treat as timeout
            raise subprocess.TimeoutExpired(skill_cmd, skill_timeout)
        exit_code = proc.returncode
        skill_stdout = "\n".join(stdout_lines)
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
        timed_out = True
        log("error", f"Skill runner timed out ({skill_timeout}s)")
        debug_log(f"[run] skill exec: TIMEOUT ({skill_timeout}s)")
        exit_code = 1
        skill_stdout = "\n".join(stdout_lines)
        skill_stderr = ""
    except Exception as e:
        if proc is not None:
            _kill_process_group(proc)
        log("error", f"Skill runner failed: {e}")
        debug_log(f"[run] skill exec: EXCEPTION {e}")
        exit_code = 1
        skill_stdout = "\n".join(stdout_lines)
        skill_stderr = ""
    finally:
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
    try:
        with open(stdout_file, 'wb') as f:
            f.write(skill_stdout.encode('utf-8'))

        from app.mission_runner import run_post_mission
        run_post_mission(
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
        )
    except Exception as e:
        log("error", f"Post-mission error: {e}")
    finally:
        _cleanup_temp(stdout_file, stderr_file)
    duration = int(time.time()) - mission_start
    debug_log(f"[run] skill exec: done in {duration}s, exit_code={exit_code}")
    return exit_code


def _cleanup_temp(*files):
    """Remove temporary files."""
    for f in files:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError:
            pass


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
                # Restart signal
                crash_count = 0
                print("[koan] Restarting run loop...")
                time.sleep(1)
                continue
            raise
        except Exception:
            crash_count += 1
            tb = traceback.format_exc()
            print(f"[koan] Unexpected crash ({crash_count}/{MAX_MAIN_CRASHES}): {tb}", file=sys.stderr)

            if crash_count >= MAX_MAIN_CRASHES:
                print(f"[koan] Too many crashes ({MAX_MAIN_CRASHES}). Giving up.", file=sys.stderr)
                break

            backoff = _calculate_backoff(crash_count, MAX_BACKOFF_MAIN)
            print(f"[koan] Restarting in {backoff}s...", file=sys.stderr)
            time.sleep(backoff)

    _reset_terminal()


if __name__ == "__main__":
    main()
