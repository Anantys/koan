#!/usr/bin/env python3
"""
Kōan -- Daily report generator

Builds a concise Telegram-friendly digest of the day's activity.
Called from run.py at session boundaries (morning or evening).

Usage from shell:
    python3 daily_report.py [--yesterday]

Usage from Python:
    from app.daily_report import generate_report, should_send_report
"""

import os
import re
import sys
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import List, Optional

from app.notify import format_and_send
from app.signals import DAILY_REPORT_FILE, QUOTA_RESET_FILE

from app.utils import load_dotenv

load_dotenv()

if "KOAN_ROOT" not in os.environ:
    raise SystemExit("KOAN_ROOT environment variable is required")
KOAN_ROOT = Path(os.environ["KOAN_ROOT"])
INSTANCE_DIR = KOAN_ROOT / "instance"
MISSIONS_FILE = INSTANCE_DIR / "missions.md"
REPORT_MARKER = KOAN_ROOT / DAILY_REPORT_FILE


def should_send_report() -> Optional[str]:
    """Check if a daily report should be sent now.

    Returns:
        "morning" if 7-9am and no report sent today,
        "evening" if after 8pm and quota exhausted,
        None otherwise.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Don't send the same report twice in one day
    if REPORT_MARKER.exists():
        marker_date = REPORT_MARKER.read_text().strip()
        if marker_date == today:
            return None

    # Morning report: 7am-9am -> report on yesterday
    if 7 <= now.hour <= 9:
        return "morning"

    # Evening report: after 8pm if quota exhausted
    if now.hour >= 20:
        quota_file = KOAN_ROOT / QUOTA_RESET_FILE
        if quota_file.exists():
            return "evening"

    return None


def _read_journal(target_date: date) -> str:
    """Read all journal entries for a given date across all project subdirs."""
    from app.journal import read_all_journals
    return read_all_journals(INSTANCE_DIR, target_date)


def _extract_mission_title(line: str) -> Optional[str]:
    """Extract a clean title from a mission line.

    Handles both current format ``- [project:name] text ⏳(...) ▶(...) ✅(...)``
    and legacy bold format ``- **title** (extra)``.
    Returns None if the line doesn't look like a mission.
    """
    line = line.strip()
    if not line.startswith("- "):
        return None
    text = line[2:].strip()
    if not text:
        return None

    # Strip lifecycle timestamps: ⏳(...) ▶(...) ✅(...) ❌(...)
    text = re.sub(r"\s*[⏳▶✅❌]\s*\([^)]*\)", "", text).strip()

    # Strip project tag: [project:name]
    text = re.sub(r"^\[project:[^\]]+\]\s*", "", text).strip()

    # Legacy bold format: **title** — strip markdown bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text).strip()

    # Strip trailing metadata: (session N), — session N, — PR #NNN
    text = re.split(r"\s*[\(\—–—]", text)[0].strip()

    return text if text else None


def _parse_completed_missions(target_date: Optional[date] = None) -> List[str]:
    """Extract completed missions from missions.md.

    Args:
        target_date: If provided, only return missions completed on this date.
                     If None, return all completed missions (legacy behavior).
    """
    if not MISSIONS_FILE.exists():
        return []

    from app.missions import parse_sections

    content = MISSIONS_FILE.read_text()
    sections = parse_sections(content)
    completed = []
    for item in sections["done"]:
        first_line = item.split("\n")[0]

        if target_date is not None:
            # Filter by ✅ completion date
            match = re.search(r"✅\s*\((\d{4}-\d{2}-\d{2})", first_line)
            if not match or match.group(1) != target_date.strftime("%Y-%m-%d"):
                continue

        title = _extract_mission_title(first_line)
        if title:
            completed.append(title)

    return completed


def _count_pending_missions() -> int:
    """Count pending missions."""
    if not MISSIONS_FILE.exists():
        return 0

    from app.missions import count_pending

    return count_pending(MISSIONS_FILE.read_text())


def generate_report(report_type: str = "morning") -> str:
    """Generate a daily digest.

    Args:
        report_type: "morning" (yesterday's recap) or "evening" (today's recap)
    """
    if report_type == "morning":
        target = date.today() - timedelta(days=1)
        header = f"Report for {target.strftime('%Y-%m-%d')}"
    else:
        target = date.today()
        header = "Daily Summary"

    journal = _read_journal(target)
    completed = _parse_completed_missions(target_date=target)
    pending = _count_pending_missions()

    lines = [f"-- {header} --", ""]

    # Completed missions
    if completed:
        lines.append("Completed missions:")
        for m in completed[-5:]:  # Last 5 max
            lines.append(f"  . {m}")
        lines.append("")

    # Pending missions
    if pending > 0:
        lines.append(f"Pending: {pending} mission(s)")
        lines.append("")

    # Journal summary (extract key sections)
    if journal:
        # Extract section headers as activity summary
        activities = []
        for line in journal.splitlines():
            if line.startswith("## ") and not line.startswith("## Quota"):
                activity = line[3:].strip()
                # Remove timestamps
                activity = re.sub(r"\s*[—–-]{1,2}\s*\d{2}:\d{2}(:\d{2})?", "", activity)
                if activity:
                    activities.append(activity)

        if activities:
            lines.append("Activity:")
            for a in activities[-6:]:  # Last 6 max
                lines.append(f"  . {a}")
            lines.append("")
    else:
        lines.append("No activity recorded.")
        lines.append("")

    # In-progress items
    if MISSIONS_FILE.exists():
        from app.missions import parse_sections

        content = MISSIONS_FILE.read_text()
        sections = parse_sections(content)
        in_progress = []
        for item in sections["in_progress"]:
            first_line = item.split("\n")[0]
            # Handle ### multi-line blocks (legacy)
            stripped = first_line.strip()
            if stripped.startswith("### "):
                in_progress.append(stripped[4:].strip())
            else:
                title = _extract_mission_title(first_line)
                if title:
                    in_progress.append(title)

        if in_progress:
            lines.append("In Progress:")
            for ip in in_progress:
                lines.append(f"  . {ip}")
            lines.append("")

    lines.append("-- Kōan")
    return "\n".join(lines)


def mark_report_sent():
    """Mark today's report as sent."""
    REPORT_MARKER.write_text(date.today().strftime("%Y-%m-%d"))


def send_daily_report(report_type: str = None) -> bool:
    """Check conditions and send daily report if appropriate.

    Args:
        report_type: Force a specific type ("morning"/"evening"), or None for auto-detect.

    Returns:
        True if a report was sent.
    """
    if report_type is None:
        report_type = should_send_report()

    if report_type is None:
        return False

    report = generate_report(report_type)
    success = format_and_send(report)

    if success:
        mark_report_sent()
        print(f"[daily-report] {report_type} report sent")
    else:
        print(f"[daily-report] Failed to send {report_type} report")

    return success


if __name__ == "__main__":
    forced_type = None
    if "--yesterday" in sys.argv or "--morning" in sys.argv:
        forced_type = "morning"
    elif "--evening" in sys.argv or "--today" in sys.argv:
        forced_type = "evening"

    if forced_type:
        report = generate_report(forced_type)
        print(report)
        print()
        success = format_and_send(report)
        if success:
            mark_report_sent()
        sys.exit(0 if success else 1)
    else:
        sent = send_daily_report()
        sys.exit(0 if sent else 1)
