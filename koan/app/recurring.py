#!/usr/bin/env python3
"""
Kōan -- Recurring missions

Manages recurring missions (hourly, daily, weekly) stored in instance/recurring.json.
The scheduler checks which missions are due and inserts them into missions.md pending
section for normal execution by the run loop.

Storage format (recurring.json):
[
    {
        "id": "rec_1706990000",
        "frequency": "daily",
        "text": "summarize unread emails",
        "project": null,
        "created": "2026-02-03T22:00:00",
        "last_run": null,
        "enabled": true,
        "at": null
    },
    ...
]

The optional "at" field (e.g. "20:00") restricts when the mission fires:
  - daily: fires once per day, but only at or after the specified time
  - weekly: fires once per week, but only at or after the specified time
  - hourly: "at" is ignored (hourly already fires every hour)
"""

import fcntl
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, TypeVar

from app.utils import atomic_write, insert_pending_mission

T = TypeVar("T")


FREQUENCIES = ("hourly", "daily", "weekly", "every")

# Regex for parsing "HH:MM" at the start of mission text
import re
_AT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+")
# Regex for parsing interval strings like "5m", "2h", "1h30m", "90s"
_INTERVAL_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def load_recurring(recurring_path: Path) -> List[Dict]:
    """Load recurring missions from JSON file.

    Returns empty list if file doesn't exist or is invalid.
    """
    if not recurring_path.exists():
        return []
    try:
        data = json.loads(recurring_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, OSError):
        return []


def save_recurring(recurring_path: Path, missions: List[Dict]):
    """Save recurring missions to JSON file atomically."""
    content = json.dumps(missions, ensure_ascii=False, indent=2) + "\n"
    atomic_write(recurring_path, content)


def _locked_modify(recurring_path: Path, fn: Callable[[List[Dict]], T]) -> T:
    """Acquire a file lock, load recurring missions, apply *fn*, and save.

    Returns whatever *fn* returns.
    """
    lock_path = recurring_path.parent / ".recurring.lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            missions = load_recurring(recurring_path)
            result = fn(missions)
            save_recurring(recurring_path, missions)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def parse_at_time(text: str) -> tuple:
    """Extract an optional HH:MM prefix from mission text.

    Returns (at_time_str_or_None, remaining_text).
    E.g. "20:00 check emails" -> ("20:00", "check emails")
         "check emails"       -> (None, "check emails")

    Raises:
        ValueError: If time format is present but invalid (e.g. 25:00).
    """
    match = _AT_TIME_RE.match(text.strip())
    if not match:
        return None, text.strip()
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time: {match.group(1)}:{match.group(2)}. Use HH:MM (00:00–23:59).")
    at_str = f"{hour:02d}:{minute:02d}"
    remaining = text.strip()[match.end():]
    return at_str, remaining.strip()


def parse_interval(text: str) -> int:
    """Parse an interval string like "5m", "2h", "1h30m" into seconds.

    Supported units: h (hours), m (minutes), s (seconds).
    Minimum interval: 1 minute (60 seconds).

    Returns:
        Interval in seconds.

    Raises:
        ValueError: If format is invalid or interval is too short.
    """
    text = text.strip().lower()
    match = _INTERVAL_RE.match(text)
    if not match or text == "":
        raise ValueError(f"Invalid interval: '{text}'. Use format like 5m, 2h, 1h30m.")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if total < 60:
        raise ValueError("Minimum interval is 1 minute (1m).")
    return total


def format_interval(seconds: int) -> str:
    """Format seconds back to a human-readable interval string."""
    if seconds < 60:
        return f"{seconds}s"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def add_recurring_interval(
    recurring_path: Path,
    interval_seconds: int,
    interval_display: str,
    text: str,
    project: Optional[str] = None,
) -> Dict:
    """Add a recurring mission with a custom interval.

    Args:
        recurring_path: Path to recurring.json
        interval_seconds: Interval in seconds (minimum 60)
        interval_display: Human-readable interval (e.g. "5m")
        text: Mission description
        project: Optional project name

    Returns:
        The created mission dict
    """
    created_mission = {}

    def _add(missions: List[Dict]) -> Dict:
        mission = {
            "id": f"rec_{int(time.time())}_{os.getpid()}_{len(missions)}",
            "frequency": "every",
            "interval_seconds": interval_seconds,
            "interval_display": interval_display,
            "text": text.strip(),
            "project": project,
            "created": datetime.now().isoformat(timespec="seconds"),
            "last_run": None,
            "enabled": True,
            "at": None,
        }
        missions.append(mission)
        created_mission.update(mission)
        return mission

    _locked_modify(recurring_path, _add)
    return created_mission


def add_recurring(
    recurring_path: Path,
    frequency: str,
    text: str,
    project: Optional[str] = None,
    at: Optional[str] = None,
) -> Dict:
    """Add a new recurring mission.

    Args:
        recurring_path: Path to recurring.json
        frequency: One of "hourly", "daily", "weekly"
        text: Mission description
        project: Optional project name
        at: Optional time of day "HH:MM" (for daily/weekly)

    Returns:
        The created mission dict

    Raises:
        ValueError: If frequency is invalid
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"Invalid frequency: {frequency}. Must be one of {FREQUENCIES}")

    created_mission = {}

    def _add(missions: List[Dict]) -> Dict:
        mission = {
            "id": f"rec_{int(time.time())}_{os.getpid()}_{len(missions)}",
            "frequency": frequency,
            "text": text.strip(),
            "project": project,
            "created": datetime.now().isoformat(timespec="seconds"),
            "last_run": None,
            "enabled": True,
            "at": at,
        }
        missions.append(mission)
        created_mission.update(mission)
        return mission

    _locked_modify(recurring_path, _add)
    return created_mission


def remove_recurring(recurring_path: Path, identifier: str) -> str:
    """Remove a recurring mission by number (1-indexed) or keyword.

    Args:
        recurring_path: Path to recurring.json
        identifier: Number (1-indexed) or keyword substring

    Returns:
        Description of the removed mission

    Raises:
        ValueError: If identifier doesn't match any mission
    """
    def _remove(missions: List[Dict]) -> str:
        if not missions:
            raise ValueError("No recurring missions configured.")

        enabled = [m for m in missions if m.get("enabled", True)]
        if not enabled:
            raise ValueError("No active recurring missions.")

        if identifier.isdigit():
            idx = int(identifier) - 1
            if idx < 0 or idx >= len(enabled):
                raise ValueError(
                    f"Invalid number: {identifier}. "
                    f"Valid range: 1-{len(enabled)}"
                )
            target = enabled[idx]
        else:
            # Keyword match (case-insensitive substring)
            matches = [
                m for m in enabled
                if identifier.lower() in m["text"].lower()
            ]
            if not matches:
                raise ValueError(f"No recurring mission matching '{identifier}'.")
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple matches for '{identifier}'. Be more specific or use a number."
                )
            target = matches[0]

        # Remove from list (mutate in place so _locked_modify saves correctly)
        missions[:] = [m for m in missions if m["id"] != target["id"]]
        return f"[{target['frequency']}] {target['text']}"

    return _locked_modify(recurring_path, _remove)


def list_recurring(recurring_path: Path) -> List[Dict]:
    """List all enabled recurring missions.

    Returns list of mission dicts, sorted by frequency (hourly, daily, weekly).
    """
    missions = load_recurring(recurring_path)
    enabled = [m for m in missions if m.get("enabled", True)]
    freq_order = {"every": 0, "hourly": 1, "daily": 2, "weekly": 3}
    return sorted(enabled, key=lambda m: freq_order.get(m["frequency"], 99))


def format_recurring_list(missions: List[Dict]) -> str:
    """Format recurring missions for display.

    Returns a human-readable string with numbered entries.
    """
    if not missions:
        return "No recurring missions configured."

    lines = ["Recurring missions:"]
    for i, m in enumerate(missions, 1):
        freq = m["frequency"]
        text = m["text"]
        project = m.get("project")
        last_run = m.get("last_run")

        at = m.get("at")
        if freq == "every":
            interval_display = m.get("interval_display") or format_interval(m.get("interval_seconds", 0))
            entry = f"  {i}. [every {interval_display}] {text}"
        elif at:
            entry = f"  {i}. [{freq} at {at}] {text}"
        else:
            entry = f"  {i}. [{freq}] {text}"
        if project:
            entry += f" (project: {project})"
        if last_run:
            # Show relative time
            try:
                last_dt = datetime.fromisoformat(last_run)
                delta = datetime.now() - last_dt
                if delta.total_seconds() < 3600:
                    entry += f" — last: {int(delta.total_seconds() / 60)}min ago"
                elif delta.total_seconds() < 86400:
                    entry += f" — last: {int(delta.total_seconds() / 3600)}h ago"
                else:
                    entry += f" — last: {int(delta.days)}d ago"
            except (ValueError, TypeError):
                entry += f" — last: {last_run}"
        else:
            entry += " — never run"

        lines.append(entry)

    return "\n".join(lines)


def _past_at_time(at: Optional[str], now: datetime) -> bool:
    """Check if current time is at or past the scheduled "HH:MM".

    Returns True if no ``at`` is set (always eligible).
    """
    if not at:
        return True
    try:
        parts = at.split(":")
        target_hour, target_minute = int(parts[0]), int(parts[1])
        return (now.hour, now.minute) >= (target_hour, target_minute)
    except (ValueError, IndexError):
        return True  # Malformed at = ignore constraint


def is_due(mission: Dict, now: Optional[datetime] = None) -> bool:
    """Check if a recurring mission is due for execution.

    Rules:
        - hourly: last_run is None or > 1 hour ago
        - daily: last_run is None or last_run date != today
        - weekly: last_run is None or > 7 days ago

    When ``at`` is set (e.g. "20:00"), daily/weekly missions additionally
    require the current time to be at or past that hour. This allows
    scheduling a daily task to run in the evening without restricting the
    whole agent schedule.
    """
    if not mission.get("enabled", True):
        return False

    now = now or datetime.now()
    last_run = mission.get("last_run")
    at = mission.get("at")

    if last_run is None:
        # Never run — still respect the "at" constraint for daily/weekly
        if at and mission["frequency"] in ("daily", "weekly"):
            return _past_at_time(at, now)
        return True

    try:
        last_dt = datetime.fromisoformat(last_run)
    except (ValueError, TypeError):
        return True  # Invalid date = treat as never run

    frequency = mission["frequency"]

    if frequency == "every":
        interval_seconds = mission.get("interval_seconds", 0)
        if interval_seconds <= 0:
            return True  # Misconfigured — run immediately
        return (now - last_dt) >= timedelta(seconds=interval_seconds)
    elif frequency == "hourly":
        return (now - last_dt) >= timedelta(hours=1)
    elif frequency == "daily":
        if last_dt.date() >= now.date():
            return False  # Already ran today
        return _past_at_time(at, now)
    elif frequency == "weekly":
        if (now - last_dt) < timedelta(weeks=1):
            return False  # Not yet a week
        return _past_at_time(at, now)

    return False


def check_and_inject(
    recurring_path: Path,
    missions_path: Path,
    now: Optional[datetime] = None,
) -> List[str]:
    """Check all recurring missions and inject due ones into missions.md.

    This is the main scheduler entry point. Called from run.py at the top
    of each loop iteration.

    Args:
        recurring_path: Path to recurring.json
        missions_path: Path to missions.md
        now: Optional datetime for testing

    Returns:
        List of mission descriptions that were injected
    """
    now = now or datetime.now()

    def _check(missions: List[Dict]) -> List[str]:
        if not missions:
            return []

        injected = []
        for mission in missions:
            if not is_due(mission, now):
                continue

            text = mission["text"]
            project = mission.get("project")
            freq = mission["frequency"]

            # Build mission entry for missions.md
            if freq == "every":
                interval_display = mission.get("interval_display") or format_interval(mission.get("interval_seconds", 0))
                tag = f"[every {interval_display}] "
            else:
                tag = f"[{freq}] "
            if project:
                entry = f"- [project:{project}] {tag}{text}"
            else:
                entry = f"- {tag}{text}"

            # Insert into pending section
            insert_pending_mission(missions_path, entry)

            # Update last_run
            mission["last_run"] = now.isoformat(timespec="seconds")
            injected.append(f"[{freq}] {text}")

        return injected

    return _locked_modify(recurring_path, _check)
