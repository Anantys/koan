"""Restart signal management for Kōan processes.

Provides file-based restart signaling between bridge and run loop:
- Bridge creates .koan-restart to signal both processes
- run.py checks at loop start and exits with code 42
- Bridge detects the signal and re-execs itself via os.execv()

The restart flow:
1. User sends /restart on Telegram
2. Bridge writes .koan-restart
3. Bridge sends ack to Telegram
4. Bridge re-execs itself (os.execv replaces process in-place)
5. run.py detects .koan-restart at next iteration, exits with code 42
6. Wrapper re-launches run.py

Exit code 42 is the restart sentinel — any other exit is a real stop.
"""

import os
import sys
import time
from pathlib import Path

from app.signals import RESTART_FILE
RESTART_EXIT_CODE = 42


def request_restart(koan_root: str) -> None:
    """Create the restart signal file.

    Both processes check for this file:
    - run.py: at loop start, exits with code 42
    - awake.py: in main loop, triggers os.execv()
    """
    from app.utils import atomic_write

    atomic_write(
        Path(koan_root) / RESTART_FILE,
        f"restart requested at {time.strftime('%H:%M:%S')}\n",
    )


def check_restart(koan_root: str, since: float = 0) -> bool:
    """Check if a restart has been requested.

    Args:
        koan_root: Root path for the koan installation.
        since: If > 0, only return True if the file was modified after this
               timestamp.  Used to ignore stale restart signals left over
               from a previous process incarnation (prevents restart loops
               when Telegram re-delivers the /restart message).
    """
    restart_file = os.path.join(koan_root, RESTART_FILE)
    if not os.path.isfile(restart_file):
        return False
    try:
        if since > 0 and os.path.getmtime(restart_file) <= since:
            return False
    except OSError:
        return False
    return True


def clear_restart(koan_root: str) -> None:
    """Remove the restart signal file."""
    path = os.path.join(koan_root, RESTART_FILE)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def reexec_bridge() -> None:
    """Re-exec the current Python process (bridge self-restart).

    Uses os.execv() to replace the current process with a fresh one.
    Same PID, same terminal, same file descriptors — clean restart.
    """
    python = sys.executable
    args = [python] + sys.argv
    os.execv(python, args)
