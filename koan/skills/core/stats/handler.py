"""Kōan stats skill — session outcome statistics per project."""

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def handle(ctx):
    """Show session productivity stats, optionally filtered by project."""
    instance_dir = ctx.instance_dir
    project_filter = ctx.args.strip() if ctx.args else ""

    outcomes = _load_outcomes(instance_dir / "session_outcomes.json")
    if not outcomes:
        return "No session data yet. Stats will appear after the first completed run."

    if project_filter:
        filtered = [o for o in outcomes if o.get("project") == project_filter]
        if not filtered:
            known = sorted(set(o.get("project", "") for o in outcomes))
            return (
                f"No data for '{project_filter}'.\n"
                f"Known projects: {', '.join(known)}"
            )
        return _format_project_detail(project_filter, filtered)

    return _format_overview(outcomes)


def _load_outcomes(path: Path) -> list:
    """Load session outcomes from JSON file."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _format_overview(outcomes: list) -> str:
    """Format a cross-project overview."""
    by_project = {}
    for o in outcomes:
        project = o.get("project", "unknown")
        by_project.setdefault(project, []).append(o)

    total = len(outcomes)
    total_productive = sum(1 for o in outcomes if o.get("outcome") == "productive")
    total_empty = sum(1 for o in outcomes if o.get("outcome") == "empty")
    total_blocked = sum(1 for o in outcomes if o.get("outcome") == "blocked")

    pct = int(total_productive / max(1, total) * 100)

    # Streak
    streak = _productive_streak(outcomes)

    lines = [
        "Session Stats",
        f"  Total: {total} sessions | {pct}% productive",
        f"  {total_productive} productive | {total_empty} empty | {total_blocked} blocked",
    ]

    if streak >= 2:
        lines.append(f"  Streak: {streak} productive in a row")

    # Time-based breakdowns
    now = datetime.now()
    today_line = _format_period_line(
        _filter_by_period(outcomes, "today", now), "Today", now
    )
    week_line = _format_period_line(
        _filter_by_period(outcomes, "week", now), "This week", now
    )
    last_week_line = _format_period_line(
        _filter_by_period(outcomes, "last_week", now), "Last week", now
    )

    time_lines = [l for l in (today_line, week_line, last_week_line) if l]
    if time_lines:
        lines.append("")
        lines.extend(time_lines)

    lines.append("")

    # Per-project summary sorted by session count
    sorted_projects = sorted(by_project.items(), key=lambda x: -len(x[1]))
    for project, project_outcomes in sorted_projects:
        count = len(project_outcomes)
        productive = sum(1 for o in project_outcomes if o.get("outcome") == "productive")
        staleness = _consecutive_non_productive(project_outcomes)
        p_pct = int(productive / max(1, count) * 100)

        status = ""
        if staleness >= 5:
            status = " !!!"
        elif staleness >= 3:
            status = " !"

        lines.append(f"  {project}: {count} ({p_pct}% productive){status}")

    lines.append("")
    lines.append("Use /stats <project> for details.")

    return "\n".join(lines)


def _format_project_detail(project: str, outcomes: list) -> str:
    """Format detailed stats for a single project."""
    total = len(outcomes)
    productive = sum(1 for o in outcomes if o.get("outcome") == "productive")
    empty = sum(1 for o in outcomes if o.get("outcome") == "empty")
    blocked = sum(1 for o in outcomes if o.get("outcome") == "blocked")
    pct = int(productive / max(1, total) * 100)

    # Mode breakdown
    mode_counter = Counter(o.get("mode", "unknown") for o in outcomes)

    # Duration stats
    durations = [o.get("duration_minutes", 0) for o in outcomes if o.get("duration_minutes")]
    avg_duration = int(sum(durations) / max(1, len(durations))) if durations else 0

    # Staleness
    staleness = _consecutive_non_productive(outcomes)

    # Streak
    streak = _productive_streak(outcomes)

    lines = [
        f"Stats: {project}",
        f"  Sessions: {total} | {pct}% productive",
        f"  {productive} productive | {empty} empty | {blocked} blocked",
    ]

    if staleness > 0:
        if staleness >= 5:
            lines.append(f"  Staleness: {staleness} consecutive non-productive")
        elif staleness >= 3:
            lines.append(f"  Staleness: {staleness} (approaching limit)")

    if streak >= 2:
        lines.append(f"  Streak: {streak} productive in a row")

    # Time-based breakdowns
    now = datetime.now()
    today_line = _format_period_line(
        _filter_by_period(outcomes, "today", now), "Today", now
    )
    week_line = _format_period_line(
        _filter_by_period(outcomes, "week", now), "This week", now
    )

    time_lines = [l for l in (today_line, week_line) if l]
    if time_lines:
        lines.append("")
        lines.extend(time_lines)

    lines.append("")

    # Mode breakdown
    lines.append("By mode:")
    for mode in ("deep", "implement", "review", "wait"):
        count = mode_counter.get(mode, 0)
        if count > 0:
            mode_outcomes = [o for o in outcomes if o.get("mode") == mode]
            mode_productive = sum(1 for o in mode_outcomes if o.get("outcome") == "productive")
            lines.append(f"  {mode}: {count} ({mode_productive} productive)")

    # Show unknown modes if any
    for mode, count in mode_counter.items():
        if mode not in ("deep", "implement", "review", "wait") and count > 0:
            lines.append(f"  {mode}: {count}")

    if avg_duration > 0:
        lines.append(f"\nAvg duration: {avg_duration} min")

    # Last 5 sessions
    recent = outcomes[-5:]
    lines.append("\nRecent:")
    for o in reversed(recent):
        outcome = o.get("outcome", "?")
        mode = o.get("mode", "?")
        ts = o.get("timestamp", "?")
        if "T" in ts:
            ts = ts.split("T")[1][:5]
        summary = o.get("summary", "")
        if len(summary) > 50:
            summary = summary[:47] + "..."

        icon = "+" if outcome == "productive" else "-" if outcome == "empty" else "~"
        line = f"  {icon} {ts} [{mode}]"
        if summary:
            line += f" {summary}"
        lines.append(line)

    return "\n".join(lines)


def _consecutive_non_productive(outcomes: list) -> int:
    """Count consecutive non-productive sessions from the end."""
    count = 0
    for o in reversed(outcomes):
        if o.get("outcome") == "productive":
            break
        count += 1
    return count


def _productive_streak(outcomes: list) -> int:
    """Count consecutive productive sessions from the end."""
    count = 0
    for o in reversed(outcomes):
        if o.get("outcome") != "productive":
            break
        count += 1
    return count


def _filter_by_period(outcomes: list, period: str,
                      now: datetime = None) -> list:
    """Filter outcomes by time period.

    Args:
        outcomes: List of outcome dicts with 'timestamp' field.
        period: One of "today", "week", "last_week".
        now: Override current time (for testing).

    Returns:
        Filtered list of outcomes within the period.
    """
    if now is None:
        now = datetime.now()

    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = None
    elif period == "week":
        # Monday of current week at midnight
        cutoff = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = None
    elif period == "last_week":
        this_monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff = this_monday - timedelta(days=7)
        end = this_monday
    else:
        return outcomes

    filtered = []
    for o in outcomes:
        ts_str = o.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff and (end is None or ts < end):
            filtered.append(o)
    return filtered


def _format_period_line(outcomes: list, label: str,
                        now: datetime = None) -> str:
    """Format a single time-period summary line.

    Returns empty string if no sessions in the period.
    """
    if not outcomes:
        return ""
    total = len(outcomes)
    productive = sum(1 for o in outcomes if o.get("outcome") == "productive")
    pct = int(productive / max(1, total) * 100)
    return f"  {label}: {total} sessions ({pct}% productive)"
