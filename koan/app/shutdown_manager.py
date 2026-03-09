"""Shutdown signal management for Kōan processes.

Manages the .koan-shutdown file that signals both the agent loop (run.py)
and the messaging bridge (awake.py) to exit cleanly.

Unlike /stop (which only stops run.py after the current mission), /shutdown
terminates both processes.

Staleness protection: the shutdown file contains the UNIX timestamp of when
the shutdown was requested. Each process records its own start time and only
honors a shutdown signal if it was issued AFTER the process started. This
prevents a leftover shutdown file from killing a freshly started instance.
"""

import os
import time
from pathlib import Path

from app.signals import SHUTDOWN_FILE


def request_shutdown(koan_root: str) -> None:
    """Create the shutdown signal file with the current timestamp."""
    from app.utils import atomic_write
    atomic_write(Path(koan_root, SHUTDOWN_FILE), str(int(time.time())))


def is_shutdown_requested(koan_root: str, process_start_time: float) -> bool:
    """Check if a valid (non-stale) shutdown has been requested.

    Args:
        koan_root: Path to koan root directory.
        process_start_time: UNIX timestamp of when the calling process started.

    Returns:
        True if a shutdown was requested after the process started.
    """
    path = os.path.join(koan_root, SHUTDOWN_FILE)
    if not os.path.isfile(path):
        return False

    try:
        with open(path) as f:
            shutdown_time = int(f.read().strip())
    except (OSError, ValueError):
        return False

    if shutdown_time >= int(process_start_time):
        return True

    # Stale shutdown file (predates this process) — clean it up
    clear_shutdown(koan_root)
    return False


def clear_shutdown(koan_root: str) -> None:
    """Remove the shutdown signal file."""
    path = os.path.join(koan_root, SHUTDOWN_FILE)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
