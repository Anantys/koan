"""Kōan — Passive Mode Manager

Manages the .koan-passive file that controls whether the agent loop should
skip all execution (missions, exploration, contemplation) while keeping the
loop alive for heartbeat, GitHub notification polling, and Telegram commands.

Passive state format:
  .koan-passive — JSON file:
    activated_at: UNIX timestamp when passive was activated
    duration: duration in seconds (0 = indefinite)
    reason: human-readable reason

When passive mode is active:
  - No missions are executed (they stay Pending in missions.md)
  - No autonomous exploration or contemplative sessions
  - No Claude CLI calls, no branch switching, no code changes
  - GitHub notifications still get converted to missions (queued only)
  - Telegram commands still work
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.signals import PASSIVE_FILE


@dataclass
class PassiveState:
    """Represents the current passive state."""

    activated_at: int
    duration: int  # 0 = indefinite
    reason: str

    @property
    def expires_at(self) -> Optional[int]:
        if self.duration == 0:
            return None
        return self.activated_at + self.duration

    def is_expired(self, now: Optional[int] = None) -> bool:
        if self.duration == 0:
            return False  # indefinite — never expires
        if now is None:
            now = int(time.time())
        return now >= self.activated_at + self.duration

    def remaining_seconds(self, now: Optional[int] = None) -> int:
        if self.duration == 0:
            return -1  # indefinite
        if now is None:
            now = int(time.time())
        remaining = self.activated_at + self.duration - now
        return max(0, remaining)

    def remaining_display(self, now: Optional[int] = None) -> str:
        remaining = self.remaining_seconds(now)
        if remaining < 0:
            return "indefinite"
        if remaining == 0:
            return "expired"
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m"


def _passive_path(koan_root: str) -> Path:
    return Path(koan_root) / PASSIVE_FILE


def is_passive(koan_root: str) -> bool:
    """Check if passive mode is active (convenience boolean)."""
    return check_passive(koan_root) is not None


def get_passive_state(koan_root: str) -> Optional[PassiveState]:
    """Read the current passive state from .koan-passive.

    Returns None if not passive or file doesn't exist.
    Does NOT auto-remove expired state (use check_passive for that).
    """
    path = _passive_path(koan_root)
    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    try:
        return PassiveState(
            activated_at=int(data.get("activated_at", 0)),
            duration=int(data.get("duration", 0)),
            reason=str(data.get("reason", "")),
        )
    except (TypeError, ValueError):
        return None


def create_passive(
    koan_root: str,
    duration: int = 0,
    reason: str = "manual",
) -> PassiveState:
    """Activate passive mode.

    Args:
        koan_root: Path to koan root directory
        duration: Passive duration in seconds (0 = indefinite)
        reason: Human-readable reason

    Returns:
        The created PassiveState
    """
    now = int(time.time())
    state = PassiveState(activated_at=now, duration=duration, reason=reason)
    data = {
        "activated_at": state.activated_at,
        "duration": state.duration,
        "reason": state.reason,
    }

    from app.utils import atomic_write

    atomic_write(_passive_path(koan_root), json.dumps(data))

    return state


def remove_passive(koan_root: str) -> None:
    """Deactivate passive mode."""
    _passive_path(koan_root).unlink(missing_ok=True)


def check_passive(koan_root: str) -> Optional[PassiveState]:
    """Check passive state, auto-removing if expired.

    Returns the active PassiveState, or None if not passive or expired.
    """
    state = get_passive_state(koan_root)
    if state is None:
        return None
    if state.is_expired():
        remove_passive(koan_root)
        return None
    return state
