#!/usr/bin/env python3
"""
Kōan — Health check

Monitors heartbeats for the Telegram bridge (awake.py) and the run loop (run.py).
awake.py writes a timestamp to .koan-heartbeat every poll cycle.
run.py writes a timestamp to .koan-run-heartbeat every iteration and during sleep.
This module checks staleness and alerts via notify.py if a component is down.

Usage from shell:
    python3 health_check.py /path/to/koan_root [--max-age 60]

Exit codes:
    0 = healthy (or no heartbeat file yet — first run)
    1 = stale heartbeat (bridge or run loop likely down)
    2 = usage error
"""

import sys
import time
from pathlib import Path

from app.notify import format_and_send
from app.signals import HEARTBEAT_FILE, RUN_HEARTBEAT_FILE
from app.utils import atomic_write
DEFAULT_MAX_AGE = 60  # seconds
# Run loop heartbeat is written once per iteration (~minutes apart),
# so a longer max age is appropriate. 10 minutes covers normal idle periods.
DEFAULT_RUN_MAX_AGE = 600  # seconds


def write_heartbeat(koan_root: str) -> None:
    """Write current timestamp to heartbeat file. Called by awake.py."""
    path = Path(koan_root) / HEARTBEAT_FILE
    atomic_write(path, str(time.time()))


def check_heartbeat(koan_root: str, max_age: int = DEFAULT_MAX_AGE) -> bool:
    """Check if the heartbeat is fresh.

    Returns True if healthy (fresh or no file yet), False if stale.
    """
    path = Path(koan_root) / HEARTBEAT_FILE
    if not path.exists():
        # No heartbeat file = bridge never started or first run. Not an error.
        return True

    try:
        ts = float(path.read_text().strip())
    except (ValueError, OSError):
        return False

    age = time.time() - ts
    return age <= max_age


def check_and_alert(koan_root: str, max_age: int = DEFAULT_MAX_AGE) -> bool:
    """Check heartbeat and send alert if stale. Returns True if healthy."""
    if check_heartbeat(koan_root, max_age):
        return True

    path = Path(koan_root) / HEARTBEAT_FILE
    try:
        ts = float(path.read_text().strip())
        age_minutes = (time.time() - ts) / 60
        format_and_send(
            f"Telegram bridge (awake.py) appears down — "
            f"last heartbeat {age_minutes:.0f} min ago."
        )
    except (ValueError, OSError):
        format_and_send("Telegram bridge (awake.py) appears down — heartbeat file unreadable.")

    return False


# --- Run loop heartbeat ---


def write_run_heartbeat(koan_root: str) -> None:
    """Write current timestamp to run heartbeat file. Called by run.py."""
    path = Path(koan_root) / RUN_HEARTBEAT_FILE
    atomic_write(path, str(time.time()))


def check_run_heartbeat(koan_root: str, max_age: int = DEFAULT_RUN_MAX_AGE) -> bool:
    """Check if the run loop heartbeat is fresh.

    Returns True if healthy (fresh or no file yet), False if stale.
    """
    path = Path(koan_root) / RUN_HEARTBEAT_FILE
    if not path.exists():
        return True

    try:
        ts = float(path.read_text().strip())
    except (ValueError, OSError):
        return False

    age = time.time() - ts
    return age <= max_age


def get_run_heartbeat_age(koan_root: str) -> float:
    """Return age of the run heartbeat in seconds, or -1 if no file."""
    path = Path(koan_root) / RUN_HEARTBEAT_FILE
    if not path.exists():
        return -1
    try:
        ts = float(path.read_text().strip())
        return time.time() - ts
    except (ValueError, OSError):
        return -1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <koan_root> [--max-age SECONDS]", file=sys.stderr)
        sys.exit(2)

    root = sys.argv[1]
    max_age = DEFAULT_MAX_AGE

    if "--max-age" in sys.argv:
        idx = sys.argv.index("--max-age")
        if idx + 1 < len(sys.argv):
            try:
                max_age = int(sys.argv[idx + 1])
            except ValueError:
                print(f"Invalid max-age value: {sys.argv[idx + 1]}", file=sys.stderr)
                sys.exit(2)

    bridge_healthy = check_and_alert(root, max_age)
    bridge_status = "healthy" if bridge_healthy else "STALE"
    print(f"[health] Bridge: {bridge_status}")

    run_healthy = check_run_heartbeat(root)
    run_status = "healthy" if run_healthy else "STALE"
    run_age = get_run_heartbeat_age(root)
    if run_age >= 0:
        print(f"[health] Run loop: {run_status} (age: {run_age:.0f}s)")
    else:
        print(f"[health] Run loop: no heartbeat file")

    all_healthy = bridge_healthy and run_healthy
    sys.exit(0 if all_healthy else 1)
