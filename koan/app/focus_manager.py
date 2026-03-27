"""Kōan — Focus Mode Manager

Manages the .koan-focus file that controls whether the agent loop should
skip contemplative sessions and free exploration, focusing exclusively on
queued missions.

Focus state format:
  .koan-focus — JSON file:
    activated_at: UNIX timestamp when focus was activated
    duration: duration in seconds (default 5h = 18000)
    reason: human-readable reason

When focus mode is active:
  - Contemplative sessions (random reflection rolls) are skipped
  - The agent prompt is modified to discourage free exploration
  - The agent only picks up queued missions from missions.md
"""

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.signals import FOCUS_FILE

# Default focus duration: 5 hours
DEFAULT_FOCUS_DURATION = 5 * 60 * 60


@dataclass
class FocusState:
    """Represents the current focus state."""

    activated_at: int
    duration: int
    reason: str

    @property
    def expires_at(self) -> int:
        return self.activated_at + self.duration

    def is_expired(self, now: Optional[int] = None) -> bool:
        if now is None:
            now = int(time.time())
        return now >= self.expires_at

    def remaining_seconds(self, now: Optional[int] = None) -> int:
        if now is None:
            now = int(time.time())
        remaining = self.expires_at - now
        return max(0, remaining)

    def remaining_display(self, now: Optional[int] = None) -> str:
        remaining = self.remaining_seconds(now)
        if remaining <= 0:
            return "expired"
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m"


def _focus_path(koan_root: str) -> Path:
    return Path(koan_root) / FOCUS_FILE


def get_focus_state(koan_root: str) -> Optional[FocusState]:
    """Read the current focus state from .koan-focus.

    Returns None if not focused or file doesn't exist.
    """
    path = _focus_path(koan_root)
    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    try:
        return FocusState(
            activated_at=int(data.get("activated_at", 0)),
            duration=int(data.get("duration", DEFAULT_FOCUS_DURATION)),
            reason=str(data.get("reason", "")),
        )
    except (TypeError, ValueError):
        return None


def create_focus(
    koan_root: str,
    duration: int = DEFAULT_FOCUS_DURATION,
    reason: str = "missions",
) -> FocusState:
    """Activate focus mode.

    Args:
        koan_root: Path to koan root directory
        duration: Focus duration in seconds (default 5h)
        reason: Human-readable reason

    Returns:
        The created FocusState
    """
    now = int(time.time())
    state = FocusState(activated_at=now, duration=duration, reason=reason)
    data = {
        "activated_at": state.activated_at,
        "duration": state.duration,
        "reason": state.reason,
    }

    from app.utils import atomic_write

    atomic_write(_focus_path(koan_root), json.dumps(data))

    return state


def remove_focus(koan_root: str) -> None:
    """Deactivate focus mode."""
    _focus_path(koan_root).unlink(missing_ok=True)


def check_focus(koan_root: str) -> Optional[FocusState]:
    """Check focus state, auto-removing if expired.

    Returns the active FocusState, or None if not focused or expired.
    """
    state = get_focus_state(koan_root)
    if state is None:
        return None
    if state.is_expired():
        remove_focus(koan_root)
        return None
    return state


def parse_duration(text: str) -> Optional[int]:
    """Parse a human-readable duration string into seconds.

    Supports: "5h", "3h30m", "2h", "90m", "30m", bare number (hours).
    Returns None if parsing fails.
    """
    text = text.strip().lower()
    if not text:
        return None

    # Bare number = hours
    try:
        hours = float(text)
        result = int(hours * 3600)
        return result if result > 0 else None
    except ValueError:
        pass

    # Parse NhMm format
    total = 0
    remaining = text

    # Extract hours
    if "h" in remaining:
        parts = remaining.split("h", 1)
        try:
            total += int(parts[0]) * 3600
        except ValueError:
            return None
        remaining = parts[1]

    # Extract minutes
    if "m" in remaining:
        parts = remaining.split("m", 1)
        try:
            total += int(parts[0]) * 60
        except ValueError:
            return None
    elif remaining.strip():
        # Leftover text after 'h' that's not 'm' — try as minutes
        try:
            total += int(remaining) * 60
        except ValueError:
            return None

    return total if total > 0 else None


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: focus_manager.py <command> <koan_root> [args...]",
            file=sys.stderr,
        )
        print("Commands:", file=sys.stderr)
        print(
            "  check <root>     - Check focus (exit 0=focused, 1=not focused)",
            file=sys.stderr,
        )
        print(
            "  status <root>    - Print focus status as JSON",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1]
    koan_root = sys.argv[2]

    if cmd == "check":
        state = check_focus(koan_root)
        if state:
            print(state.remaining_display())
            sys.exit(0)
        else:
            sys.exit(1)

    elif cmd == "status":
        state = get_focus_state(koan_root)
        if state and not state.is_expired():
            result = {
                "focused": True,
                "remaining": state.remaining_display(),
                "reason": state.reason,
                "expires_at": state.expires_at,
            }
        else:
            result = {"focused": False}
        print(json.dumps(result))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
