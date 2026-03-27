"""Kōan — Session outcome tracking for smarter autonomous decisions.

Records what each session accomplished and detects "stale" projects
where consecutive sessions produce no actionable work. This breaks the
pattern of 17 consecutive verification sessions by giving the agent
(and the iteration planner) concrete feedback on recent productivity.

Data is stored in instance/session_outcomes.json (append-only, capped).

Integration points:
- Write: mission_runner.run_post_mission() records after each session
- Read: deep_research.py injects staleness warnings into agent prompt
- Read: iteration_manager.py weights project selection by freshness
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Maximum entries to keep in session_outcomes.json (rolling window)
MAX_OUTCOMES = 200

# TTL cache for _count_commits_since() — avoids repeated git subprocess calls
# Key: (project_path, since_iso), Value: (commit_count, monotonic_timestamp)
_commits_cache: Dict[tuple, tuple] = {}
_COMMITS_CACHE_TTL = 300  # 5 minutes

# Keywords that signal an empty/non-productive session
_EMPTY_KEYWORDS = [
    "verification session",
    "no code",
    "no new code",
    "waiting state",
    "legitimate waiting state",
    "blocked on merge",
    "housekeeping only",
    "no actionable work",
    "same state",
    "same pattern",
    "no code changes",
    "nothing actionable",
    "merge queue",
    "all work blocked",
    "pas de code",
    "identical session",
    "no changes needed",
]

# Strong productive signals — multi-word phrases that are unambiguous
_STRONG_PRODUCTIVE = [
    "branch pushed",
    "branch `koan/",
    "branch koan",
    "pr #",
    "pr created",
    "draft pr",
    "tests pass",
]

# Weaker productive signals — single words that need context
_WEAK_PRODUCTIVE_RE = re.compile(
    r"\b(?:implemented|refactored|migrated)\b", re.IGNORECASE
)

# Skill commands that are inherently productive (they DO things:
# create branches, push code, write PRs). A session executing one of
# these should never be classified as "empty".
_PRODUCTIVE_SKILLS = re.compile(
    r"^/(?:rebase|recreate|fix|implement|plan|review|refactor|ai|check|claudemd|mission)\b"
)


def classify_session(journal_content: str, mission_title: str = "") -> str:
    """Classify a session as productive, empty, or blocked.

    Uses keyword matching on the journal/pending content to determine
    whether the session produced actionable output.

    Strategy: strong signals win immediately, weak signals are counted.
    Empty phrases are multi-word (specific), so they're reliable.
    Single-word productive signals ("added", "fixed") are too ambiguous
    and were removed in favor of strong multi-word patterns.

    Args:
        journal_content: The session's journal entry or pending.md content.
        mission_title: The mission title (e.g. "/rebase https://...").
            Skill commands are inherently productive.

    Returns:
        "productive", "empty", or "blocked"
    """
    # Skill commands are inherently productive — they create branches,
    # push code, write PRs. Even if pending.md is empty (agent cleaned up),
    # running /rebase or /fix IS work.
    if mission_title and _PRODUCTIVE_SKILLS.search(mission_title.strip()):
        return "productive"

    if not journal_content:
        return "empty"

    lower = journal_content.lower()

    # Strong productive signals — any one is conclusive
    if any(kw in lower for kw in _STRONG_PRODUCTIVE):
        return "productive"

    # Count empty signals (multi-word phrases, low false positive rate)
    empty_score = sum(1 for kw in _EMPTY_KEYWORDS if kw in lower)

    # Strong empty overrides everything (including blocked keywords)
    if empty_score >= 3:
        return "empty"

    # Blocked keywords with insufficient productive evidence → blocked
    has_blocked = (
        "blocked on merge" in lower
        or "merge queue" in lower
        or "all work blocked" in lower
    )
    weak_productive = len(_WEAK_PRODUCTIVE_RE.findall(lower))
    if has_blocked and weak_productive < 1:
        return "blocked"

    # Moderate empty signals → empty
    if empty_score >= 2:
        return "empty"

    # Weak productive signals → productive
    if weak_productive >= 1:
        return "productive"

    # Single empty signal → empty
    if empty_score >= 1:
        return "empty"

    # Default: benefit of the doubt
    return "productive"


def _extract_summary(journal_content: str, max_chars: int = 120) -> str:
    """Extract a brief summary from journal content.

    Looks for the first substantive line (not a header, not empty).
    """
    if not journal_content:
        return ""

    for line in journal_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        if line.startswith("Project:") or line.startswith("Started:"):
            continue
        if line.startswith("Run:") or line.startswith("Mode:"):
            continue
        # Found a content line
        if len(line) > max_chars:
            return line[:max_chars] + "..."
        return line

    return ""


def classify_mission_type(mission_title: str) -> str:
    """Classify a mission into a type category for metrics tracking.

    Categories:
        "skill" — Skill command (/rebase, /implement, /review, etc.)
        "autonomous" — Autonomous exploration (no mission title or "Autonomous ...")
        "mission" — Free-text human-submitted mission

    Args:
        mission_title: The mission title string.

    Returns:
        One of "skill", "autonomous", or "mission".
    """
    if not mission_title or not mission_title.strip():
        return "autonomous"
    title = mission_title.strip()
    if _PRODUCTIVE_SKILLS.search(title):
        return "skill"
    if title.lower().startswith("autonomous "):
        return "autonomous"
    return "mission"


def _detect_pr_created(content: str) -> bool:
    """Detect whether a PR was created from journal/summary content."""
    if not content:
        return False
    lower = content.lower()
    return any(signal in lower for signal in (
        "pr #", "pr created", "draft pr", "pull request",
    ))


def _detect_branch_pushed(content: str) -> bool:
    """Detect whether a branch was pushed from journal/summary content."""
    if not content:
        return False
    lower = content.lower()
    return any(signal in lower for signal in (
        "branch pushed", "branch `koan/", "branch koan",
    ))


def record_outcome(
    instance_dir: str,
    project: str,
    mode: str,
    duration_minutes: int,
    journal_content: str,
    mission_title: str = "",
) -> dict:
    """Record a session outcome to session_outcomes.json.

    Args:
        instance_dir: Path to instance directory.
        project: Project name.
        mode: Autonomous mode (review/implement/deep).
        duration_minutes: Session duration in minutes.
        journal_content: The session's journal/pending content for classification.
        mission_title: The mission title for skill-aware classification.

    Returns:
        The recorded outcome dict.
    """
    outcome_type = classify_session(journal_content, mission_title=mission_title)
    summary = _extract_summary(journal_content)

    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "mode": mode,
        "duration_minutes": duration_minutes,
        "outcome": outcome_type,
        "summary": summary,
        "mission_type": classify_mission_type(mission_title),
        "has_pr": _detect_pr_created(journal_content),
        "has_branch": _detect_branch_pushed(journal_content),
    }

    outcomes_path = Path(instance_dir) / "session_outcomes.json"

    # Load existing outcomes
    outcomes = _load_outcomes(outcomes_path)

    # Append and cap
    outcomes.append(entry)
    if len(outcomes) > MAX_OUTCOMES:
        outcomes = outcomes[-MAX_OUTCOMES:]

    # Write atomically
    try:
        from app.utils import atomic_write
        atomic_write(outcomes_path, json.dumps(outcomes, indent=2))
    except Exception as e:
        print(f"[session_tracker] Failed to write outcomes: {e}", file=sys.stderr)

    return entry


def _load_outcomes(outcomes_path: Path) -> list:
    """Load outcomes from JSON file. Returns empty list on error."""
    if not outcomes_path.exists():
        return []
    try:
        data = json.loads(outcomes_path.read_text())
        if not isinstance(data, list):
            print(
                f"[session_tracker] Unexpected JSON type {type(data).__name__}, "
                "expected list — resetting",
                file=sys.stderr,
            )
            return []
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[session_tracker] Failed to read outcomes: {e}", file=sys.stderr)
        return []


def get_recent_outcomes(
    instance_dir: str,
    project: str,
    limit: int = 10,
    _all_outcomes: Optional[list] = None,
) -> List[dict]:
    """Get the last N outcomes for a project.

    Args:
        instance_dir: Path to instance directory.
        project: Project name to filter by.
        limit: Maximum number of outcomes to return.
        _all_outcomes: Pre-loaded outcomes list (avoids re-reading the file).

    Returns:
        List of outcome dicts, most recent last.
    """
    if _all_outcomes is None:
        outcomes_path = Path(instance_dir) / "session_outcomes.json"
        _all_outcomes = _load_outcomes(outcomes_path)

    project_outcomes = [o for o in _all_outcomes if o.get("project") == project]
    return project_outcomes[-limit:]


def get_staleness_score(
    instance_dir: str,
    project: str,
    _all_outcomes: Optional[list] = None,
) -> int:
    """Count consecutive empty/blocked sessions for a project.

    Counts backwards from the most recent session. Stops at the first
    productive session.

    Args:
        instance_dir: Path to instance directory.
        project: Project name.
        _all_outcomes: Pre-loaded outcomes list (avoids re-reading the file).

    Returns:
        Number of consecutive non-productive sessions. 0 = fresh.
    """
    recent = get_recent_outcomes(instance_dir, project, limit=20,
                                 _all_outcomes=_all_outcomes)
    if not recent:
        return 0

    count = 0
    for outcome in reversed(recent):
        if outcome.get("outcome") == "productive":
            break
        count += 1

    return count


def get_staleness_warning(instance_dir: str, project: str) -> str:
    """Generate a human-readable staleness warning if appropriate.

    Args:
        instance_dir: Path to instance directory.
        project: Project name.

    Returns:
        Warning string, or empty string if project is fresh.
    """
    # Load outcomes once for both staleness score and recent outcomes lookup
    outcomes_path = Path(instance_dir) / "session_outcomes.json"
    all_outcomes = _load_outcomes(outcomes_path)

    score = get_staleness_score(instance_dir, project,
                                 _all_outcomes=all_outcomes)
    if score < 3:
        return ""

    recent = get_recent_outcomes(instance_dir, project, limit=score + 1,
                                  _all_outcomes=all_outcomes)
    empty_summaries = [
        o.get("summary", "")
        for o in recent[-score:]
        if o.get("outcome") != "productive"
    ]

    # Build contextual warning
    if score >= 5:
        intensity = "CRITICAL"
        advice = (
            "This project has had {score} consecutive non-productive sessions. "
            "STOP doing verification/housekeeping. Either:\n"
            "  1. Find genuinely NEW work (a bug, a missing feature, an architectural issue)\n"
            "  2. Skip this project entirely and work on something else\n"
            "  3. Write a strategic proposal or analysis that adds real value"
        )
    elif score >= 3:
        intensity = "WARNING"
        advice = (
            "Last {score} sessions found nothing actionable. "
            "Avoid repeating the same checks. Look for genuinely new work, "
            "or consider that this project may not need attention right now."
        )
    else:
        return ""

    lines = [
        f"### {intensity}: Project Staleness Detected",
        "",
        advice.format(score=score),
        "",
    ]

    if empty_summaries:
        lines.append("Recent non-productive sessions:")
        for s in empty_summaries[-3:]:  # Show last 3
            if s:
                lines.append(f"  - {s[:100]}")
        lines.append("")

    return "\n".join(lines)


def get_project_freshness(
    instance_dir: str,
    projects: List[Tuple[str, str]],
    _all_outcomes: Optional[list] = None,
) -> Dict[str, int]:
    """Get freshness scores for all projects (for weighted selection).

    Returns a dict mapping project name to a weight (higher = fresher).
    Fresh projects get weight 10, stale projects get progressively less.
    Projects with staleness >= 5 get weight 1 (minimal chance).

    Args:
        instance_dir: Path to instance directory.
        projects: List of (name, path) tuples.
        _all_outcomes: Pre-loaded outcomes list (avoids re-reading the file).

    Returns:
        Dict mapping project name to weight (1-10).
    """
    if _all_outcomes is None:
        outcomes_path = Path(instance_dir) / "session_outcomes.json"
        _all_outcomes = _load_outcomes(outcomes_path)

    weights = {}
    for name, _ in projects:
        score = get_staleness_score(instance_dir, name,
                                     _all_outcomes=_all_outcomes)
        if score == 0:
            weights[name] = 10
        elif score == 1:
            weights[name] = 8
        elif score == 2:
            weights[name] = 6
        elif score <= 4:
            weights[name] = 3
        else:
            weights[name] = 1  # Heavily deprioritized

    return weights


def get_last_session_timestamp(
    instance_dir: str,
    project: str,
    _all_outcomes: Optional[list] = None,
) -> Optional[str]:
    """Get the ISO timestamp of the most recent session for a project.

    Args:
        instance_dir: Path to instance directory.
        project: Project name.
        _all_outcomes: Pre-loaded outcomes list (avoids re-reading the file).

    Returns:
        ISO timestamp string, or None if no sessions found.
    """
    recent = get_recent_outcomes(instance_dir, project, limit=1,
                                 _all_outcomes=_all_outcomes)
    if not recent:
        return None
    return recent[-1].get("timestamp")


def _count_commits_since(project_path: str, since_iso: str) -> int:
    """Count commits on the default branch since a given ISO timestamp.

    Uses ``git log --oneline --since`` to count new commits. Runs in
    the project directory. Returns -1 on error (missing repo, bad path).

    Results are cached for 5 minutes (``_COMMITS_CACHE_TTL``) since commit
    counts change rarely but are queried on every autonomous iteration.

    Args:
        project_path: Path to the project's git repository.
        since_iso: ISO 8601 timestamp (e.g. "2026-03-01T10:00:00").

    Returns:
        Number of commits since the timestamp, or -1 on error.
    """
    cache_key = (project_path, since_iso)
    now = time.monotonic()

    cached = _commits_cache.get(cache_key)
    if cached is not None:
        value, cached_at = cached
        if now - cached_at < _COMMITS_CACHE_TTL:
            return value

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--since={since_iso}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            count = -1
        else:
            lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
            count = len(lines)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        count = -1

    _commits_cache[cache_key] = (count, now)
    return count


def get_project_drift(
    instance_dir: str,
    projects: List[Tuple[str, str]],
    _all_outcomes: Optional[list] = None,
) -> Dict[str, int]:
    """Measure how much each project has drifted since the agent's last session.

    For each project, finds the last session timestamp and counts commits
    on the default branch since then. Projects with no prior sessions get
    drift = 0 (no baseline to compare against).

    Args:
        instance_dir: Path to instance directory.
        projects: List of (name, path) tuples.
        _all_outcomes: Pre-loaded outcomes list (avoids re-reading the file).

    Returns:
        Dict mapping project name to commit count since last session.
        Values are >= 0 (errors mapped to 0).
    """
    if _all_outcomes is None:
        outcomes_path = Path(instance_dir) / "session_outcomes.json"
        _all_outcomes = _load_outcomes(outcomes_path)

    drift = {}
    for name, path in projects:
        ts = get_last_session_timestamp(instance_dir, name,
                                         _all_outcomes=_all_outcomes)
        if not ts or not path:
            drift[name] = 0
            continue
        count = _count_commits_since(path, ts)
        drift[name] = max(0, count)
    return drift


def get_drift_summary(
    instance_dir: str,
    project_name: str,
    project_path: str,
) -> str:
    """Generate a human-readable drift summary for a single project.

    Used by prompt_builder to inject context about how much the project
    has changed since the agent last worked on it.

    Args:
        instance_dir: Path to instance directory.
        project_name: Project name.
        project_path: Path to the project directory.

    Returns:
        Summary string, or empty string if no significant drift.
    """
    ts = get_last_session_timestamp(instance_dir, project_name)
    if not ts or not project_path:
        return ""

    count = _count_commits_since(project_path, ts)
    if count <= 0:
        return ""

    # Only report meaningful drift (3+ commits)
    if count < 3:
        return ""

    # Parse the timestamp for display
    try:
        dt = datetime.fromisoformat(ts)
        days_ago = (datetime.now() - dt).days
        if days_ago == 0:
            time_desc = "today"
        elif days_ago == 1:
            time_desc = "yesterday"
        else:
            time_desc = f"{days_ago} days ago"
    except (ValueError, TypeError):
        time_desc = "recently"

    # Get brief log of recent changes
    recent_log = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--since={ts}", "-5"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()[:5]
            recent_log = "\n".join(f"  - {l.strip()}" for l in lines)
    except (subprocess.TimeoutExpired, OSError):
        pass

    summary_lines = [
        f"### Project Drift Detected",
        "",
        f"**{count} commits** landed on main since your last session ({time_desc}).",
        "Review recent changes before starting work to avoid conflicts or duplication.",
    ]

    if recent_log:
        summary_lines.extend([
            "",
            "Recent commits:",
            recent_log,
        ])

    if count >= 15:
        summary_lines.extend([
            "",
            "**High drift** — consider reading CLAUDE.md and key files again "
            "before starting work. The codebase may have changed significantly.",
        ])

    return "\n".join(summary_lines)
