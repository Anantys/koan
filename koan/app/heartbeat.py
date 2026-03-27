"""
Kōan — Heartbeat checks for periodic background health monitoring.

Runs during interruptible sleep in the agent loop. Provides:
- Stale mission detection (In Progress missions with no journal activity)
- Disk space monitoring

All checks are pure Python file operations — no API calls, no subprocess.
"""

import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List

from app.missions import parse_sections
from app.utils import parse_project


# --- Stale mission detection ---

# Default threshold: 2 hours without journal activity = stale
STALE_MISSION_HOURS = 2

# Track which missions we've already alerted on (avoids spam)
_alerted_stale_missions: set = set()

# Throttle: minimum seconds between stale mission checks
STALE_CHECK_INTERVAL = 1800  # 30 minutes
_last_stale_check: float = None


def reset_stale_state() -> None:
    """Reset module-level state. Used by tests."""
    global _alerted_stale_missions, _last_stale_check
    _alerted_stale_missions = set()
    _last_stale_check = None


def check_stale_missions(
    instance_dir: str, max_age_hours: float = STALE_MISSION_HOURS
) -> List[str]:
    """Detect In Progress missions with no recent journal activity.

    Only flags simple missions (- lines), not complex multi-step missions
    (### headers), matching recover.py's distinction.

    Args:
        instance_dir: Path to instance directory.
        max_age_hours: Hours without journal activity before flagging.

    Returns:
        List of stale mission descriptions (already-alerted ones excluded).
    """
    missions_path = Path(instance_dir) / "missions.md"
    if not missions_path.exists():
        return []

    try:
        content = missions_path.read_text()
    except OSError:
        return []

    sections = parse_sections(content)
    in_progress = sections.get("in_progress", [])
    if not in_progress:
        return []

    max_age_seconds = max_age_hours * 3600
    now = time.time()
    stale = []

    for mission in in_progress:
        stripped = mission.strip()
        # Skip complex missions (### headers) — they can legitimately run for days
        if stripped.startswith("### "):
            continue
        # Skip non-mission lines
        if not stripped.startswith("- "):
            continue

        # Check if already alerted
        if stripped in _alerted_stale_missions:
            continue

        # Check journal activity for this mission's project
        project_name, _ = parse_project(stripped)
        last_activity = _get_last_journal_activity(instance_dir, project_name)

        if last_activity < 0:
            # No journal files at all — can't determine staleness
            continue

        if (now - last_activity) > max_age_seconds:
            _alerted_stale_missions.add(stripped)
            # Clean display: strip "- " prefix
            display = stripped[2:] if stripped.startswith("- ") else stripped
            stale.append(display)

    return stale


def _get_last_journal_activity(instance_dir: str, project_name: str = None) -> float:
    """Get the mtime of the most recent journal file.

    Checks pending.md and today's journal directory for recent writes.

    Returns:
        Most recent mtime as a Unix timestamp, or -1 if no journal files.
    """
    journal_dir = Path(instance_dir) / "journal"
    if not journal_dir.exists():
        return -1

    mtimes = []

    # Check pending.md (written during active runs)
    pending = journal_dir / "pending.md"
    if pending.exists():
        try:
            mtimes.append(pending.stat().st_mtime)
        except OSError:
            pass

    # Check today's journal directory
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = journal_dir / today
    if today_dir.is_dir():
        try:
            for f in today_dir.iterdir():
                if f.is_file():
                    # If we have a project name, prioritize its journal file
                    if project_name and f.stem == project_name:
                        mtimes.append(f.stat().st_mtime)
                    elif not project_name:
                        mtimes.append(f.stat().st_mtime)
            # Always include any file if we didn't find the project-specific one
            if not mtimes:
                for f in today_dir.iterdir():
                    if f.is_file():
                        mtimes.append(f.stat().st_mtime)
        except OSError:
            pass

    return max(mtimes) if mtimes else -1


def run_stale_mission_check(instance_dir: str) -> List[str]:
    """Throttled stale mission check. Returns list of newly-stale missions.

    Called from interruptible_sleep() every check cycle. Only performs
    the actual check every STALE_CHECK_INTERVAL seconds.
    """
    global _last_stale_check

    now = time.monotonic()
    if _last_stale_check is not None and now - _last_stale_check < STALE_CHECK_INTERVAL:
        return []
    _last_stale_check = now

    stale = check_stale_missions(instance_dir)
    if stale:
        _send_stale_alert(stale)
    return stale


def _send_stale_alert(stale_missions: List[str]) -> None:
    """Send a Telegram notification about stale missions."""
    try:
        from app.notify import send_telegram, NotificationPriority
        count = len(stale_missions)
        header = f"⚠️ {count} mission(s) appear stale (no journal activity for >{STALE_MISSION_HOURS}h):"
        details = "\n".join(f"  • {m[:80]}" for m in stale_missions)
        send_telegram(f"{header}\n{details}\n\nUse /cancel to remove or /list to review.",
                      priority=NotificationPriority.WARNING)
    except (ImportError, OSError):
        pass


# --- Disk space monitoring ---

# Default threshold: warn below 1 GB
DISK_SPACE_WARN_GB = 1.0

# Track whether we've already alerted this session
_disk_space_alerted: bool = False


def reset_disk_state() -> None:
    """Reset module-level state. Used by tests."""
    global _disk_space_alerted
    _disk_space_alerted = False


def check_disk_space(koan_root: str, warn_threshold_gb: float = DISK_SPACE_WARN_GB) -> bool:
    """Check available disk space on the KOAN_ROOT partition.

    Returns True if space is sufficient, False if below threshold.
    """
    try:
        usage = shutil.disk_usage(koan_root)
        free_gb = usage.free / (1024 ** 3)
        return free_gb >= warn_threshold_gb
    except OSError:
        return True  # Can't check — assume OK


def get_disk_free_gb(koan_root: str) -> float:
    """Return free disk space in GB, or -1 on error."""
    try:
        usage = shutil.disk_usage(koan_root)
        return usage.free / (1024 ** 3)
    except OSError:
        return -1


def run_disk_space_check(koan_root: str) -> bool:
    """Check disk space and alert once per session if low.

    Returns True if space is OK, False if low (alert sent).
    """
    global _disk_space_alerted

    if _disk_space_alerted:
        return True  # Already alerted, don't spam

    if check_disk_space(koan_root):
        return True

    _disk_space_alerted = True
    free_gb = get_disk_free_gb(koan_root)
    try:
        from app.notify import send_telegram, NotificationPriority
        send_telegram(
            f"⚠️ Low disk space: {free_gb:.1f} GB free on KOAN_ROOT partition.\n"
            f"Consider cleaning up journal files or old branches.",
            priority=NotificationPriority.WARNING,
        )
    except (ImportError, OSError):
        pass
    return False
