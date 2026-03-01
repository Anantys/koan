"""Exclusive PID file management for Kōan processes.

Ensures only one instance of each process type (run, awake, ollama) can run
at a time. Uses fcntl.flock() — OS releases lock on crash.

PID files live in $KOAN_ROOT:
  .koan-pid-run    — agent loop (run.py)
  .koan-pid-awake  — Telegram bridge (awake.py)
  .koan-pid-ollama — ollama serve (external binary)

Log files live in $KOAN_ROOT/logs/:
  run.log          — agent loop output
  awake.log        — Telegram bridge output
  ollama.log       — ollama serve output

Usage from Python:
    lock = acquire_pidfile(koan_root, "awake")
    # ... run main loop ...
    release_pidfile(lock, koan_root, "awake")
"""

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, IO


def _pidfile_path(koan_root: Path, process_name: str) -> Path:
    """Return the PID file path for a given process type."""
    return koan_root / f".koan-pid-{process_name}"


def _log_dir(koan_root: Path) -> Path:
    """Return the logs directory, creating it if needed."""
    d = koan_root / "logs"
    d.mkdir(exist_ok=True)
    return d


def _open_log_file(koan_root: Path, process_name: str):
    """Open a log file for a process, rotating the previous log first.

    Before truncating, backs up the existing log as .log.1, shifting older
    backups (.log.1 → .log.2, etc.) and compressing old ones with gzip.

    Returns an open file handle suitable for subprocess stdout/stderr.

    The caller should close the handle after passing it to Popen.
    The child process inherits the file descriptor via fork, so
    closing the parent's copy is safe and prevents fd leaks.
    """
    from app.log_rotation import rotate_log, get_log_config
    
    log_path = _log_dir(koan_root) / f"{process_name}.log"
    
    # Load rotation config, with sensible defaults if config unavailable
    config = None
    try:
        from app.utils import load_config
        config = load_config()
    except (ImportError, OSError, ValueError):
        pass  # Fall back to defaults
    
    cfg = get_log_config(config)
    rotate_log(log_path, max_backups=cfg["max_backups"], compress=cfg["compress"])
    
    # Open with buffering disabled for immediate log visibility
    return open(log_path, "w", buffering=1)


def _read_pid(pidfile: Path) -> Optional[int]:
    """Read the PID from a PID file, or None if unreadable."""
    try:
        text = pidfile.read_text().strip()
        return int(text) if text else None
    except (ValueError, OSError):
        return None


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive (not a zombie).

    ``os.kill(pid, 0)`` succeeds for zombie processes, which look alive
    but are effectively dead.  When the caller is not the parent (common
    with ``make stop``), zombies are not reapable via ``waitpid`` and
    would cause ``_wait_for_exit`` to burn the full timeout.

    Falls back to ``ps`` to detect zombie state on macOS/Linux.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    # os.kill(pid, 0) succeeded — but it could be a zombie.
    # Try /proc first (Linux, fast), then ps (macOS/Linux, slower).
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line
    except (FileNotFoundError, PermissionError, OSError):
        pass  # macOS or /proc not available

    try:
        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        state = result.stdout.strip()
        if state and state[0] in ("Z", "z"):
            return False
    except (subprocess.SubprocessError, OSError):
        pass

    return True


def acquire_pidfile(koan_root: Path, process_name: str) -> IO:
    """Acquire an exclusive flock on the PID file (for Python processes).

    If another instance holds the lock, prints an error with the
    running PID and exits with code 1.

    Returns the open file handle — caller must keep it alive for the
    duration of the process (closing it releases the lock).
    """
    pidfile = _pidfile_path(koan_root, process_name)

    fh = open(pidfile, "a+")

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        # Lock held by another process
        fh.seek(0)
        existing_pid = None
        try:
            text = fh.read().strip()
            existing_pid = int(text) if text else None
        except ValueError:
            pass
        fh.close()

        msg = f"Error: {process_name} process already running"
        if existing_pid:
            msg += f" (PID {existing_pid})"
        msg += ". Aborting."
        print(msg, file=sys.stderr)
        sys.exit(1)

    # Lock acquired — write our PID
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()

    return fh


def release_pidfile(fh: IO, koan_root: Path, process_name: str) -> None:
    """Release the PID file lock and remove the file."""
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    except (OSError, ValueError):
        # ValueError: file already closed (idempotent release)
        pass

    pidfile = _pidfile_path(koan_root, process_name)
    pidfile.unlink(missing_ok=True)


def acquire_pid(koan_root: Path, process_name: str, pid: int) -> None:
    """Write a PID file after checking no other instance is alive.

    For bash processes that can't hold a Python flock. Checks if the
    PID in the existing file is still alive — if so, aborts.

    Args:
        koan_root: Root path of the Kōan installation.
        process_name: Process type ("run" or "awake").
        pid: The PID to write (typically $$ from bash).
    """
    pidfile = _pidfile_path(koan_root, process_name)

    if pidfile.exists():
        existing_pid = _read_pid(pidfile)
        if existing_pid and existing_pid != pid and _is_process_alive(existing_pid):
            print(
                f"Error: {process_name} process already running "
                f"(PID {existing_pid}). Aborting.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Write our PID
    pidfile.write_text(str(pid))


def release_pid(koan_root: Path, process_name: str) -> None:
    """Remove the PID file (for bash processes)."""
    pidfile = _pidfile_path(koan_root, process_name)
    pidfile.unlink(missing_ok=True)


def check_pidfile(koan_root: Path, process_name: str) -> Optional[int]:
    """Check if a process is running via its PID file.

    Tries flock first (detects Python processes), falls back to PID
    liveness check (detects bash processes).

    Returns the PID if running, None otherwise.
    """
    pidfile = _pidfile_path(koan_root, process_name)
    if not pidfile.exists():
        return None

    # Try flock probe — detects Python processes holding the lock
    try:
        fh = open(pidfile, "r")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Lock acquired — no Python process holding it
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
        except (OSError, BlockingIOError):
            # Lock held — Python process is running
            pid = _read_pid(pidfile)
            fh.close()
            return pid
    except OSError:
        pass

    # Fall back to PID liveness check (for bash processes)
    pid = _read_pid(pidfile)
    if pid and _is_process_alive(pid):
        return pid

    return None


PROCESS_NAMES = ("run", "awake", "ollama")

# Process startup verification timeouts
DEFAULT_VERIFY_TIMEOUT = 3.0
OLLAMA_VERIFY_TIMEOUT = 5.0


def _launch_python_process(
    koan_root: Path, script_name: str, process_name: str, verify_timeout: float
) -> tuple:
    """Launch a Python process in the background and verify startup.
    
    Args:
        koan_root: Root path of the Kōan installation.
        script_name: Python script filename (e.g., "app/run.py").
        process_name: Process type identifier ("run" or "awake").
        verify_timeout: Seconds to wait for PID file verification.
    
    Returns:
        (success: bool, message: str)
    """
    # Already running?
    pid = check_pidfile(koan_root, process_name)
    if pid:
        return False, f"{process_name.capitalize()} already running (PID {pid})"

    # Build launch command
    python = sys.executable
    koan_dir = koan_root / "koan"
    env = {
        **os.environ,
        "KOAN_ROOT": str(koan_root),
        "PYTHONPATH": str(koan_dir),
        "KOAN_FORCE_COLOR": "1",
    }

    log_fh = _open_log_file(koan_root, process_name)
    try:
        subprocess.Popen(
            [python, script_name],
            cwd=str(koan_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"Failed to launch: {e}"
    finally:
        log_fh.close()

    # Wait briefly for process to acquire its PID file
    deadline = time.monotonic() + verify_timeout
    while time.monotonic() < deadline:
        new_pid = check_pidfile(koan_root, process_name)
        if new_pid:
            label = "Agent loop" if process_name == "run" else "Bridge"
            return True, f"{label} started (PID {new_pid})"
        time.sleep(0.3)

    return False, "Launched but PID not detected — check logs"


def start_runner(koan_root: Path, verify_timeout: float = DEFAULT_VERIFY_TIMEOUT) -> tuple:
    """Start the agent loop (run.py) as a detached subprocess.

    Clears .koan-stop signal, launches run.py, and verifies startup
    via PID file.

    Returns (success: bool, message: str).
    """
    # Clear stop and pause signals so run.py starts fresh
    for signal_file in (".koan-stop", ".koan-pause", ".koan-pause-reason"):
        (koan_root / signal_file).unlink(missing_ok=True)

    return _launch_python_process(koan_root, "app/run.py", "run", verify_timeout)


def start_ollama(koan_root: Path, verify_timeout: float = OLLAMA_VERIFY_TIMEOUT) -> tuple:
    """Start ollama serve as a detached subprocess.

    Checks that ollama binary is available, not already running,
    then launches it in the background with a tracked PID file.

    Returns (success: bool, message: str).
    """
    pid = check_pidfile(koan_root, "ollama")
    if pid:
        return False, f"ollama already running (PID {pid})"

    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return False, "ollama not found in PATH — install with: brew install ollama"

    log_fh = _open_log_file(koan_root, "ollama")
    try:
        proc = subprocess.Popen(
            [ollama_bin, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"Failed to launch ollama: {e}"
    finally:
        log_fh.close()

    # Write PID file — ollama serve is an external binary (no flock)
    acquire_pid(koan_root, "ollama", proc.pid)

    # Wait for ollama to start listening on HTTP
    deadline = time.monotonic() + verify_timeout
    while time.monotonic() < deadline:
        if not _is_process_alive(proc.pid):
            release_pid(koan_root, "ollama")
            return False, "ollama launched but exited immediately — check ollama logs"
        if _ollama_http_ready():
            return True, f"ollama serve started (PID {proc.pid})"
        time.sleep(0.3)

    # Process is alive but HTTP not ready yet — still report success
    if _is_process_alive(proc.pid):
        return True, f"ollama serve started (PID {proc.pid}), still warming up"
    release_pid(koan_root, "ollama")
    return False, "ollama launched but exited immediately — check ollama logs"


def start_awake(koan_root: Path, verify_timeout: float = DEFAULT_VERIFY_TIMEOUT) -> tuple:
    """Start the Telegram bridge (awake.py) as a detached subprocess.

    Returns (success: bool, message: str).
    """
    return _launch_python_process(koan_root, "app/awake.py", "awake", verify_timeout)


def get_status_processes(koan_root: Path) -> tuple:
    """Return the process names to display in status output.

    Only includes ollama when the CLI provider is local/ollama.
    """
    provider = _detect_provider(koan_root)
    if _needs_ollama(provider):
        return PROCESS_NAMES
    return tuple(n for n in PROCESS_NAMES if n != "ollama")


def _read_runner_state(koan_root: Path) -> dict:
    """Read the runner's current state from signal files.

    Gathers information from .koan-status, .koan-pause, .koan-pause-reason,
    and .koan-project to build a complete picture of the runner state.

    Returns a dict with keys: status, paused, pause_reason, project.
    All values are strings (empty string if unavailable).
    """
    state = {"status": "", "paused": False, "pause_reason": "", "project": ""}

    status_file = koan_root / ".koan-status"
    if status_file.exists():
        try:
            state["status"] = status_file.read_text().strip()
        except OSError:
            pass

    pause_file = koan_root / ".koan-pause"
    if pause_file.exists():
        state["paused"] = True
        reason_file = koan_root / ".koan-pause-reason"
        if reason_file.exists():
            try:
                state["pause_reason"] = reason_file.read_text().strip().split("\n")[0]
            except OSError:
                pass

    project_file = koan_root / ".koan-project"
    if project_file.exists():
        try:
            state["project"] = project_file.read_text().strip()
        except OSError:
            pass

    return state


def format_status_all(koan_root: Path) -> list:
    """Build a rich status display for 'make status'.

    Shows process PIDs, runner state (mode, mission, project),
    and pause information. Only shows runner context when the
    runner process is actually alive.

    Returns a list of lines to print.
    """
    lines = []
    process_names = get_status_processes(koan_root)
    run_pid = check_pidfile(koan_root, "run")
    awake_pid = check_pidfile(koan_root, "awake")

    # Read state files (only meaningful when runner is alive)
    runner_state = _read_runner_state(koan_root) if run_pid else {}

    # --- Runner ---
    if run_pid:
        status_text = runner_state.get("status", "")
        if runner_state.get("paused"):
            reason = runner_state.get("pause_reason", "")
            if reason == "quota":
                lines.append(f"  run: paused — quota exhausted (PID {run_pid})")
            elif reason == "max_runs":
                lines.append(f"  run: paused — max runs reached (PID {run_pid})")
            elif reason == "errors":
                lines.append(f"  run: paused — too many errors (PID {run_pid})")
            elif reason:
                lines.append(f"  run: paused — {reason} (PID {run_pid})")
            else:
                lines.append(f"  run: paused (PID {run_pid})")
        elif status_text:
            lines.append(f"  run: {status_text} (PID {run_pid})")
        else:
            lines.append(f"  run: running (PID {run_pid})")

        # Show current project when runner is active and not idle
        project = runner_state.get("project", "")
        if project and not runner_state.get("paused"):
            lines.append(f"       project: {project}")
    else:
        lines.append("  run: not running")

    # --- Bridge ---
    if awake_pid:
        lines.append(f"  awake: running (PID {awake_pid})")
    else:
        lines.append("  awake: not running")

    # --- Ollama (conditional) ---
    if "ollama" in process_names:
        ollama_pid = check_pidfile(koan_root, "ollama")
        if ollama_pid:
            lines.append(f"  ollama: running (PID {ollama_pid})")
        else:
            lines.append("  ollama: not running")

    return lines


def _detect_provider(koan_root: Path) -> str:
    """Detect the configured CLI provider.

    Uses the provider package resolution (env var > config.yaml > default).
    Returns provider name: "claude", "copilot", "local", "ollama",
    or "ollama-launch".
    """
    try:
        # Lazy import to avoid circular deps and keep pid_manager lightweight
        from app.provider import get_provider_name
        return get_provider_name()
    except (ImportError, OSError, ValueError):
        return "claude"


def _ollama_http_ready() -> bool:
    """Check if Ollama server is responding on HTTP."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _needs_ollama(provider: str) -> bool:
    """Return True if the provider requires ollama serve."""
    return provider in ("local", "ollama")


def _show_startup_banner(koan_root: Path, provider: str) -> None:
    """Display the unified startup banner with system information.
    
    Banner is cosmetic and never blocks startup. Logs errors to stderr
    if banner fails to display.
    """
    try:
        from app.banners import print_startup_banner
        from app.startup_info import gather_startup_info
        info = gather_startup_info(koan_root)
        info["provider"] = provider
        print_startup_banner(info)
    except Exception as e:
        # Banner is cosmetic — log but don't block startup
        print(f"Warning: Failed to display startup banner: {e}", file=sys.stderr)


def start_all(koan_root: Path, provider: str = None) -> dict:
    """Start the full Kōan stack for the configured provider.

    Auto-detects the provider if not specified.
    - claude/copilot/ollama-launch: starts awake + run (2 processes)
    - local/ollama: starts ollama + awake + run (3 processes)

    Note: ollama-launch does not need a separate ollama serve process
    because ``ollama launch claude`` handles server lifecycle internally.

    Returns dict mapping component name to (success, message).
    """
    if provider is None:
        provider = _detect_provider(koan_root)

    # Display startup banner before launching processes
    _show_startup_banner(koan_root, provider)

    results = {}

    # 1. Start ollama serve if needed
    if _needs_ollama(provider):
        ok, msg = start_ollama(koan_root)
        results["ollama"] = (ok, msg)
        if ok:
            time.sleep(1)

    # 2. Start awake (Telegram bridge)
    ok, msg = start_awake(koan_root)
    results["awake"] = (ok, msg)

    # 3. Start agent loop (run.py)
    ok, msg = start_runner(koan_root)
    results["run"] = (ok, msg)

    return results


def start_stack(koan_root: Path) -> dict:
    """Start the full ollama stack: ollama serve + awake + run.

    Kept for backward compatibility with `make ollama`.
    Delegates to start_all() with provider="local".
    """
    return start_all(koan_root, provider="local")


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Wait for a process to exit, with timeout.

    Returns True if the process exited, False if still alive after timeout.
    Handles both child processes (waitpid) and non-children (kill probe).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Try to reap child zombie (works if we're the parent)
        try:
            result = os.waitpid(pid, os.WNOHANG)
            if result[0] != 0:
                return True  # Child reaped
        except ChildProcessError:
            pass  # Not our child — use kill probe

        if not _is_process_alive(pid):
            return True
        time.sleep(0.2)
    return not _is_process_alive(pid)


def stop_processes(koan_root: Path, timeout: float = 5.0) -> dict:
    """Stop all running Kōan processes (run + awake + ollama).

    Sends SIGTERM to each running process, waits up to timeout seconds
    for termination. Creates .koan-stop signal file for graceful shutdown.

    Returns dict mapping process name to result: "stopped", "not_running",
    or "force_killed".
    """
    results = {}

    # Create .koan-stop signal file for graceful run loop shutdown
    stop_file = koan_root / ".koan-stop"
    stop_file.write_text("STOP")

    for name in PROCESS_NAMES:
        pid = check_pidfile(koan_root, name)
        if not pid:
            results[name] = "not_running"
            continue

        # Send SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            results[name] = "not_running"
            continue

        # Wait for process to exit
        if _wait_for_exit(pid, timeout):
            results[name] = "stopped"
        else:
            # Force kill
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            # Wait briefly for SIGKILL to take effect
            _wait_for_exit(pid, 1.0)
            results[name] = "force_killed"

        # Clean up PID file
        pidfile = _pidfile_path(koan_root, name)
        pidfile.unlink(missing_ok=True)

    return results


def _print_stack_results(results: dict) -> int:
    """Print stack start results and return exit code (0=ok, 1=failure)."""
    any_failed = False
    for name in ("ollama", "awake", "run"):
        if name not in results:
            continue
        ok, msg = results[name]
        print(f"  {name}: {msg}")
        if not ok and "already running" not in msg.lower():
            any_failed = True

    if not any_failed:
        print()
        print("  Use 'make logs' to watch live output")
        print("  Use 'make status' to check process status")
        print("  Use 'make stop' to stop all processes")

    return 1 if any_failed else 0


# --- CLI interface ---
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python -m app.pid_manager "
            "<acquire-pid|release-pid|check|stop-all> <run|awake|KOAN_ROOT> [koan_root] [pid]",
            file=sys.stderr,
        )
        sys.exit(2)

    action = sys.argv[1]

    if action == "stop-all":
        root = Path(sys.argv[2])
        results = stop_processes(root)
        any_stopped = False
        for name, result in results.items():
            if result == "stopped":
                print(f"  {name}: stopped")
                any_stopped = True
            elif result == "force_killed":
                print(f"  {name}: force killed")
                any_stopped = True
            else:
                print(f"  {name}: not running")
        if not any_stopped:
            print("No processes were running.")
        sys.exit(0)

    if action == "start-runner":
        root = Path(sys.argv[2])
        ok, msg = start_runner(root)
        print(f"  {msg}")
        sys.exit(0 if ok else 1)

    if action == "start-ollama":
        root = Path(sys.argv[2])
        ok, msg = start_ollama(root)
        print(f"  {msg}")
        sys.exit(0 if ok else 1)

    if action == "start-all":
        root = Path(sys.argv[2])
        provider = sys.argv[3] if len(sys.argv) > 3 else None
        results = start_all(root, provider=provider)
        sys.exit(_print_stack_results(results))

    if action == "start-stack":
        root = Path(sys.argv[2])
        results = start_stack(root)
        sys.exit(_print_stack_results(results))

    if action == "status-all":
        root = Path(sys.argv[2])
        for line in format_status_all(root):
            print(line)
        sys.exit(0)

    if len(sys.argv) < 4:
        print(
            "Usage: python -m app.pid_manager "
            "<acquire-pid|release-pid|check> <run|awake> <koan_root> [pid]",
            file=sys.stderr,
        )
        sys.exit(2)

    proc_name = sys.argv[2]
    if proc_name not in PROCESS_NAMES:
        print(f"Invalid process name: {proc_name}. Must be one of: {', '.join(PROCESS_NAMES)}",
              file=sys.stderr)
        sys.exit(2)
    root = Path(sys.argv[3])

    if action == "acquire-pid":
        if len(sys.argv) < 5:
            print("acquire-pid requires a PID argument", file=sys.stderr)
            sys.exit(2)
        acquire_pid(root, proc_name, int(sys.argv[4]))
    elif action == "release-pid":
        release_pid(root, proc_name)
    elif action == "check":
        pid = check_pidfile(root, proc_name)
        if pid:
            print(f"running:{pid}")
        else:
            print("not_running")
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(2)
