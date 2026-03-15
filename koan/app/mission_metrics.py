"""Kōan — Statistical mission metrics for agent self-evaluation.

Computes reliability and quality metrics from session_outcomes.json:
- Success rate per project and per mission type
- PR creation rate
- Average duration per outcome
- Trend detection (improving/declining/stable)

Integration points:
- Read: status skill /metrics command surfaces metrics to human
- Read: iteration_manager.py uses success rates for project weighting
- Data: session_tracker.py records enriched outcomes (mission_type, has_pr, has_branch)
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def _load_outcomes(instance_dir: str) -> list:
    """Load session outcomes from JSON file."""
    outcomes_path = Path(instance_dir) / "session_outcomes.json"
    if not outcomes_path.exists():
        return []
    try:
        data = json.loads(outcomes_path.read_text())
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, OSError):
        return []


def compute_project_metrics(
    instance_dir: str,
    project: str,
    days: int = 30,
) -> dict:
    """Compute metrics for a single project over a time window.

    Args:
        instance_dir: Path to instance directory.
        project: Project name to filter by.
        days: Number of days to look back (0 = all time).

    Returns:
        Dict with keys:
            total_sessions (int): Total sessions in window.
            productive (int): Number of productive sessions.
            empty (int): Number of empty sessions.
            blocked (int): Number of blocked sessions.
            success_rate (float): Productive / total (0.0-1.0).
            pr_rate (float): Sessions with PR / total (0.0-1.0).
            branch_rate (float): Sessions with branch / total (0.0-1.0).
            avg_duration_minutes (float): Mean session duration.
            by_mission_type (dict): Per-type breakdown {type: {total, productive, success_rate}}.
    """
    outcomes = _load_outcomes(instance_dir)
    filtered = _filter_by_project_and_window(outcomes, project, days)

    if not filtered:
        return _empty_metrics()

    total = len(filtered)
    productive = sum(1 for o in filtered if o.get("outcome") == "productive")
    empty = sum(1 for o in filtered if o.get("outcome") == "empty")
    blocked = sum(1 for o in filtered if o.get("outcome") == "blocked")

    pr_count = sum(1 for o in filtered if o.get("has_pr"))
    branch_count = sum(1 for o in filtered if o.get("has_branch"))

    durations = [o.get("duration_minutes", 0) for o in filtered
                 if isinstance(o.get("duration_minutes"), (int, float))]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    # Per mission-type breakdown
    by_type = defaultdict(lambda: {"total": 0, "productive": 0})
    for o in filtered:
        mtype = o.get("mission_type", "unknown")
        by_type[mtype]["total"] += 1
        if o.get("outcome") == "productive":
            by_type[mtype]["productive"] += 1

    by_type_out = {}
    for mtype, counts in by_type.items():
        by_type_out[mtype] = {
            "total": counts["total"],
            "productive": counts["productive"],
            "success_rate": counts["productive"] / counts["total"] if counts["total"] else 0.0,
        }

    return {
        "total_sessions": total,
        "productive": productive,
        "empty": empty,
        "blocked": blocked,
        "success_rate": productive / total,
        "pr_rate": pr_count / total,
        "branch_rate": branch_count / total,
        "avg_duration_minutes": round(avg_duration, 1),
        "by_mission_type": by_type_out,
    }


def compute_global_metrics(
    instance_dir: str,
    days: int = 30,
) -> dict:
    """Compute cross-project metrics.

    Args:
        instance_dir: Path to instance directory.
        days: Number of days to look back (0 = all time).

    Returns:
        Dict with keys:
            total_sessions (int): Total across all projects.
            success_rate (float): Global productive / total.
            by_project (dict): Per-project {total, productive, success_rate}.
            trend (str): "improving", "declining", or "stable".
    """
    outcomes = _load_outcomes(instance_dir)
    filtered = _filter_by_window(outcomes, days)

    if not filtered:
        return {
            "total_sessions": 0,
            "success_rate": 0.0,
            "by_project": {},
            "trend": "stable",
        }

    total = len(filtered)
    productive = sum(1 for o in filtered if o.get("outcome") == "productive")
    empty = sum(1 for o in filtered if o.get("outcome") == "empty")
    blocked = sum(1 for o in filtered if o.get("outcome") == "blocked")

    # Per-project breakdown
    by_project = defaultdict(lambda: {"total": 0, "productive": 0})
    for o in filtered:
        proj = o.get("project", "unknown")
        by_project[proj]["total"] += 1
        if o.get("outcome") == "productive":
            by_project[proj]["productive"] += 1

    by_project_out = {}
    for proj, counts in by_project.items():
        by_project_out[proj] = {
            "total": counts["total"],
            "productive": counts["productive"],
            "success_rate": counts["productive"] / counts["total"] if counts["total"] else 0.0,
        }

    trend = _compute_trend(filtered)

    return {
        "total_sessions": total,
        "productive": productive,
        "empty": empty,
        "blocked": blocked,
        "success_rate": productive / total,
        "by_project": by_project_out,
        "trend": trend,
    }


def get_project_success_rates(
    instance_dir: str,
    projects: List[str],
    days: int = 30,
) -> Dict[str, float]:
    """Get success rates for multiple projects (for iteration_manager weighting).

    Args:
        instance_dir: Path to instance directory.
        projects: List of project names.
        days: Number of days to look back.

    Returns:
        Dict mapping project name to success rate (0.0-1.0).
        Projects with no data get 0.5 (neutral).
    """
    outcomes = _load_outcomes(instance_dir)
    filtered = _filter_by_window(outcomes, days)

    by_project = defaultdict(lambda: {"total": 0, "productive": 0})
    for o in filtered:
        proj = o.get("project", "")
        if proj in projects:
            by_project[proj]["total"] += 1
            if o.get("outcome") == "productive":
                by_project[proj]["productive"] += 1

    rates = {}
    for proj in projects:
        counts = by_project.get(proj)
        if counts and counts["total"] >= 3:
            rates[proj] = counts["productive"] / counts["total"]
        else:
            rates[proj] = 0.5  # Neutral — not enough data
    return rates


def compute_project_trend(instance_dir: str, project: str, days: int = 30) -> str:
    """Compute trend for a single project over the given window.

    Args:
        instance_dir: Path to instance directory.
        project: Project name to filter by.
        days: Number of days to look back (0 = all time).

    Returns:
        "improving", "declining", or "stable".
    """
    outcomes = _load_outcomes(instance_dir)
    filtered = _filter_by_project_and_window(outcomes, project, days)
    return _compute_trend(filtered)


def format_metrics_summary(instance_dir: str, days: int = 30) -> str:
    """Format a human-readable metrics summary for display.

    Args:
        instance_dir: Path to instance directory.
        days: Number of days to look back.

    Returns:
        Formatted multi-line summary string.
    """
    global_m = compute_global_metrics(instance_dir, days=days)

    if global_m["total_sessions"] == 0:
        return "No session data available yet."

    window = f"last {days} days" if days else "all time"
    lines = [
        f"Mission Metrics ({window})",
        "",
        f"Total sessions: {global_m['total_sessions']}",
        f"Success rate: {global_m['success_rate']:.0%}",
        f"Trend: {global_m['trend']}",
    ]

    # Per-project breakdown
    if global_m["by_project"]:
        lines.append("")
        lines.append("By project:")
        for proj in sorted(global_m["by_project"].keys()):
            stats = global_m["by_project"][proj]
            lines.append(
                f"  {proj}: {stats['success_rate']:.0%} "
                f"({stats['productive']}/{stats['total']})"
            )

    # Per-project detailed metrics (top projects by session count)
    sorted_projects = sorted(
        global_m["by_project"].keys(),
        key=lambda p: global_m["by_project"][p]["total"],
        reverse=True,
    )

    for proj in sorted_projects[:3]:
        proj_m = compute_project_metrics(instance_dir, proj, days=days)
        if proj_m["total_sessions"] < 3:
            continue

        lines.append("")
        lines.append(f"{proj} detail:")
        lines.append(f"  PR rate: {proj_m['pr_rate']:.0%}")
        lines.append(f"  Branch rate: {proj_m['branch_rate']:.0%}")
        lines.append(f"  Avg duration: {proj_m['avg_duration_minutes']:.0f}min")

        if proj_m["by_mission_type"]:
            for mtype, mstats in sorted(proj_m["by_mission_type"].items()):
                if mstats["total"] >= 2:
                    lines.append(
                        f"  {mtype}: {mstats['success_rate']:.0%} "
                        f"({mstats['productive']}/{mstats['total']})"
                    )

    return "\n".join(lines)


# --- Internal helpers ---


def _filter_by_project_and_window(
    outcomes: list, project: str, days: int,
) -> list:
    """Filter outcomes by project and time window."""
    filtered = [o for o in outcomes if o.get("project") == project]
    if days > 0:
        filtered = _filter_by_window(filtered, days)
    return filtered


def _filter_by_window(outcomes: list, days: int) -> list:
    """Filter outcomes to those within the last N days."""
    if days <= 0:
        return outcomes

    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for o in outcomes:
        ts = o.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            if dt >= cutoff:
                result.append(o)
        except (ValueError, TypeError):
            # Include entries with unparseable timestamps (benefit of the doubt)
            result.append(o)
    return result


def _compute_trend(outcomes: list) -> str:
    """Detect if success rate is improving, declining, or stable.

    Splits outcomes into two halves and compares success rates.
    Needs at least 6 sessions to compute a meaningful trend.
    """
    if len(outcomes) < 6:
        return "stable"

    mid = len(outcomes) // 2
    first_half = outcomes[:mid]
    second_half = outcomes[mid:]

    def _rate(items):
        total = len(items)
        productive = sum(1 for o in items if o.get("outcome") == "productive")
        return productive / total if total else 0.0

    first_rate = _rate(first_half)
    second_rate = _rate(second_half)

    diff = second_rate - first_rate
    if diff > 0.15:
        return "improving"
    elif diff < -0.15:
        return "declining"
    return "stable"


def _empty_metrics() -> dict:
    """Return empty metrics structure."""
    return {
        "total_sessions": 0,
        "productive": 0,
        "empty": 0,
        "blocked": 0,
        "success_rate": 0.0,
        "pr_rate": 0.0,
        "branch_rate": 0.0,
        "avg_duration_minutes": 0.0,
        "by_mission_type": {},
    }
