"""PR babysitting — automated monitoring of open PRs created by Kōan.

Discovers all open PRs authored by Kōan (matching branch_prefix), checks
each one for actionable state changes (CI failure, review comments, merge
conflicts, staleness), and queues targeted fix missions.

Main entry point: ``run_babysit()`` — called from iteration_manager once per
N iterations when ``pr_babysit.enabled`` is true in config.yaml.

Tracker file: ``instance/.babysit-tracker.json``
Schema per PR URL:
{
  "https://github.com/.../pull/N": {
    "last_checked_at": "ISO-8601",
    "last_ci_status": "SUCCESS" | "FAILURE" | "PENDING" | null,
    "last_review_decision": "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | null,
    "last_comment_count": int,
    "last_action": "none" | "fix" | "review" | "rebase" | "notify",
    "last_action_at": "ISO-8601" | null,
    "fix_attempts": int,
    "review_attempts": int,
    "rebase_attempts": int,
  }
}
"""

import fcntl
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Tracker helpers
# ---------------------------------------------------------------------------

_TRACKER_FILE = ".babysit-tracker.json"
_TRACKER_LOCK = ".babysit-tracker.lock"


def _tracker_path(instance_dir: Path) -> Path:
    return instance_dir / _TRACKER_FILE


def _load_tracker(instance_dir: Path) -> Dict[str, Any]:
    path = _tracker_path(instance_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tracker(instance_dir: Path, data: Dict[str, Any]):
    from app.utils import atomic_write
    path = _tracker_path(instance_dir)
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def _get_tracker_entry(instance_dir: Path, pr_url: str) -> Dict[str, Any]:
    return _load_tracker(instance_dir).get(pr_url, {})


def _update_tracker_entry(instance_dir: Path, pr_url: str, updates: Dict[str, Any]):
    """Atomically update a single PR's tracker entry."""
    lock_path = instance_dir / _TRACKER_LOCK
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _load_tracker(instance_dir)
            entry = data.get(pr_url, {})
            entry.update(updates)
            data[pr_url] = entry
            _save_tracker(instance_dir, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_babysit_config() -> Dict[str, Any]:
    """Load pr_babysit section from config.yaml."""
    try:
        from app.utils import load_config
        config = load_config()
        return config.get("pr_babysit", {}) or {}
    except (ImportError, OSError, ValueError):
        return {}


def _babysit_enabled() -> bool:
    return bool(_get_babysit_config().get("enabled", False))


def _get_check_interval(cfg: Dict[str, Any]) -> int:
    return int(cfg.get("check_interval", 3))


def _get_max_retries(cfg: Dict[str, Any]) -> int:
    return int(cfg.get("max_retries", 2))


def _get_cooldown_minutes(cfg: Dict[str, Any]) -> int:
    return int(cfg.get("cooldown_minutes", 60))


def _get_stale_days(cfg: Dict[str, Any]) -> int:
    return int(cfg.get("stale_days", 7))


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _run_gh(*args, timeout: int = 30) -> Optional[str]:
    """Run gh CLI, return stdout or None on error."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def discover_open_prs(projects_config: dict, branch_prefix: str) -> List[Dict[str, Any]]:
    """Fetch all open PRs created by Kōan (matching branch_prefix) across projects.

    Args:
        projects_config: Loaded projects.yaml config dict.
        branch_prefix: Branch prefix (e.g., "koan/").

    Returns:
        List of PR info dicts with keys:
            url, number, title, headRefName, owner, repo,
            updatedAt, reviewDecision, mergeStateStatus,
            statusCheckRollup (overall CI status), comments (count).
    """
    from app.projects_config import get_projects_from_config

    # Collect all GitHub URLs across projects
    github_urls: List[str] = []
    if projects_config:
        for name, path in get_projects_from_config(projects_config):
            project_cfg = projects_config.get("projects", {}).get(name, {}) or {}
            primary = project_cfg.get("github_url", "")
            if primary:
                github_urls.append(primary)
            for url in project_cfg.get("github_urls", []):
                if url and url not in github_urls:
                    github_urls.append(url)

    if not github_urls:
        return []

    prs: List[Dict[str, Any]] = []
    # Strip trailing slash from prefix for comparison
    prefix = branch_prefix.rstrip("/")

    for repo_url in github_urls:
        # Extract owner/repo from URL
        owner_repo = _extract_owner_repo(repo_url)
        if not owner_repo:
            continue
        owner, repo = owner_repo

        raw = _run_gh(
            "pr", "list",
            "--repo", f"{owner}/{repo}",
            "--state", "open",
            "--json",
            "number,title,headRefName,url,updatedAt,reviewDecision,"
            "mergeStateStatus,statusCheckRollup,comments,isDraft",
            "--limit", "50",
            timeout=30,
        )
        if not raw:
            continue

        try:
            pr_list = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for pr in pr_list:
            head = pr.get("headRefName", "")
            # Only babysit PRs created by this Kōan instance
            if not head.startswith(prefix + "/"):
                continue
            prs.append({
                "url": pr.get("url", ""),
                "number": pr.get("number", 0),
                "title": pr.get("title", ""),
                "headRefName": head,
                "owner": owner,
                "repo": repo,
                "updatedAt": pr.get("updatedAt", ""),
                "reviewDecision": pr.get("reviewDecision"),
                "mergeStateStatus": pr.get("mergeStateStatus"),
                "isDraft": pr.get("isDraft", False),
                "statusCheckRollup": _extract_ci_status(pr.get("statusCheckRollup")),
                "commentCount": len(pr.get("comments", [])),
            })

    return prs


def _extract_owner_repo(github_url: str) -> Optional[Tuple[str, str]]:
    """Parse owner/repo from a GitHub URL."""
    import re
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", github_url)
    if m:
        return m.group(1), m.group(2)
    return None


def _extract_ci_status(rollup) -> Optional[str]:
    """Normalize statusCheckRollup to SUCCESS / FAILURE / PENDING / None."""
    if not rollup:
        return None
    if isinstance(rollup, str):
        return rollup.upper() if rollup else None
    if isinstance(rollup, list):
        # List of check run objects — derive overall status
        statuses = [r.get("conclusion") or r.get("status", "") for r in rollup]
        statuses_upper = [s.upper() for s in statuses if s]
        if not statuses_upper:
            return None
        if any(s in ("FAILURE", "ERROR", "ACTION_REQUIRED", "TIMED_OUT") for s in statuses_upper):
            return "FAILURE"
        if any(s in ("IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "EXPECTED") for s in statuses_upper):
            return "PENDING"
        if all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in statuses_upper):
            return "SUCCESS"
        return "PENDING"
    return None


def _get_ci_failure_context(owner: str, repo: str, pr_number: int) -> str:
    """Fetch a brief description of which CI check(s) failed."""
    raw = _run_gh(
        "pr", "checks", str(pr_number),
        "--repo", f"{owner}/{repo}",
        timeout=30,
    )
    if not raw:
        return ""

    lines = raw.splitlines()
    failed = [l for l in lines if any(w in l.lower() for w in ("fail", "error", "timed_out"))]
    return "; ".join(failed[:3])  # first 3 failing checks


# ---------------------------------------------------------------------------
# PR health check
# ---------------------------------------------------------------------------

def check_pr_health(
    pr: Dict[str, Any],
    tracker_entry: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Inspect a single PR and return a list of actions to take.

    Each action is a dict with keys:
        type: "fix" | "review" | "rebase" | "notify"
        reason: human-readable description
        context: extra context for the mission text (may be empty)

    Args:
        pr: PR info dict from discover_open_prs().
        tracker_entry: Current tracker state for this PR.
        cfg: pr_babysit config dict.

    Returns:
        List of action dicts (empty if no action needed).
    """
    actions: List[Dict[str, str]] = []
    max_retries = _get_max_retries(cfg)
    pr_url = pr["url"]
    owner = pr["owner"]
    repo = pr["repo"]
    number = pr["number"]

    # Skip draft PRs with no CI results (fresh push, normal state)
    if pr.get("isDraft") and pr.get("statusCheckRollup") is None:
        return []

    ci_status = pr.get("statusCheckRollup")
    review_decision = pr.get("reviewDecision")
    merge_status = pr.get("mergeStateStatus", "")
    comment_count = pr.get("commentCount", 0)
    updated_at = pr.get("updatedAt", "")

    prev_ci = tracker_entry.get("last_ci_status")
    prev_review = tracker_entry.get("last_review_decision")
    prev_comments = tracker_entry.get("last_comment_count", 0)
    fix_attempts = tracker_entry.get("fix_attempts", 0)
    review_attempts = tracker_entry.get("review_attempts", 0)
    rebase_attempts = tracker_entry.get("rebase_attempts", 0)

    # 1. CI failure
    if ci_status == "FAILURE":
        already_addressed = prev_ci == "FAILURE" and fix_attempts > 0
        if not already_addressed and fix_attempts < max_retries:
            context = _get_ci_failure_context(owner, repo, number)
            actions.append({
                "type": "fix",
                "reason": f"CI check failed on PR #{number}",
                "context": context,
            })
        elif fix_attempts >= max_retries:
            # Reached retry cap — notify human
            if tracker_entry.get("last_action") != "notify_ci_cap":
                actions.append({
                    "type": "notify",
                    "notify_key": "notify_ci_cap",
                    "reason": (
                        f"CI still failing on PR #{number} after {fix_attempts} fix "
                        f"attempt(s). Manual intervention needed."
                    ),
                    "context": "",
                })

    # 2. Changes requested
    if review_decision == "CHANGES_REQUESTED":
        if review_attempts < max_retries:
            actions.append({
                "type": "review",
                "reason": f"Review requested changes on PR #{number}",
                "context": "",
            })
        elif tracker_entry.get("last_action") != "notify_review_cap":
            actions.append({
                "type": "notify",
                "notify_key": "notify_review_cap",
                "reason": (
                    f"PR #{number} still has unresolved review changes after "
                    f"{review_attempts} attempt(s)."
                ),
                "context": "",
            })

    # 3. New unresolved review comments (comment count increased)
    # Skip if we already queued a review action from check #2 above
    has_review_action = any(a["type"] == "review" for a in actions)
    if not has_review_action and comment_count > prev_comments and review_decision != "APPROVED":
        if review_attempts < max_retries:
            actions.append({
                "type": "review",
                "reason": (
                    f"New review comments on PR #{number} "
                    f"({comment_count - prev_comments} new)"
                ),
                "context": "",
            })

    # 4. Merge conflicts
    if merge_status in ("CONFLICTING", "DIRTY"):
        if rebase_attempts < max_retries:
            actions.append({
                "type": "rebase",
                "reason": f"Merge conflicts detected on PR #{number}",
                "context": "",
            })
        elif tracker_entry.get("last_action") != "notify_conflict_cap":
            actions.append({
                "type": "notify",
                "notify_key": "notify_conflict_cap",
                "reason": (
                    f"PR #{number} still has merge conflicts after "
                    f"{rebase_attempts} rebase attempt(s)."
                ),
                "context": "",
            })

    # 5. Staleness (no activity for N days)
    stale_days = _get_stale_days(cfg)
    if updated_at and stale_days > 0:
        try:
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days >= stale_days:
                if tracker_entry.get("last_action") != "notify_stale":
                    actions.append({
                        "type": "notify",
                        "notify_key": "notify_stale",
                        "reason": (
                            f"PR #{number} has had no activity for {age_days} day(s). "
                            f"Consider manual intervention."
                        ),
                        "context": "",
                    })
        except (ValueError, TypeError):
            pass

    return actions


# ---------------------------------------------------------------------------
# Mission deduplication
# ---------------------------------------------------------------------------

def _mission_already_queued(missions_path: Path, pr_url: str, action_type: str) -> bool:
    """Return True if a mission targeting this PR+action is already pending/in-progress."""
    if not missions_path.exists():
        return False

    content = missions_path.read_text()
    # Look for the PR URL in Pending or In Progress sections
    from app.missions import parse_sections
    sections = parse_sections(content)

    for section_name in ("pending", "in_progress"):
        for line in sections.get(section_name, []):
            if pr_url in line:
                # PR already has a pending/active mission — skip
                return True

    return False


def _is_in_cooldown(tracker_entry: Dict[str, Any], cooldown_minutes: int) -> bool:
    """Return True if last action was within cooldown_minutes."""
    last_at = tracker_entry.get("last_action_at")
    if not last_at:
        return False
    try:
        dt = datetime.fromisoformat(last_at)
        age_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        return age_minutes < cooldown_minutes
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Mission queuing
# ---------------------------------------------------------------------------

def _resolve_project_for_pr(pr: Dict[str, Any]) -> str:
    """Resolve project name from PR owner/repo."""
    from app.utils import project_name_for_path, resolve_project_path
    repo = pr.get("repo", "")
    owner = pr.get("owner", "")
    project_path = resolve_project_path(repo, owner=owner)
    if project_path:
        return project_name_for_path(project_path)
    return repo


def queue_fix_missions(
    pr: Dict[str, Any],
    actions: List[Dict[str, str]],
    missions_path: Path,
    instance_dir: Path,
    cfg: Dict[str, Any],
    notify_on_fix: bool = True,
) -> List[str]:
    """Queue missions for the given actions, avoiding duplicates.

    Returns:
        List of queued mission descriptions (for logging).
    """
    from app.utils import insert_pending_mission, append_to_outbox

    pr_url = pr["url"]
    project_name = _resolve_project_for_pr(pr)
    cooldown_minutes = _get_cooldown_minutes(cfg)
    tracker_entry = _get_tracker_entry(instance_dir, pr_url)
    queued = []

    if _is_in_cooldown(tracker_entry, cooldown_minutes):
        return []

    for action in actions:
        action_type = action["type"]
        reason = action["reason"]
        context = action.get("context", "")

        if action_type == "notify":
            # Write to outbox — no mission needed
            outbox_path = instance_dir / "outbox.md"
            append_to_outbox(outbox_path, f"🔍 Babysit: {reason}")
            notify_key = action.get("notify_key", "notify")
            _update_tracker_entry(instance_dir, pr_url, {
                "last_action": notify_key,
                "last_action_at": _now_iso(),
                "last_ci_status": pr.get("statusCheckRollup"),
                "last_review_decision": pr.get("reviewDecision"),
                "last_comment_count": pr.get("commentCount", 0),
                "last_checked_at": _now_iso(),
            })
            queued.append(f"notify: {reason}")
            continue

        # Check dedup
        if _mission_already_queued(missions_path, pr_url, action_type):
            continue

        # Build mission text
        if action_type == "fix":
            mission = f"- [project:{project_name}] /fix {pr_url}"
            if context:
                mission += f" — CI failure: {context}"
            attempts_key = "fix_attempts"
        elif action_type == "review":
            mission = f"- [project:{project_name}] /review {pr_url}"
            attempts_key = "review_attempts"
        elif action_type == "rebase":
            mission = f"- [project:{project_name}] /rebase {pr_url}"
            attempts_key = "rebase_attempts"
        else:
            continue

        insert_pending_mission(missions_path, mission)

        # Update tracker
        current_attempts = tracker_entry.get(attempts_key, 0)
        _update_tracker_entry(instance_dir, pr_url, {
            "last_action": action_type,
            "last_action_at": _now_iso(),
            "last_ci_status": pr.get("statusCheckRollup"),
            "last_review_decision": pr.get("reviewDecision"),
            "last_comment_count": pr.get("commentCount", 0),
            "last_checked_at": _now_iso(),
            attempts_key: current_attempts + 1,
        })
        # Refresh entry for next action in this loop
        tracker_entry = _get_tracker_entry(instance_dir, pr_url)

        queued.append(f"{action_type}: {reason}")

        if notify_on_fix:
            outbox_path = instance_dir / "outbox.md"
            append_to_outbox(
                outbox_path,
                f"🔍 Babysit queued `/{action_type}` for PR #{pr['number']}: {reason}",
            )

    if not actions:
        # Just update the checked timestamp
        _update_tracker_entry(instance_dir, pr_url, {
            "last_checked_at": _now_iso(),
            "last_ci_status": pr.get("statusCheckRollup"),
            "last_review_decision": pr.get("reviewDecision"),
            "last_comment_count": pr.get("commentCount", 0),
        })

    return queued


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_babysit(
    instance_dir: str,
    koan_root: str,
    branch_prefix: str,
    projects_config: Optional[dict],
) -> str:
    """Main entry point — called from iteration_manager.

    Discovers open PRs, checks each one, queues fix missions as needed.

    Args:
        instance_dir: Path to instance directory.
        koan_root: Path to KOAN_ROOT.
        branch_prefix: Branch prefix (e.g., "koan/").
        projects_config: Loaded projects.yaml config dict (may be None).

    Returns:
        Summary string for logging (empty string if nothing to report).
    """
    cfg = _get_babysit_config()
    instance = Path(instance_dir)
    missions_path = instance / "missions.md"
    notify_on_fix = bool(cfg.get("notify_on_fix", True))

    prs = discover_open_prs(projects_config or {}, branch_prefix)
    if not prs:
        return ""

    summaries: List[str] = []
    for pr in prs:
        pr_url = pr["url"]
        tracker_entry = _get_tracker_entry(instance, pr_url)

        actions = check_pr_health(pr, tracker_entry, cfg)
        if not actions:
            # Still update last_checked_at
            _update_tracker_entry(instance, pr_url, {
                "last_checked_at": _now_iso(),
                "last_ci_status": pr.get("statusCheckRollup"),
                "last_review_decision": pr.get("reviewDecision"),
                "last_comment_count": pr.get("commentCount", 0),
            })
            continue

        queued = queue_fix_missions(
            pr, actions, missions_path, instance, cfg,
            notify_on_fix=notify_on_fix,
        )
        summaries.extend(queued)

    if summaries:
        return f"{len(summaries)} action(s): {'; '.join(summaries[:3])}"
    return f"checked {len(prs)} PR(s), no action needed"


# ---------------------------------------------------------------------------
# Status query (used by /babysit skill)
# ---------------------------------------------------------------------------

def get_babysit_status(instance_dir: str) -> List[Dict[str, Any]]:
    """Return list of tracked PRs with their current babysit state.

    Each entry has: url, last_checked_at, last_ci_status,
    last_review_decision, last_action, last_action_at.
    """
    data = _load_tracker(Path(instance_dir))
    result = []
    for url, entry in data.items():
        result.append({"url": url, **entry})
    return result
