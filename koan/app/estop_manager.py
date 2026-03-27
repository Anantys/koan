"""
Kōan -- Emergency Stop (E-stop) State Manager

Manages the .koan-estop and .koan-estop-state files that provide a graduated
safety override for the agent loop.

Unlike pause (operational: quota, cooldown, manual), e-stop is a safety
mechanism with graduated restriction levels. It never auto-resumes —
always requires explicit human /resume.

Signal files:
  .koan-estop        — existence = e-stopped (empty file, gate signal)
  .koan-estop-state  — JSON file with rich state:
    {
      "level": "full" | "readonly" | "project_freeze",
      "reason": "human-provided reason",
      "timestamp": 1234567890,
      "frozen_projects": ["project1", "project2"],
      "triggered_by": "telegram"
    }

E-stop levels:
  FULL           — halt immediately, kill running subprocess
  READONLY       — allow missions but restrict tools to read-only set
  PROJECT_FREEZE — block specific projects while others continue
"""

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


ESTOP_SIGNAL_FILE = ".koan-estop"
ESTOP_STATE_FILE = ".koan-estop-state"

# Read-only tool set for READONLY level (same as REVIEW mode)
READONLY_TOOLS = ["Read", "Glob", "Grep"]


class EstopLevel(Enum):
    """E-stop severity levels."""
    FULL = "full"
    READONLY = "readonly"
    PROJECT_FREEZE = "project_freeze"


@dataclass
class EstopState:
    """Represents the current e-stop state."""
    level: EstopLevel
    reason: str
    timestamp: int
    frozen_projects: List[str] = field(default_factory=list)
    triggered_by: str = "telegram"

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "frozen_projects": self.frozen_projects,
            "triggered_by": self.triggered_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EstopState":
        try:
            level = EstopLevel(data.get("level", "full"))
        except ValueError:
            level = EstopLevel.FULL
        return cls(
            level=level,
            reason=data.get("reason", ""),
            timestamp=data.get("timestamp", 0),
            frozen_projects=data.get("frozen_projects", []),
            triggered_by=data.get("triggered_by", "telegram"),
        )


def is_estopped(koan_root: str) -> bool:
    """Check if the e-stop signal file exists."""
    return os.path.isfile(os.path.join(koan_root, ESTOP_SIGNAL_FILE))


def get_estop_state(koan_root: str) -> Optional[EstopState]:
    """Read the current e-stop state from .koan-estop-state.

    Returns None if not e-stopped or no state file exists.
    If the state file is corrupt, returns a FULL stop state (fail-safe).
    """
    if not is_estopped(koan_root):
        return None

    state_file = os.path.join(koan_root, ESTOP_STATE_FILE)
    if not os.path.isfile(state_file):
        # Signal file exists but no state — fail-safe to FULL
        return EstopState(
            level=EstopLevel.FULL,
            reason="unknown (missing state file)",
            timestamp=0,
        )

    try:
        with open(state_file) as f:
            data = json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        # Corrupt state file — fail-safe to FULL
        return EstopState(
            level=EstopLevel.FULL,
            reason="unknown (corrupt state file)",
            timestamp=0,
        )

    return EstopState.from_dict(data)


def activate_estop(
    koan_root: str,
    level: EstopLevel,
    reason: str = "",
    frozen_projects: Optional[List[str]] = None,
    triggered_by: str = "telegram",
) -> EstopState:
    """Activate the e-stop with the given level.

    For PROJECT_FREEZE: if an estop is already active at PROJECT_FREEZE level,
    adds the new frozen projects to the existing list instead of replacing.

    Args:
        koan_root: Path to koan root directory.
        level: E-stop severity level.
        reason: Human-readable reason for the e-stop.
        frozen_projects: List of project names to freeze (PROJECT_FREEZE only).
        triggered_by: Who triggered the e-stop.

    Returns:
        The created/updated EstopState.
    """
    from app.utils import atomic_write

    if frozen_projects is None:
        frozen_projects = []

    # For PROJECT_FREEZE, merge with existing frozen projects
    if level == EstopLevel.PROJECT_FREEZE:
        existing = get_estop_state(koan_root)
        if existing and existing.level == EstopLevel.PROJECT_FREEZE:
            merged = list(existing.frozen_projects)
            for p in frozen_projects:
                if p not in merged:
                    merged.append(p)
            frozen_projects = merged

    state = EstopState(
        level=level,
        reason=reason,
        timestamp=int(time.time()),
        frozen_projects=frozen_projects,
        triggered_by=triggered_by,
    )

    # Write state file FIRST (so it's ready before the signal file)
    state_path = Path(koan_root) / ESTOP_STATE_FILE
    atomic_write(state_path, json.dumps(state.to_dict(), indent=2))

    # Create the signal file atomically
    signal_path = Path(koan_root) / ESTOP_SIGNAL_FILE
    atomic_write(signal_path, "")

    return state


def deactivate_estop(koan_root: str) -> None:
    """Remove both e-stop files, fully clearing the e-stop.

    Order: remove state file first (informational), then signal file (gate).
    """
    for name in (ESTOP_STATE_FILE, ESTOP_SIGNAL_FILE):
        path = os.path.join(koan_root, name)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def unfreeze_project(koan_root: str, project_name: str) -> Optional[EstopState]:
    """Remove a single project from the frozen list.

    If the frozen list becomes empty, deactivates the e-stop entirely.

    Returns:
        The updated EstopState, or None if e-stop was fully deactivated.
    """
    state = get_estop_state(koan_root)
    if state is None or state.level != EstopLevel.PROJECT_FREEZE:
        return None

    remaining = [p for p in state.frozen_projects if p != project_name]
    if not remaining:
        deactivate_estop(koan_root)
        return None

    # Deactivate first to clear old state, then re-activate with updated list
    deactivate_estop(koan_root)
    return activate_estop(
        koan_root,
        level=EstopLevel.PROJECT_FREEZE,
        reason=state.reason,
        frozen_projects=remaining,
        triggered_by=state.triggered_by,
    )


def is_project_frozen(koan_root: str, project_name: str) -> bool:
    """Check if a specific project is frozen by the e-stop."""
    state = get_estop_state(koan_root)
    if state is None:
        return False
    if state.level != EstopLevel.PROJECT_FREEZE:
        return False
    return project_name in state.frozen_projects


def get_estop_tools() -> list:
    """Return the read-only tool set for READONLY e-stop level."""
    return list(READONLY_TOOLS)
