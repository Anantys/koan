#!/usr/bin/env python3
"""
Kōan -- Pause State Manager

Manages the .koan-pause file that controls the agent loop's pause/resume
behavior.

Pause state is a **single atomic file** (.koan-pause):
  - Existence = paused
  - Content = 3-line reason data:
      line 1: reason (e.g., "quota", "max_runs", "manual")
      line 2: timestamp (UNIX epoch — reset time for quota, pause time for max_runs)
      line 3: display info (human-readable, e.g., "resets 10am (Europe/Paris)")

Previous versions used two files (.koan-pause + .koan-pause-reason) with
non-atomic two-step writes.  The single-file design eliminates orphan state
that could permanently block the agent.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.signals import PAUSE_FILE

# Default cooldown for non-quota pauses (max_runs, manual)
DEFAULT_COOLDOWN_SECONDS = 5 * 60 * 60  # 5 hours

# Retry interval for quota pauses when reset time is unknown.
# Shorter than DEFAULT_COOLDOWN_SECONDS to discover quota resets faster.
QUOTA_RETRY_SECONDS = 3600  # 1 hour


@dataclass
class PauseState:
    """Represents the current pause state."""

    reason: str  # "quota", "max_runs", "timed", "manual", or other
    timestamp: int  # Reset time (quota/timed) or pause time (max_runs)
    display: str  # Human-readable info

    @property
    def is_quota(self) -> bool:
        return self.reason == "quota"

    @property
    def is_timed(self) -> bool:
        return self.reason == "timed"


def parse_duration(text: str) -> Optional[int]:
    """Parse a duration string like '2h', '30m', '1h30m' into seconds.

    Returns None if the text cannot be parsed or the duration is zero.
    """
    text = text.strip().lower()
    if not text:
        return None

    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", text)
    if not match or not any(match.groups()):
        return None

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    total = hours * 3600 + minutes * 60
    return total if total > 0 else None


def is_paused(koan_root: str) -> bool:
    """Check if the pause file exists."""
    return os.path.isfile(os.path.join(koan_root, PAUSE_FILE))


def get_pause_state(koan_root: str) -> Optional[PauseState]:
    """
    Read the current pause state from .koan-pause.

    Returns None if not paused or the file has no parseable content.
    """
    pause_file = os.path.join(koan_root, PAUSE_FILE)
    if not os.path.isfile(pause_file):
        return None

    try:
        with open(pause_file) as f:
            content = f.read().strip()
    except OSError:
        return None

    if not content:
        # Empty .koan-pause (legacy or touch-created) — paused but no reason.
        return None

    lines = content.splitlines()
    reason = lines[0].strip()
    if not reason:
        return None

    timestamp = 0
    display = ""

    if len(lines) >= 2:
        try:
            timestamp = int(lines[1].strip())
        except (ValueError, IndexError):
            timestamp = 0

    if len(lines) >= 3:
        display = lines[2].strip()

    return PauseState(reason=reason, timestamp=timestamp, display=display)


def should_auto_resume(state: PauseState, now: Optional[int] = None) -> bool:
    """
    Determine if auto-resume conditions are met.

    For manual pauses: never auto-resume (only /resume clears them).
    For quota pauses: resume when current time >= reset timestamp.
    For timed pauses: resume when current time >= resume timestamp.
    For max_runs/other: resume after DEFAULT_COOLDOWN_SECONDS (5h).
    """
    if state.reason == "manual":
        return False

    if now is None:
        now = int(time.time())

    if state.is_quota or state.is_timed:
        # Quota/timed: resume when the stored timestamp is reached
        return state.timestamp > 0 and now >= state.timestamp
    else:
        # Non-quota: resume after 5h cooldown from pause time
        if state.timestamp <= 0:
            return False
        elapsed = now - state.timestamp
        return elapsed >= DEFAULT_COOLDOWN_SECONDS


def create_pause(
    koan_root: str,
    reason: str,
    timestamp: Optional[int] = None,
    display: str = "",
) -> None:
    """
    Create pause state atomically (single file write).

    Args:
        koan_root: Path to koan root directory
        reason: Pause reason ("quota", "max_runs", "manual", etc.)
        timestamp: Reset time (quota) or pause time (max_runs).
                   Defaults to current time.
        display: Human-readable display info
    """
    from app.utils import atomic_write

    if timestamp is None:
        timestamp = int(time.time())

    pause_file = Path(koan_root) / PAUSE_FILE
    content = f"{reason}\n{timestamp}\n{display}\n"
    atomic_write(pause_file, content)


def remove_pause(koan_root: str) -> None:
    """Remove the pause file (single atomic delete)."""
    try:
        os.remove(os.path.join(koan_root, PAUSE_FILE))
    except FileNotFoundError:
        pass


def check_and_resume(koan_root: str) -> Optional[str]:
    """
    Check if paused and if auto-resume conditions are met.

    Returns:
        A resume message if auto-resumed, None if still paused or not paused.
        The caller should notify the user with the returned message.

    Side effects:
        Removes the pause file if auto-resuming.
    """
    state = get_pause_state(koan_root)
    if state is None:
        # Empty or unparseable .koan-pause — stay paused (safe default).
        # The user can always /resume manually.
        return None

    if not should_auto_resume(state):
        return None

    # Auto-resume: remove pause file
    remove_pause(koan_root)

    if state.is_quota:
        return f"quota reset time reached ({state.display})"
    elif state.is_timed:
        return f"timed pause expired ({state.display})"
    else:
        return f"5h have passed since pause ({state.reason})"


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: pause_manager.py <command> <koan_root> [args...]",
            file=sys.stderr,
        )
        print("Commands:", file=sys.stderr)
        print(
            "  check <root>       - Check auto-resume (exit 0=resumed, 1=still paused)",
            file=sys.stderr,
        )
        print(
            "  status <root>      - Print pause status as JSON",
            file=sys.stderr,
        )
        print(
            "  create <root> <reason> [timestamp] [display]  - Create pause",
            file=sys.stderr,
        )
        print("  remove <root>      - Remove pause", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    koan_root = sys.argv[2]

    if cmd == "check":
        # Check for auto-resume, print message if resumed
        resume_msg = check_and_resume(koan_root)
        if resume_msg:
            print(resume_msg)
            sys.exit(0)
        else:
            sys.exit(1)

    elif cmd == "status":
        # Print pause state as JSON
        state = get_pause_state(koan_root)
        if state:
            result = {
                "paused": True,
                "reason": state.reason,
                "timestamp": state.timestamp,
                "display": state.display,
            }
        else:
            result = {"paused": is_paused(koan_root), "reason": "", "timestamp": 0, "display": ""}
        print(json.dumps(result))

    elif cmd == "create":
        if len(sys.argv) < 4:
            print("Usage: pause_manager.py create <root> <reason> [timestamp] [display]", file=sys.stderr)
            sys.exit(1)
        reason = sys.argv[3]
        timestamp = int(sys.argv[4]) if len(sys.argv) > 4 else None
        display = sys.argv[5] if len(sys.argv) > 5 else ""
        create_pause(koan_root, reason, timestamp, display)

    elif cmd == "remove":
        remove_pause(koan_root)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
