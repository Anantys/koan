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

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, IO

from app.signals import (
    CYCLE_FILE,
    PAUSE_FILE,
    PROJECT_FILE,
    STATUS_FILE,
    STOP_FILE,
    pid_file,
)


def _pidfile_path(koan_root: Path, process_name: str) -> Path:
    """Return the PID file path for a given process type."""
    return koan_root / pid_file(process_name)


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
    except BaseException:
        fh.close()
        raise

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


PROCESS_NAMES = ("run", "awake", "ollama", "dashboard", "api", "mcp")

# Process startup verification timeouts
DEFAULT_VERIFY_TIMEOUT = 3.0
OLLAMA_VERIFY_TIMEOUT = 5.0


def _launch_python_process(
    koan_root: Path, script_name: str, process_name: str, verify_timeout: float,
    extra_env: dict = None,
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
        **(extra_env or {}),
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
            labels = {
                "run": "Agent loop",
                "awake": "Bridge",
                "dashboard": "Dashboard",
                "api": "REST API",
                "mcp": "MCP HTTP",
            }
            label = labels.get(process_name, process_name.capitalize())
            return True, f"{label} started (PID {new_pid})"
        time.sleep(0.3)

    return False, "Launched but PID not detected — check logs"


def start_runner(
    koan_root: Path, verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    extra_env: dict = None,
) -> tuple:
    """Start the agent loop (run.py) as a detached subprocess.

    Clears .koan-stop signal, launches run.py, and verifies startup
    via PID file.

    Args:
        extra_env: Additional environment variables passed to the subprocess
                   (e.g. {"KOAN_SKIP_START_PAUSE": "1"} to bypass start_on_pause).

    Returns (success: bool, message: str).
    """
    # Clear stop, pause, and cycle signals so run.py starts fresh
    for signal_file in (STOP_FILE, PAUSE_FILE, CYCLE_FILE):
        (koan_root / signal_file).unlink(missing_ok=True)

    return _launch_python_process(
        koan_root, "app/run.py", "run", verify_timeout, extra_env=extra_env,
    )


def _ollama_railway_opt_in() -> bool:
    """True when the operator explicitly allows ollama serve on a hosted deploy.

    ``ollama serve`` is the single largest idle RAM consumer in the stack, so on
    a Railway (hosted single-container) deploy — whose profile defaults to the
    Claude provider — it is refused unless ``KOAN_ALLOW_OLLAMA`` opts in.
    """
    return os.environ.get("KOAN_ALLOW_OLLAMA", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _ollama_start_allowed(koan_root: Path, provider: Optional[str] = None) -> tuple:
    """Decide whether ``ollama serve`` may be started on this deploy.

    Returns ``(allowed: bool, reason: str)``. ``reason`` is empty when allowed.

    ``ollama serve`` must never launch unless the resolved CLI provider is
    exactly ``"ollama"`` — it is the largest idle memory consumer and is pure
    wasted footprint on a Claude/Copilot deploy (#2172). On Railway the hosted
    profile defaults to Claude, so even an ``ollama`` provider is refused unless
    ``KOAN_ALLOW_OLLAMA`` explicitly opts in.

    When ``provider`` is None it is resolved via :func:`_detect_provider`.
    Callers that deliberately want ollama (e.g. ``make ollama``) pass
    ``provider="ollama"`` to assert that intent.
    """
    if provider is None:
        provider = _detect_provider(koan_root)
    if not _needs_ollama(provider):
        return False, (
            f"provider is '{provider}', not 'ollama' — skipping ollama serve "
            "(saves idle RAM)"
        )
    try:
        from app.railway import is_railway
        railway = is_railway()
    except (ImportError, OSError, ValueError):
        railway = False
    if railway and not _ollama_railway_opt_in():
        return False, (
            "Railway deploy defaults to the Claude provider — refusing to start "
            "ollama serve. Set KOAN_ALLOW_OLLAMA=1 to override."
        )
    return True, ""


def start_ollama(
    koan_root: Path,
    verify_timeout: float = OLLAMA_VERIFY_TIMEOUT,
    provider: Optional[str] = None,
) -> tuple:
    """Start ollama serve as a detached subprocess.

    Refuses to launch unless :func:`_ollama_start_allowed` permits it — this is
    the single chokepoint that guarantees ``ollama serve`` (the largest idle RAM
    consumer) never starts on a non-ollama deploy or an opt-out Railway deploy
    (#2172). Then checks that the ollama binary is available and not already
    running, and launches it in the background with a tracked PID file.

    ``provider`` lets a caller assert the resolved/intended provider; when None
    it is detected from env/config.

    Returns (success: bool, message: str).
    """
    allowed, reason = _ollama_start_allowed(koan_root, provider)
    if not allowed:
        return False, reason

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

    # Wait briefly for ollama to start listening
    deadline = time.monotonic() + verify_timeout
    while time.monotonic() < deadline:
        if _is_process_alive(proc.pid):
            return True, f"ollama serve started (PID {proc.pid})"
        time.sleep(0.3)

    # Clean up stale PID file — process is dead, don't leave phantom PIDs
    release_pid(koan_root, "ollama")
    return False, "ollama launched but exited immediately — check ollama logs"


def start_awake(koan_root: Path, verify_timeout: float = DEFAULT_VERIFY_TIMEOUT) -> tuple:
    """Start the Telegram bridge (awake.py) as a detached subprocess.

    Returns (success: bool, message: str).
    """
    return _launch_python_process(koan_root, "app/awake.py", "awake", verify_timeout)


def start_dashboard(koan_root: Path, verify_timeout: float = DEFAULT_VERIFY_TIMEOUT) -> tuple:
    """Start the web dashboard (dashboard/__main__.py) as a detached subprocess.

    Only launched when ``dashboard.enabled: true`` in config.yaml.
    Returns (success: bool, message: str).
    """
    return _launch_python_process(koan_root, "app/dashboard/__main__.py", "dashboard", verify_timeout)


def start_api(koan_root: Path, verify_timeout: float = DEFAULT_VERIFY_TIMEOUT) -> tuple:
    """Start the REST API server (api/server.py) as a detached subprocess.

    Only launched when ``api.enabled: true`` in config.yaml.
    Returns (success: bool, message: str).
    """
    return _launch_python_process(koan_root, "app/api/server.py", "api", verify_timeout)


def start_mcp(
    koan_root: Path,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
) -> tuple:
    """Start the MCP Streamable HTTP daemon as a detached subprocess.

    Only launched when ``mcp.enabled: true`` and ``mcp.transport: http`` —
    stdio mode is client-launched and never daemonized.
    Returns (success: bool, message: str).
    """
    return _launch_python_process(
        koan_root, "app/mcp/__main__.py", "mcp", verify_timeout,
    )


def _is_mcp_http_enabled() -> bool:
    """Whether the MCP HTTP daemon should be managed by the process manager.

    Only HTTP mode is daemon-managed; stdio is launched by MCP clients and is
    never started by ``make start``.
    """
    try:
        from app.config import get_mcp_enabled, get_mcp_transport

        return get_mcp_enabled() and get_mcp_transport() == "http"
    except (ImportError, OSError, ValueError) as e:
        # Downgrading MCP to "not managed" hides a live daemon from `make
        # status` and skips it in `make stop`, so say why rather than let a
        # transient config-read failure orphan a listening process.
        print(f"[pid_manager] MCP management check failed: {e}", file=sys.stderr)
        return False


def _is_api_enabled() -> bool:
    """Check if REST API is enabled in config.yaml and allowed by the deploy gate.

    On Railway every optional process adds to the idle RAM floor, so the API
    only launches when explicitly opted in (token set) — see
    ``railway.api_allowed``. Off Railway, config gating alone decides."""
    try:
        from app.railway import api_allowed
        if not api_allowed():
            return False
        from app.config import is_api_enabled
        return is_api_enabled()
    except (ImportError, OSError, ValueError):
        return False


def _is_dashboard_enabled() -> bool:
    """Check if dashboard is enabled in config.yaml and allowed by the deploy gate.

    On Railway the dashboard is publicly exposed, so it must not launch without
    KOAN_DASHBOARD_PWD — see ``railway.dashboard_allowed`` (the shared gate also
    enforced by ``docker/dashboard-supervised.sh``)."""
    try:
        from app.railway import dashboard_allowed
        if not dashboard_allowed():
            return False
        from app.config import is_dashboard_enabled
        return is_dashboard_enabled()
    except (ImportError, OSError, ValueError):
        return False


def get_status_processes(koan_root: Path) -> tuple:
    """Return the process names to display in status output.

    Only includes ollama when the CLI provider is local/ollama.
    Only includes dashboard when dashboard.enabled is true in config.
    """
    provider = _detect_provider(koan_root)
    dashboard = _is_dashboard_enabled()
    api = _is_api_enabled()
    mcp = _is_mcp_http_enabled()
    names = list(PROCESS_NAMES)
    if not _needs_ollama(provider):
        names.remove("ollama")
    if not dashboard:
        names.remove("dashboard")
    if not api:
        names.remove("api")
    if not mcp:
        names.remove("mcp")
    return tuple(names)


def _read_runner_state(koan_root: Path) -> dict:
    """Read the runner's current state from signal files.

    Gathers information from .koan-status, .koan-pause, and .koan-project
    to build a complete picture of the runner state.

    Returns a dict with keys: status, paused, pause_reason, project.
    All values are strings (empty string if unavailable).
    """
    state = {"status": "", "paused": False, "pause_reason": "", "project": ""}

    status_file = koan_root / STATUS_FILE
    if status_file.exists():
        with contextlib.suppress(OSError):
            state["status"] = status_file.read_text().strip()

    pause_file = koan_root / PAUSE_FILE
    if pause_file.exists():
        state["paused"] = True
        try:
            content = pause_file.read_text().strip()
            if content:
                state["pause_reason"] = content.split("\n")[0]
        except OSError:
            pass

    project_file = koan_root / PROJECT_FILE
    if project_file.exists():
        with contextlib.suppress(OSError):
            state["project"] = project_file.read_text().strip()

    return state


def _in_progress_count(koan_root: Path) -> int:
    """Best-effort count of In Progress mission lines (#2086)."""
    try:
        from app.mission_store.transition import read_sections

        instance = Path(koan_root) / "instance"
        return len(read_sections(instance).get("in_progress", []))
    except Exception as e:
        # A chronic parse failure silently reports zero In Progress lines,
        # which would suppress the zombie warning for the exact "In Progress
        # but nothing launched" case this feature targets — log it (#2086).
        print(f"[pid_manager] in-progress count failed: {e}", file=sys.stderr)
        return 0


def _format_execution_line(koan_root: Path) -> str:
    """One-line truthful provider-execution state (#2086).

    Cross-checks the declarative In Progress count against observed liveness so
    the loud zombie warning also fires for the common "In Progress but nothing
    ever launched" case — parity with the REST ``/v1/status`` cross-check —
    not only when a dead PID is recorded.
    """
    from app.active_mission import get_execution_state, is_zombie

    ex = get_execution_state(koan_root)
    state = ex["state"]
    in_progress = _in_progress_count(koan_root)

    if is_zombie(koan_root, in_progress=in_progress > 0, execution=ex):
        pid = ex["pid"]
        if pid:
            return f"execution: ⚠ ZOMBIE — In Progress but PID {pid} not alive"
        return "execution: ⚠ ZOMBIE — In Progress but no live provider"
    if state == "zombie":
        # Stale signal: a recorded provider PID is dead even though no mission
        # line is currently In Progress (leftover from a hard crash).
        return f"execution: ⚠ ZOMBIE — stale signal, PID {ex['pid']} not alive"
    if state == "idle":
        return "execution: idle — no provider running"
    pid = ex["pid"]
    elapsed = ex["elapsed"]
    age = ex["last_output_age"]
    if state == "stalled":
        age_s = f"{int(age)}s" if age is not None else "?"
        return f"execution: stalled (PID {pid}, no output for {age_s})"
    age_s = f"{int(age)}s ago" if age is not None else "unknown"
    return f"execution: working (PID {pid}, {elapsed}s, last output {age_s})"


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

        # Truthful provider-execution state from the live PID signal (#2086),
        # not the inferred missions.md timestamp.
        lines.append(f"       {_format_execution_line(koan_root)}")
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

    # --- Dashboard (conditional) ---
    if "dashboard" in process_names:
        dash_pid = check_pidfile(koan_root, "dashboard")
        if dash_pid:
            lines.append(f"  dashboard: running (PID {dash_pid})")
        else:
            lines.append("  dashboard: not running")

    # --- MCP HTTP (conditional) ---
    if "mcp" in process_names:
        mcp_pid = check_pidfile(koan_root, "mcp")
        if mcp_pid:
            lines.append(f"  mcp: running (PID {mcp_pid})")
        else:
            lines.append("  mcp: not running")

    return lines


def _detect_provider(koan_root: Path) -> str:
    """Detect the configured CLI provider.

    Uses the provider package resolution (env var > config.yaml > default).
    Returns provider name: "claude", "cline", "copilot", "ollama",
    or "ollama-launch".
    """
    try:
        # Lazy import to avoid circular deps and keep pid_manager lightweight
        from app.provider import get_provider_name
        return get_provider_name()
    except (ImportError, OSError, ValueError):
        return "claude"


def _needs_ollama(provider: str) -> bool:
    """Return True if the provider requires ollama serve."""
    return provider == "ollama"


def _show_startup_banner(koan_root: Path, provider: str) -> None:
    """Display the unified startup banner with system information.
    
    Banner is cosmetic and never blocks startup. Logs errors to stderr
    if banner fails to display.
    """
    try:
        from app.banners import print_hero_banner
        from app.provider import describe_cli_roles, get_provider_display
        from app.startup_info import gather_startup_info
        info = gather_startup_info(koan_root)
        # Show the alternate CLI binary flavor (KOAN_CLAUDE_CLI_PATH) next to
        # the provider, mirroring /status — e.g. "claude (ollama-claude)".
        info["provider"] = get_provider_display(provider)
        # Append per-role provider overrides (cli: section) when configured.
        roles = describe_cli_roles()
        if roles:
            info["provider"] += f" [{roles}]"
        print_hero_banner(info)
    except Exception as e:
        # Banner is cosmetic — log but don't block startup
        print(f"Warning: Failed to display startup banner: {e}", file=sys.stderr)


def start_all(koan_root: Path, provider: str = None, show_banner: bool = True) -> dict:
    """Start the full Kōan stack for the configured provider.

    Auto-detects the provider if not specified.
    - claude/copilot/ollama-launch: starts awake + run (2 processes)
    - ollama: starts ollama + awake + run (3 processes)

    Note: ollama-launch does not need a separate ollama serve process
    because ``ollama launch claude`` handles server lifecycle internally.

    ``show_banner`` lets the interactive launcher (``make koan``) suppress the
    startup banner it has already rendered itself, avoiding a double banner.

    Returns dict mapping component name to (success, message).
    """
    if provider is None:
        provider = _detect_provider(koan_root)

    # Log the resolved provider so a mis-set env/config (which would otherwise
    # silently launch ollama serve) is visible at startup (#2172).
    print(f"[pid_manager] resolved CLI provider: {provider}", file=sys.stderr)

    # Display startup banner before launching processes
    if show_banner:
        _show_startup_banner(koan_root, provider)

    results = {}

    # 1. Start ollama serve only when the resolved provider needs it AND the
    #    deploy permits it (never on a Claude deploy; not on Railway unless
    #    explicitly opted in). ollama serve is the largest idle RAM consumer.
    allowed, reason = _ollama_start_allowed(koan_root, provider)
    if allowed:
        ok, msg = start_ollama(koan_root, provider=provider)
        results["ollama"] = (ok, msg)
        if ok:
            time.sleep(1)
    elif _needs_ollama(provider):
        # Provider is ollama but the deploy guard refused (e.g. Railway without
        # opt-in). Log the reason — this is intentional, not a launch failure.
        print(f"[pid_manager] {reason}", file=sys.stderr)

    # 2. Start awake (Telegram bridge)
    ok, msg = start_awake(koan_root)
    results["awake"] = (ok, msg)

    # 3. Start agent loop (run.py)
    ok, msg = start_runner(koan_root)
    results["run"] = (ok, msg)

    # 4. Start dashboard if enabled
    if _is_dashboard_enabled():
        ok, msg = start_dashboard(koan_root)
        results["dashboard"] = (ok, msg)

    # 5. Start REST API if enabled
    if _is_api_enabled():
        ok, msg = start_api(koan_root)
        results["api"] = (ok, msg)

    # 6. Start MCP HTTP daemon after the API it depends on
    if _is_mcp_http_enabled():
        ok, msg = start_mcp(koan_root)
        results["mcp"] = (ok, msg)

    return results


def start_stack(koan_root: Path) -> dict:
    """Start the full ollama stack: ollama serve + awake + run.

    Kept for backward compatibility with `make ollama`.
    Delegates to start_all() with the "ollama" sentinel so ollama serve starts.

    Note: with the `local` provider removed, no built-in provider talks to this
    standalone `ollama serve` (`ollama-launch` manages its own server via
    ``ollama launch claude``). `make ollama` is now only useful when you point a
    custom Claude-CLI endpoint at the bare Ollama server yourself; otherwise the
    agent resolves its provider independently. The path is preserved
    intentionally and may be deprecated in a follow-up.
    """
    return start_all(koan_root, provider="ollama")


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


def _bootout_launchd_service(name: str) -> bool:
    """Try to bootout a launchd service. Returns True if service was found and booted out."""
    if sys.platform != "darwin":
        return False
    uid = os.getuid()
    service_label = f"com.koan.{name}"
    try:
        result = subprocess.run(
            ["launchctl", "list", service_label],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{service_label}"],
            capture_output=True, timeout=10,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


# A pid file is written by the daemon once it is already running, so its mtime
# is never earlier than the process's own start time. The slack absorbs coarse
# `ps etime` granularity and clock nudges, not a PID recycled across a reboot —
# that one starts long after the file it would be matched against.
_PIDFILE_START_TOLERANCE = 60.0


def _pidfile_still_names_its_process(pid: int, pidfile: Optional[Path]) -> bool:
    """True only when *pid* can still be the process *pidfile* was written for.

    ``check_pidfile`` verifies identity via the flock probe only. A non-Python
    daemon (``ollama``) or one that died without its file being cleaned falls
    through to a bare liveness check, which a **recycled** PID passes — and
    after a reboot with ``.koan-pid-*`` files still on disk that PID is
    typically an unrelated session leader. Escalating to ``killpg`` on that
    basis takes a stranger's whole session down, so it is allowed only against
    the same start-time proof :func:`app.mission_scope.stop_registered_scopes`
    demands of its own PID records.
    """
    if pidfile is None:
        return False
    try:
        written_at = pidfile.stat().st_mtime
    except OSError:
        return False
    from app.mission_scope import _process_start_time
    started_at = _process_start_time(pid)
    if started_at is None:
        return False
    return started_at <= written_at + _PIDFILE_START_TOLERANCE


def _signal_process(pid: int, sig: int, pidfile: Optional[Path] = None) -> bool:
    """Signal *pid*'s process group, falling back to the bare PID.

    Returns False only when the target is already gone. Kōan starts every
    daemon with ``start_new_session=True``, so the daemon leads its own group
    and ``killpg`` reaches its children too; the ``pgid != pid`` fallback covers
    a pid file written by something that is not a group leader.

    The group is signalled only when *pidfile* still verifiably names *pid* —
    a destructive action verifies its target first. Anything unconfirmed
    degrades to the single-PID ``os.kill`` that predates group signalling, so a
    stale pid file costs one wrong signal rather than a stranger's session.

    The caller's own group is never signalled, and that is structural rather
    than incidental: if a daemon ever calls ``stop_processes`` over a set that
    includes itself, ``killpg`` would take the caller down mid-sweep and leave
    the remaining daemons running with their pid files intact. Mirrors the
    guard in :func:`app.subprocess_runner.kill_orphaned_process_group`.

    Only ``ProcessLookupError`` means "already gone". Every other ``OSError``
    (notably EPERM, from a group member running under different credentials)
    means the group signal could not be delivered, not that the daemon is dead —
    reporting False there would make ``stop_processes`` record ``not_running``,
    skip its SIGKILL escalation and leave the daemon live, where the single-PID
    ``os.kill`` this function predates would have stopped it. So those fall
    through to the bare PID instead.
    """
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    except OSError:
        pgid = None
    if (
        pgid == pid
        and pgid != os.getpgrp()
        and _pidfile_still_names_its_process(pid, pidfile)
    ):
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            pass
    try:
        os.kill(pid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def stop_processes(koan_root: Path, timeout: float = 5.0) -> dict:
    """Stop all running Kōan processes (run + awake + ollama).

    Detects and boots out launchd services first (prevents respawn), tears down
    any live mission cgroup scope, then signals each running process's group
    (SIGTERM, then SIGKILL on timeout). Creates .koan-stop signal file for
    graceful shutdown.

    Signalling the *group* rather than the bare PID from the pid file is what
    makes ``make stop`` actually stop: everything Kōan spawns is started with
    ``start_new_session=True``, so each daemon leads its own group, and the old
    single-PID SIGTERM left every descendant (the provider CLI and whatever it
    started) running. A daemon that detached further still needs the mission
    scope, which is why that is stopped first.

    Returns dict mapping process name to result: "stopped", "not_running",
    or "force_killed".
    """
    results = {}

    # Create .koan-stop signal file for graceful run loop shutdown
    from app.utils import atomic_write
    stop_file = koan_root / STOP_FILE
    atomic_write(stop_file, "STOP")

    # Notify Telegram before killing processes
    any_running = any(check_pidfile(koan_root, n) for n in PROCESS_NAMES)
    if any_running:
        try:
            from app.notify import send_telegram
            from app.notify_dedup import NOTICE_DEDUP_WINDOW_SECONDS
            # Dedup across incarnations (#2426): a repeated stop+start / restart
            # loop must not re-announce the same shutdown line each time.
            send_telegram(
                "🛑 Shutting down — operator requested stop.",
                dedup_window=NOTICE_DEDUP_WINDOW_SECONDS,
            )
        except Exception as e:
            print(f"[pid_manager] stop notification failed: {e}", file=sys.stderr)

    # Bootout any launchd-managed services first to prevent respawn
    for name in PROCESS_NAMES:
        _bootout_launchd_service(name)

    # Stop any live mission scope before the daemons: a build daemon that
    # re-parented to PID 1 with its own session is outside every process group
    # here, so only its cgroup can take it down.
    try:
        from app.mission_scope import stop_registered_scopes
        stopped_scopes = stop_registered_scopes(str(koan_root))
        if stopped_scopes:
            print(f"[pid_manager] stopped mission scope(s): "
                  f"{', '.join(stopped_scopes)}")
    except Exception as e:
        print(f"[pid_manager] mission scope teardown failed: {e}", file=sys.stderr)

    for name in PROCESS_NAMES:
        pid = check_pidfile(koan_root, name)
        if not pid:
            results[name] = "not_running"
            continue
        pidfile = _pidfile_path(koan_root, name)

        # Send SIGTERM to the whole group so children die with the daemon —
        # but only to a group whose leader the pid file still vouches for.
        if not _signal_process(pid, signal.SIGTERM, pidfile):
            results[name] = "not_running"
            continue

        # Wait for process to exit
        if _wait_for_exit(pid, timeout):
            results[name] = "stopped"
        else:
            # Force kill
            _signal_process(pid, signal.SIGKILL, pidfile)
            # Wait briefly for SIGKILL to take effect
            _wait_for_exit(pid, 1.0)
            results[name] = "force_killed"

        # Clean up PID file
        pidfile.unlink(missing_ok=True)

    return results


def stop_process(koan_root: Path, name: str, timeout: float = 5.0) -> str:
    """Stop a single named Kōan process (SIGTERM, then SIGKILL on timeout).

    Returns "stopped", "not_running", or "force_killed". Used by the terminal
    dashboard's web-dashboard toggle to bring just that process down.
    """
    _bootout_launchd_service(name)
    pid = check_pidfile(koan_root, name)
    if not pid:
        return "not_running"
    pidfile = _pidfile_path(koan_root, name)
    if not _signal_process(pid, signal.SIGTERM, pidfile):
        return "not_running"
    if _wait_for_exit(pid, timeout):
        result = "stopped"
    else:
        _signal_process(pid, signal.SIGKILL, pidfile)
        _wait_for_exit(pid, 1.0)
        result = "force_killed"
    pidfile.unlink(missing_ok=True)
    return result


def _print_stack_results(results: dict) -> int:
    """Print stack start results and return exit code (0=ok, 1=failure)."""
    any_failed = False
    for name in ("ollama", "awake", "run", "dashboard", "api", "mcp"):
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
        # Explicit operator command — assert ollama intent (the Railway guard in
        # start_ollama still applies). Auto paths use the resolved provider.
        ok, msg = start_ollama(root, provider="ollama")
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
