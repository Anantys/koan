"""Kōan — PR feedback loop for autonomous topic alignment.

Tracks which PRs get merged quickly (high-value work) vs. which stay
open for days (lower priority or misaligned). This feedback is injected
into the deep research suggestions to help the agent choose topics that
align with what the human actually values.

Key insight: the fastest signal of "what the human wants" is what they
merge. Fast merges = high alignment. Stale PRs = lower priority.

Integration points:
- Read: deep_research.py uses get_alignment_summary() for prompt injection
- Read: prompt_builder.py includes feedback in autonomous prompts
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# Work type categories — ordered by specificity (most specific first)
_CATEGORY_PATTERNS = [
    ("security", re.compile(r"\bsecur\w+\b|vuln\w+\b|csrf\b|xss\b|injection\b", re.IGNORECASE)),
    ("test", re.compile(r"\btest[s]?\b|coverage|spec[s]?\b", re.IGNORECASE)),
    ("ci", re.compile(r"\bci\b|deploy\w*\b|pipeline\b|docker\b|github.action\b", re.IGNORECASE)),
    ("docs", re.compile(r"\bdoc\w*\b|readme\b|comment[s]?\b|changelog\b", re.IGNORECASE)),
    ("perf", re.compile(r"\bperf\w*\b|optimi\w+\b|speed\b|fast\w*\b|cache\b", re.IGNORECASE)),
    ("refactor", re.compile(r"\brefactor\w*\b|cleanup\b|simplif\w+\b|migrat\w+\b|extract\w*\b", re.IGNORECASE)),
    ("fix", re.compile(r"\bfix\b|bug\b|patch\b|hotfix\b|resolve\b", re.IGNORECASE)),
    ("feature", re.compile(r"\bfeat\w*\b|add\b|implement\b|new\b|support\b", re.IGNORECASE)),
]

# Conventional commit prefix to category mapping
_CONVENTIONAL_MAP = {
    "test": "test",
    "fix": "fix",
    "feat": "feature",
    "refactor": "refactor",
    "docs": "docs",
    "perf": "perf",
    "ci": "ci",
    "chore": "other",
    "style": "other",
    "build": "ci",
}

# Thresholds for merge velocity classification (in hours)
FAST_MERGE_HOURS = 48
SLOW_MERGE_HOURS = 168  # 7 days


def categorize_pr(title: str) -> str:
    """Categorize a PR by work type from its title.

    Uses conventional commit prefixes first, then keyword matching.

    Args:
        title: PR title string.

    Returns:
        Category string (test, fix, security, refactor, docs, feature,
        perf, ci, other).
    """
    if not title:
        return "other"

    # Try conventional commit prefix first: "fix: something" or "fix(scope): something"
    conv_match = re.match(r"^(\w+)(?:\([^)]*\))?[!]?:\s", title)
    if conv_match:
        prefix = conv_match.group(1).lower()
        if prefix in _CONVENTIONAL_MAP:
            return _CONVENTIONAL_MAP[prefix]

    # Fall back to keyword matching
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(title):
            return category

    return "other"


def _parse_iso_datetime(dt_str: str) -> Optional[datetime]:
    """Parse an ISO datetime string from gh CLI output.

    Handles both Z suffix and +00:00 offset formats.
    """
    if not dt_str:
        return None
    try:
        # gh CLI outputs ISO format like "2026-02-20T14:30:00Z"
        dt_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None


def _hours_between(start: datetime, end: datetime) -> float:
    """Compute hours between two datetimes."""
    # Ensure both are timezone-aware or both naive
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - start
    return delta.total_seconds() / 3600


def fetch_merged_prs(
    project_path: str,
    days: int = 30,
    limit: int = 50,
) -> List[dict]:
    """Fetch recently merged koan/* PRs for a project.

    Args:
        project_path: Path to the git repo.
        days: Look back this many days.
        limit: Maximum PRs to fetch.

    Returns:
        List of PR dicts with keys: number, title, createdAt, mergedAt,
        headRefName, category, hours_to_merge.
    """
    try:
        from app.github import run_gh
    except ImportError:
        return []

    try:
        from app.config import get_branch_prefix
        prefix = get_branch_prefix()
    except Exception as e:
        print(f"[pr_feedback] Branch prefix load failed: {e}", file=sys.stderr)
        prefix = "koan/"

    try:
        raw = run_gh(
            "pr", "list",
            "--state", "merged",
            "--limit", str(limit),
            "--json", "number,title,createdAt,mergedAt,headRefName",
            cwd=project_path,
            timeout=15,
        )
    except Exception as e:
        print(f"[pr_feedback] Failed to fetch merged PRs: {e}", file=sys.stderr)
        return []

    try:
        import json
        prs = json.loads(raw)
    except Exception as e:
        print(f"[pr_feedback] JSON parse failed for merged PRs: {e}", file=sys.stderr)
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    results = []
    for pr in prs:
        branch = pr.get("headRefName", "")
        if not branch.startswith(prefix):
            continue

        title = pr.get("title", "")
        created = _parse_iso_datetime(pr.get("createdAt", ""))
        merged = _parse_iso_datetime(pr.get("mergedAt", ""))

        if not created or not merged:
            continue

        # Filter by merge date — skip PRs merged before the cutoff
        if merged < cutoff:
            continue

        hours = _hours_between(created, merged)
        category = categorize_pr(title)

        results.append({
            "number": pr.get("number"),
            "title": title,
            "createdAt": pr.get("createdAt"),
            "mergedAt": pr.get("mergedAt"),
            "headRefName": branch,
            "category": category,
            "hours_to_merge": round(hours, 1),
        })

    return results


def fetch_open_prs(project_path: str) -> List[dict]:
    """Fetch currently open koan/* PRs.

    Args:
        project_path: Path to the git repo.

    Returns:
        List of PR dicts with: number, title, createdAt, headRefName,
        category, hours_open.
    """
    try:
        from app.github import run_gh
    except ImportError:
        return []

    try:
        from app.config import get_branch_prefix
        prefix = get_branch_prefix()
    except Exception as e:
        print(f"[pr_feedback] Branch prefix load failed: {e}", file=sys.stderr)
        prefix = "koan/"

    try:
        raw = run_gh(
            "pr", "list",
            "--state", "open",
            "--json", "number,title,createdAt,headRefName",
            cwd=project_path,
            timeout=15,
        )
    except Exception as e:
        print(f"[pr_feedback] Failed to fetch open PRs: {e}", file=sys.stderr)
        return []

    try:
        import json
        prs = json.loads(raw)
    except Exception as e:
        print(f"[pr_feedback] JSON parse failed for open PRs: {e}", file=sys.stderr)
        return []

    now = datetime.now(timezone.utc)
    results = []
    for pr in prs:
        branch = pr.get("headRefName", "")
        if not branch.startswith(prefix):
            continue

        title = pr.get("title", "")
        created = _parse_iso_datetime(pr.get("createdAt", ""))

        hours_open = _hours_between(created, now) if created else 0
        category = categorize_pr(title)

        results.append({
            "number": pr.get("number"),
            "title": title,
            "createdAt": pr.get("createdAt"),
            "headRefName": branch,
            "category": category,
            "hours_open": round(hours_open, 1),
        })

    return results


def compute_merge_velocity(merged_prs: List[dict]) -> Dict[str, dict]:
    """Group merged PRs by category and compute average merge time.

    Args:
        merged_prs: List of PR dicts from fetch_merged_prs().

    Returns:
        Dict mapping category to stats:
        {
            "fix": {"count": 5, "avg_hours": 18.3, "speed": "fast"},
            "refactor": {"count": 2, "avg_hours": 192.0, "speed": "slow"},
        }
    """
    by_category: Dict[str, List[float]] = {}
    for pr in merged_prs:
        cat = pr.get("category", "other")
        hours = pr.get("hours_to_merge", 0)
        by_category.setdefault(cat, []).append(hours)

    result = {}
    for cat, hours_list in by_category.items():
        avg = sum(hours_list) / len(hours_list)
        if avg <= FAST_MERGE_HOURS:
            speed = "fast"
        elif avg <= SLOW_MERGE_HOURS:
            speed = "moderate"
        else:
            speed = "slow"

        result[cat] = {
            "count": len(hours_list),
            "avg_hours": round(avg, 1),
            "speed": speed,
        }

    return result


def _format_hours(hours: float) -> str:
    """Format hours into a human-readable string."""
    if hours < 1:
        return "<1h"
    if hours < 24:
        return f"{hours:.0f}h"
    days = hours / 24
    if days < 2:
        return f"{days:.1f}d"
    return f"{days:.0f}d"


def get_alignment_summary(
    project_path: str,
    days: int = 30,
) -> str:
    """Generate a human-readable alignment summary for prompt injection.

    Fetches both merged and open PRs, analyzes merge velocity by category,
    and formats insights for the agent.

    Args:
        project_path: Path to the git repo.
        days: Look-back window for merged PRs.

    Returns:
        Formatted markdown string, or empty string if no data.
    """
    merged = fetch_merged_prs(project_path, days=days)
    open_prs = fetch_open_prs(project_path)

    if not merged and not open_prs:
        return ""

    lines = []

    if merged:
        velocity = compute_merge_velocity(merged)

        # Sort: fast first, then moderate, then slow
        speed_order = {"fast": 0, "moderate": 1, "slow": 2}
        sorted_cats = sorted(
            velocity.items(),
            key=lambda x: (speed_order.get(x[1]["speed"], 3), -x[1]["count"]),
        )

        fast_cats = [
            f"{cat} ({v['count']} PRs, avg {_format_hours(v['avg_hours'])})"
            for cat, v in sorted_cats if v["speed"] == "fast"
        ]
        moderate_cats = [
            f"{cat} ({v['count']} PRs, avg {_format_hours(v['avg_hours'])})"
            for cat, v in sorted_cats if v["speed"] == "moderate"
        ]
        slow_cats = [
            f"{cat} ({v['count']} PRs, avg {_format_hours(v['avg_hours'])})"
            for cat, v in sorted_cats if v["speed"] == "slow"
        ]

        if fast_cats:
            lines.append(f"**Quickly merged** (<48h): {', '.join(fast_cats)}")
        if moderate_cats:
            lines.append(f"**Moderately merged** (2-7d): {', '.join(moderate_cats)}")
        if slow_cats:
            lines.append(f"**Slow to merge** (>7d): {', '.join(slow_cats)}")

    if open_prs:
        # Sort by age (oldest first)
        open_sorted = sorted(open_prs, key=lambda p: p.get("hours_open", 0), reverse=True)
        open_descs = []
        for pr in open_sorted[:5]:
            age = _format_hours(pr["hours_open"])
            open_descs.append(f"#{pr['number']} {pr['category']} — {age} old")
        lines.append(f"**Still open**: {', '.join(open_descs)}")

    if not lines:
        return ""

    return "\n".join(lines)


def get_category_boost(
    project_path: str,
    days: int = 30,
) -> Dict[str, int]:
    """Compute priority adjustment per work category based on merge feedback.

    Fast-merged categories get a boost (-1 priority = higher),
    slow-merged categories get a penalty (+1 priority = lower).

    Args:
        project_path: Path to the git repo.
        days: Look-back window.

    Returns:
        Dict mapping category to priority adjustment (-1, 0, or +1).
        Empty dict if no data available.
    """
    merged = fetch_merged_prs(project_path, days=days)
    if not merged:
        return {}

    velocity = compute_merge_velocity(merged)
    boosts = {}
    for cat, stats in velocity.items():
        if stats["speed"] == "fast":
            boosts[cat] = -1  # Boost (lower number = higher priority)
        elif stats["speed"] == "slow":
            boosts[cat] = 1   # Demote
        # moderate = no adjustment

    return boosts
