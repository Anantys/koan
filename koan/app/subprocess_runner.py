"""Subprocess execution primitives — kill, watchdog, liveness.

Consolidates the duplicated timeout/capture/teardown logic that was
spread across ``run.py``, ``cli_exec.py``, and ``provider/__init__.py``.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional


def kill_process_group(
    proc: Optional[subprocess.Popen],
    graceful_timeout: float = 3,
    force_timeout: float = 5,
) -> None:
    """Terminate a subprocess and its entire process group.

    Sends SIGTERM to the process group, waits *graceful_timeout* seconds,
    then escalates to SIGKILL if the process is still alive.  Requires the
    subprocess to have been started with ``start_new_session=True``.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=force_timeout)
            except subprocess.TimeoutExpired:
                print(
                    f"[subprocess_runner] warning: pid {proc.pid} "
                    f"did not exit after SIGKILL",
                    file=sys.stderr,
                )
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_process_group_by_pid(
    pid: int,
    graceful_timeout: float = 3,
    force_timeout: float = 5,
) -> bool:
    """SIGTERM then SIGKILL a process group named by *pid*, with no ``Popen``.

    :func:`kill_process_group` needs the ``Popen`` so it can ``wait()`` on the
    child.  Callers holding only a PID read from a file (the mission-scope
    registry) are usually not the parent, so liveness is polled instead.

    Only signals a group *led* by *pid* — which ``start_new_session=True``
    guarantees for everything Kōan spawns — so a stale PID file can never make
    this signal the caller's own group.

    Returns whether the group is *confirmed* gone, so a caller holding the only
    durable handle on it can keep that handle when it is not.
    """
    if pid <= 0:
        return False
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        # The leader is gone, but a group outlives its leader and the id can no
        # longer be recovered — nothing here can confirm the group is empty.
        return False
    except (PermissionError, OSError):
        return False
    if pgid != pid:
        return False
    return kill_orphaned_process_group(pgid, graceful_timeout, force_timeout)


def kill_orphaned_process_group(
    pgid: int,
    graceful_timeout: float = 3,
    force_timeout: float = 5,
) -> bool:
    """SIGTERM then SIGKILL every member of *pgid*, whose leader may be gone.

    A process group outlives its leader: children left behind by a mission that
    exited cleanly are still in it, and they are exactly what
    :func:`kill_process_group` cannot touch — it returns at its ``poll()`` guard
    the moment the leader is reaped.  So the group id must have been captured
    while the leader was alive; ``os.getpgid`` cannot recover it afterwards.

    Liveness is polled with ``killpg(pgid, 0)``, which reports the *group* empty
    rather than merely the leader dead.

    Returns True **only** when the group is confirmed empty.  Only
    ``ProcessLookupError`` proves that.  A ``PermissionError`` (a descendant
    that changed credentials), any other ``OSError``, a group that outlived
    SIGKILL, or a pgid that was never captured all mean containment could not
    be verified — on the fallback path this is the only containment lever there
    is, so an unverified sweep is reported rather than mistaken for a clean
    one.
    """
    if pgid <= 1 or pgid == os.getpgrp():
        # Never signal init's group or our own — either takes down far more than
        # the mission. A group id below 2 means the capture failed, not that
        # there is something to kill, and a failed capture is not containment.
        return False
    for sig, wait_for in ((signal.SIGTERM, graceful_timeout),
                          (signal.SIGKILL, force_timeout)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except OSError as exc:
            print(
                f"[subprocess_runner] warning: process group {pgid} could not "
                f"be signalled ({exc}) — descendants may survive",
                file=sys.stderr,
            )
            return False
        deadline = time.monotonic() + wait_for
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            except OSError as exc:
                print(
                    f"[subprocess_runner] warning: process group {pgid} could "
                    f"not be probed ({exc}) — descendants may survive",
                    file=sys.stderr,
                )
                return False
            time.sleep(0.1)
    print(
        f"[subprocess_runner] warning: process group {pgid} "
        f"did not exit after SIGKILL",
        file=sys.stderr,
    )
    return False


def force_kill_process_group(proc: Optional[subprocess.Popen]) -> None:
    """SIGKILL a process group immediately, with single-process fallback.

    Used by watchdog timers where graceful shutdown is not worth the delay.
    No poll() guard — the exception handler catches already-dead processes.
    """
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(OSError, ProcessLookupError):
            proc.kill()


class ProcessWatchdog:
    """Watchdog timer that kills a process group after *timeout* seconds.

    A ``completed`` flag with a lock closes the race between the stream loop
    finishing and the Timer firing — preventing spurious kills on clean exits.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        timeout: float,
        on_timeout: Optional[Callable[[], None]] = None,
        graceful: bool = True,
    ):
        self._proc = proc
        self._timeout = timeout
        self._on_timeout = on_timeout
        self._graceful = graceful
        self._fired = False
        self._completed = False
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def start(self) -> "ProcessWatchdog":
        self._timer = threading.Timer(self._timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def mark_completed(self) -> None:
        with self._lock:
            self._completed = True

    @property
    def fired(self) -> bool:
        return self._fired

    def _fire(self) -> None:
        with self._lock:
            if self._completed:
                return
            self._fired = True

        if self._on_timeout:
            self._on_timeout()

        if self._graceful:
            kill_process_group(self._proc)
        else:
            force_kill_process_group(self._proc)


class LivenessWatchdog:
    """Watchdog that resets on each heartbeat, kills on inactivity.

    Each call to :meth:`heartbeat` restarts the countdown.  If no heartbeat
    arrives within *timeout* seconds the process group is killed.

    ``graceful=False`` (mirroring :class:`ProcessWatchdog`) SIGKILLs the whole
    group immediately instead of the SIGTERM-then-escalate path. The graceful
    path stops escalating as soon as the *leader* exits, so a descendant that
    handles or ignores SIGTERM survives it. Callers whose liveness depends on
    the group releasing an inherited pipe must pass ``graceful=False``: a
    survivor holding the write end keeps the reader blocked forever, which is
    the exact hang the watchdog was armed to end.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        timeout: float,
        on_timeout: Optional[Callable[[], None]] = None,
        graceful: bool = True,
    ):
        self._proc = proc
        self._timeout = timeout
        self._on_timeout = on_timeout
        self._graceful = graceful
        self._fired = False
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def start(self) -> "LivenessWatchdog":
        self._schedule()
        return self

    def heartbeat(self) -> None:
        with self._lock:
            if self._fired:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._schedule_locked()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()

    @property
    def fired(self) -> bool:
        return self._fired

    def _schedule(self) -> None:
        with self._lock:
            self._schedule_locked()

    def _schedule_locked(self) -> None:
        self._timer = threading.Timer(self._timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self._fired = True

        if self._on_timeout:
            self._on_timeout()

        if self._graceful:
            kill_process_group(self._proc)
        else:
            force_kill_process_group(self._proc)
