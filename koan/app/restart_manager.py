"""Restart signal management for Kōan processes.

Provides file-based restart signaling between bridge and run loop.

Two consumers (bridge and runner) each get their own marker so a fast
wrapper-restart of the runner can no longer wipe the signal before the
bridge's polling tick sees it.

The restart flow:
1. ``request_restart`` writes ``.koan-restart-bridge`` and ``.koan-restart-run``.
2. Bridge's main loop notices ``.koan-restart-bridge`` and re-execs via
   ``os.execv`` (same PID, fresh interpreter).
3. Runner's main loop notices ``.koan-restart-run`` and exits with
   ``RESTART_EXIT_CODE``; its wrapper relaunches it.
4. Each process clears only its own marker on startup, so neither can
   silence the signal for the other.

Forced restart (``/restart --force``): the markers additionally carry a
``force`` line and the runner is sent SIGUSR2. The runner then kills the
in-flight mission and exits with ``RESTART_EXIT_CODE`` immediately instead
of waiting for the mission to finish; crash recovery re-queues the killed
mission on the next startup (or fails it, if it has already used up
``max_crash_retries``). The bridge needs nothing extra — it re-execs
on its next poll tick either way.

Legacy ``.koan-restart`` (DEPRECATED): the single combined marker is no
longer *written* by Kōan. It is read by nothing in-tree (both consumers poll
their own per-process marker), so writing it was a no-op that lingered on disk.
``check_restart``/``clear_restart`` still accept ``target=None`` → ``.koan-restart``
purely so any out-of-tree script polling the old path keeps working; remove that
mapping once you are certain no external consumer depends on it. All in-tree
restart triggers (run loop, bridge, auto-update, REST API, dashboard) now go
through ``request_restart`` so both consumer markers are written and the restart
actually fires.

Exit code 42 is the restart sentinel — any other exit is a real stop.
"""

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Optional

from app.signals import RESTART_FILE
RESTART_EXIT_CODE = 42

# Per-consumer marker files. The legacy ``RESTART_FILE`` (``.koan-restart``)
# is DEPRECATED: no longer written by ``request_restart``. The ``None``
# entry below is retained only so ``check_restart``/``clear_restart`` keep
# honouring ``target=None`` for any out-of-tree caller still polling the old
# path; nothing in-tree reads or writes it.
RESTART_BRIDGE_FILE = ".koan-restart-bridge"
RESTART_RUN_FILE = ".koan-restart-run"

# Body line that marks a request as forced (``/restart --force``).
FORCE_MARKER = "force"

# One-shot guard so an unreadable marker logs once, not every poll tick.
_force_read_error_logged = False

# Files written by request_restart() — the two live per-consumer markers only.
_WRITE_TARGETS = (RESTART_BRIDGE_FILE, RESTART_RUN_FILE)

_TARGET_FILES = {
    "bridge": RESTART_BRIDGE_FILE,
    "run": RESTART_RUN_FILE,
    None: RESTART_FILE,  # deprecated, read-only compat (see module docstring)
}


def _marker_path(koan_root: str, target: Optional[str]) -> str:
    try:
        fname = _TARGET_FILES[target]
    except KeyError as exc:
        raise ValueError(
            f"Unknown restart target {target!r}; "
            f"expected one of {sorted(k for k in _TARGET_FILES if k)!r} or None"
        ) from exc
    return os.path.join(koan_root, fname)


def request_restart(koan_root: str, force: bool = False) -> None:
    """Create restart signal files for both consumers.

    Writes the two per-consumer markers (``.koan-restart-bridge`` and
    ``.koan-restart-run``) so each consumer can clear its own without
    silencing the other. The deprecated legacy ``.koan-restart`` is no
    longer written — nothing reads it.

    Args:
        koan_root: Root path for the koan installation.
        force: Mark the request as forced (``/restart --force``). A forced
            marker tells the runner to kill an in-flight mission instead of
            waiting for it to finish — see :func:`is_force_restart`.
    """
    from app.utils import atomic_write

    body = f"restart requested at {time.strftime('%H:%M:%S')}\n"
    if force:
        body += f"{FORCE_MARKER}\n"
    for fname in _WRITE_TARGETS:
        atomic_write(Path(koan_root) / fname, body)


def is_force_restart(koan_root: str, target: str, since: float = 0) -> bool:
    """Return True when the pending restart request for ``target`` is forced.

    Durability fallback for the SIGUSR2 fast path: if the signal never
    reached the runner (stale PID file, runner mid-restart), the in-mission
    poll loop still sees the forced marker and kills the mission.

    Args:
        koan_root: Root path for the koan installation.
        target: ``"bridge"`` or ``"run"`` — required, mirroring
            :func:`check_restart`'s per-consumer markers. The deprecated
            legacy marker is not a forced-restart carrier.
        since: If > 0, ignore markers not modified after this timestamp, so a
            marker left over from a previous incarnation cannot force a
            restart. Also short-circuits the read on every poll tick.

    A missing marker is the normal case. Any *other* read failure (EACCES on
    a marker written by a differently-privileged path, EIO on the mount)
    silently disables this fallback, so it is logged — once per process, since
    the mission loop calls this every poll tick.
    """
    global _force_read_error_logged
    path = _marker_path(koan_root, target)
    try:
        if since > 0 and os.path.getmtime(path) <= since:
            return False
        with open(path, encoding="utf-8") as fh:
            return any(line.strip() == FORCE_MARKER for line in fh)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if not _force_read_error_logged:
            _force_read_error_logged = True
            from app.run_log import log
            log("error", f"Cannot read restart marker for forced restart: {exc}")
        return False


def check_restart(
    koan_root: str,
    since: float = 0,
    target: Optional[str] = None,
) -> bool:
    """Check if a restart has been requested for ``target``.

    Args:
        koan_root: Root path for the koan installation.
        since: If > 0, only return True if the marker was modified after
            this timestamp.  Used to ignore stale restart signals left
            over from a previous process incarnation (prevents restart
            loops when Telegram re-delivers the /restart message).
        target: ``"bridge"`` or ``"run"`` to check the per-consumer
            marker.  ``None`` (default) checks the legacy single marker
            for backward compatibility.
    """
    restart_file = _marker_path(koan_root, target)
    if not os.path.isfile(restart_file):
        return False
    try:
        if since > 0 and os.path.getmtime(restart_file) <= since:
            return False
    except OSError:
        return False
    return True


def clear_restart(koan_root: str, target: Optional[str] = None) -> None:
    """Remove the restart signal file for ``target``.

    A consumer should only clear its own marker so the other consumer
    can still observe the request on its next poll tick.
    """
    path = _marker_path(koan_root, target)
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def reexec_bridge() -> None:
    """Re-exec the current Python process (bridge self-restart).

    Uses os.execv() to replace the current process with a fresh one.
    Same PID, same terminal, same file descriptors — clean restart.
    """
    python = sys.executable
    args = [python] + sys.argv
    os.execv(python, args)
