"""Kōan — Session manager for parallel agent execution.

Manages parallel Claude Code sessions, each running in its own git worktree:
- Session dataclass: tracks individual session state
- SessionRegistry: persistent session tracking via sessions.json (fcntl-locked)
- spawn_session(): create worktree + start Claude subprocess
- poll_sessions(): check subprocess status, collect results
- kill_session(): terminate a session and clean up
- get_max_parallel_sessions(): read config

The registry file (instance/sessions.json) follows Koan's existing pattern
of file-based state with fcntl locks for cross-process safety.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.worktree_manager import (
    create_worktree,
    inject_worktree_claude_md,
    prune_worktrees,
    remove_worktree,
    setup_shared_deps,
)


# Default configuration
DEFAULT_MAX_PARALLEL = 2
MAX_PARALLEL_CAP = 5
SESSIONS_FILE = "sessions.json"


@dataclass
class Session:
    """Represents a single parallel agent session."""
    id: str
    mission_text: str
    project_name: str
    project_path: str
    worktree_path: str
    branch_name: str
    pid: int = 0
    status: str = "pending"  # pending | running | done | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    exit_code: int = -1
    stdout_file: str = ""
    stderr_file: str = ""


@dataclass
class SessionResult:
    """Result from a completed session."""
    session: Session
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class SessionRegistry:
    """Persistent session tracking via sessions.json with file locking.

    Thread-safe and process-safe via fcntl.flock.
    """

    def __init__(self, instance_dir: str):
        self.instance_dir = instance_dir
        self._path = Path(instance_dir) / SESSIONS_FILE
        self._lock = threading.Lock()

    def _lock_path(self) -> Path:
        return self._path.with_suffix(".lock")

    def _read_unlocked(self) -> Dict[str, dict]:
        """Read sessions.json without acquiring a lock (caller holds it)."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_unlocked(self, data: Dict[str, dict]):
        """Write sessions.json atomically (caller holds the file lock)."""
        fd, tmp = tempfile.mkstemp(
            dir=self.instance_dir, prefix=".koan-sessions-",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_locked(self) -> Dict[str, dict]:
        """Read sessions.json under a shared file lock."""
        if not self._path.exists():
            return {}
        try:
            with open(self._lock_path(), "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_SH)
                try:
                    return self._read_unlocked()
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError:
            return {}

    def _mutate(self, fn):
        """Run fn(data) → data under exclusive file lock (atomic read-modify-write)."""
        with self._lock:
            with open(self._lock_path(), "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    data = self._read_unlocked()
                    data = fn(data)
                    self._write_unlocked(data)
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)

    def register(self, session: Session):
        """Register a new session."""
        def _add(data):
            data[session.id] = asdict(session)
            return data
        self._mutate(_add)

    def update(self, session: Session):
        """Update an existing session."""
        def _upd(data):
            data[session.id] = asdict(session)
            return data
        self._mutate(_upd)

    def remove(self, session_id: str):
        """Remove a session from the registry."""
        def _rm(data):
            data.pop(session_id, None)
            return data
        self._mutate(_rm)

    def get(self, session_id: str) -> Optional[Session]:
        """Get a session by ID."""
        data = self._read_locked()
        entry = data.get(session_id)
        if entry:
            return _dict_to_session(entry)
        return None

    def get_all(self) -> List[Session]:
        """Get all registered sessions."""
        data = self._read_locked()
        return [_dict_to_session(v) for v in data.values()]

    def get_active(self) -> List[Session]:
        """Get sessions with status 'running'."""
        return [s for s in self.get_all() if s.status == "running"]

    def get_by_project(self, project_name: str) -> List[Session]:
        """Get active sessions for a specific project (case-insensitive)."""
        lower = project_name.lower()
        return [
            s for s in self.get_active()
            if s.project_name.lower() == lower
        ]

    def clear_completed(self):
        """Remove all done/failed sessions from the registry."""
        def _clear(data):
            return {
                k: v for k, v in data.items()
                if v.get("status") in ("pending", "running")
            }
        self._mutate(_clear)


def _dict_to_session(d: dict) -> Session:
    """Convert a dict to a Session, ignoring unknown keys."""
    known = {f.name for f in Session.__dataclass_fields__.values()}
    filtered = {k: v for k, v in d.items() if k in known}
    return Session(**filtered)


def get_max_parallel_sessions() -> int:
    """Read max_parallel_sessions from config.yaml (default: 2, max: 5)."""
    try:
        from app.utils import load_config
        config = load_config()
        n = int(config.get("max_parallel_sessions", DEFAULT_MAX_PARALLEL))
        return max(1, min(n, MAX_PARALLEL_CAP))
    except Exception as e:
        print(f"[session_manager] config read error: {e}", file=sys.stderr)
        return DEFAULT_MAX_PARALLEL


def spawn_session(
    mission_text: str,
    project_name: str,
    project_path: str,
    instance_dir: str,
    registry: SessionRegistry,
    autonomous_mode: str = "implement",
    base_branch: str = "main",
    shared_deps: Optional[List[str]] = None,
) -> Session:
    """Create a worktree and start a Claude Code subprocess for a mission.

    Args:
        mission_text: The mission to execute.
        project_name: Project name.
        project_path: Path to the project repository.
        instance_dir: Path to the instance directory.
        registry: SessionRegistry for tracking.
        autonomous_mode: Mode for tool selection.
        base_branch: Branch to base the worktree on.
        shared_deps: Dependency dirs to symlink.

    Returns:
        Session with subprocess started and registered.
    """
    from app.mission_runner import build_mission_command

    # Create worktree
    wt = create_worktree(project_path, base_branch=base_branch)

    # Setup shared dependencies
    if shared_deps:
        setup_shared_deps(wt.path, project_path, shared_deps)

    # Inject mission context into worktree CLAUDE.md
    inject_worktree_claude_md(wt.path, mission_text)

    # Build CLI command
    cmd = build_mission_command(
        prompt=mission_text,
        autonomous_mode=autonomous_mode,
        project_name=project_name,
    )

    # Create temp files for stdout/stderr
    fd_out, stdout_file = tempfile.mkstemp(prefix=f"koan-session-{wt.session_id}-out-")
    os.close(fd_out)
    fd_err, stderr_file = tempfile.mkstemp(prefix=f"koan-session-{wt.session_id}-err-")
    os.close(fd_err)

    # Create session
    session = Session(
        id=wt.session_id,
        mission_text=mission_text,
        project_name=project_name,
        project_path=project_path,
        worktree_path=wt.path,
        branch_name=wt.branch,
        status="running",
        started_at=time.time(),
        stdout_file=stdout_file,
        stderr_file=stderr_file,
    )

    # Start subprocess — file handles must outlive the process
    from app.cli_exec import popen_cli

    out_f = None
    err_f = None
    try:
        out_f = open(stdout_file, "w")  # noqa: SIM115
        err_f = open(stderr_file, "w")  # noqa: SIM115
        proc, cli_cleanup = popen_cli(
            cmd,
            stdout=out_f,
            stderr=err_f,
            cwd=wt.path,
            start_new_session=True,
        )
    except Exception:
        if err_f:
            err_f.close()
        if out_f:
            out_f.close()
        raise
    session.pid = proc.pid

    # Wrap cleanup to also close file handles after process exits
    def _session_cleanup():
        cli_cleanup()
        out_f.close()
        err_f.close()

    # Store cleanup and proc as transient state (not persisted)
    session._proc = proc  # type: ignore[attr-defined]
    session._cleanup = _session_cleanup  # type: ignore[attr-defined]

    # Register in persistent store
    registry.register(session)

    return session


def poll_sessions(
    sessions: List[Session],
    registry: SessionRegistry,
) -> List[SessionResult]:
    """Check subprocess status for active sessions, collect completed ones.

    Args:
        sessions: List of running sessions (must have _proc attribute).
        registry: SessionRegistry for updating state.

    Returns:
        List of SessionResult for newly completed sessions.
    """
    completed = []

    for session in sessions:
        proc = getattr(session, "_proc", None)
        if proc is None:
            continue

        exit_code = proc.poll()
        if exit_code is None:
            continue  # Still running

        # Session completed
        session.status = "done" if exit_code == 0 else "failed"
        session.exit_code = exit_code
        session.finished_at = time.time()

        # Call cleanup
        cleanup = getattr(session, "_cleanup", None)
        if cleanup:
            try:
                cleanup()
            except Exception as e:
                print(f"[session_manager] cleanup error for session {session.id}: {e}", file=sys.stderr)

        # Collect output
        stdout = ""
        stderr = ""
        try:
            if session.stdout_file and Path(session.stdout_file).exists():
                stdout = Path(session.stdout_file).read_text()
        except OSError:
            pass
        try:
            if session.stderr_file and Path(session.stderr_file).exists():
                stderr = Path(session.stderr_file).read_text()
        except OSError:
            pass

        # Update registry
        registry.update(session)

        completed.append(SessionResult(
            session=session,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        ))

    return completed


def kill_session(
    session: Session,
    registry: SessionRegistry,
):
    """Terminate a session's subprocess and clean up its worktree.

    Args:
        session: The session to kill.
        registry: SessionRegistry for updating state.
    """
    # Kill subprocess
    proc = getattr(session, "_proc", None)
    if proc is not None and proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif session.pid > 0:
        # No proc reference — try killing by PID
        try:
            os.kill(session.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # Call cleanup
    cleanup = getattr(session, "_cleanup", None)
    if cleanup:
        try:
            cleanup()
        except Exception as e:
            print(f"[session_manager] cleanup error for session {session.id}: {e}", file=sys.stderr)

    # Update session state
    session.status = "failed"
    session.finished_at = time.time()
    session.exit_code = -1
    registry.update(session)

    # Clean up worktree
    try:
        remove_worktree(
            session.project_path,
            session_id=session.id,
            force=True,
        )
    except Exception as e:
        print(f"[session_manager] worktree removal error for session {session.id}: {e}", file=sys.stderr)

    # Clean up temp files
    for path in (session.stdout_file, session.stderr_file):
        try:
            if path:
                Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def kill_all_sessions(registry: SessionRegistry):
    """Kill all active sessions."""
    for session in registry.get_active():
        kill_session(session, registry)


def recover_stale_sessions(registry: SessionRegistry):
    """Clean up sessions whose processes are no longer alive.

    Called on startup to handle crash recovery. Also prunes stale
    git worktree references that may have accumulated.
    """
    # Prune stale worktree refs across all projects with active sessions
    pruned_projects: set = set()
    for session in registry.get_all():
        if session.project_path and session.project_path not in pruned_projects:
            pruned_projects.add(session.project_path)
            prune_worktrees(session.project_path)

    for session in registry.get_active():
        if session.pid <= 0:
            session.status = "failed"
            session.finished_at = time.time()
            registry.update(session)
            continue

        try:
            os.kill(session.pid, 0)  # Check if process exists
        except ProcessLookupError:
            # Process gone — mark as failed and clean up
            session.status = "failed"
            session.finished_at = time.time()
            registry.update(session)
            try:
                remove_worktree(
                    session.project_path,
                    session_id=session.id,
                    force=True,
                )
            except Exception as e:
                print(f"[session_manager] stale worktree cleanup error for session {session.id}: {e}", file=sys.stderr)
        except PermissionError:
            pass  # Process exists but we can't signal it — leave it
