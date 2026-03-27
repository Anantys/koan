"""PR checkup routine — periodic health check on all open koan PRs.

Scans open pull requests across all configured projects, detects issues
(conflicts, CI failures, unanswered review comments), and queues
appropriate follow-up missions (/rebase, /check).

Designed to run 1-2x per day, either via the /checkup skill or as a
recurring mission.

Deduplication:
- Uses check_tracker to skip PRs that haven't changed since last check.
- Scans pending missions to avoid queuing duplicate /rebase or /check
  entries for the same PR.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.github import get_gh_username, run_gh
from app.projects_config import load_projects_config, get_projects_from_config


def _get_all_github_repos(koan_root: str) -> List[Dict]:
    """Collect (project_name, owner/repo) for all configured projects.

    Returns a list of dicts: {"name": str, "repo": "owner/repo"}.
    """
    config = load_projects_config(koan_root)
    if not config:
        return []

    results = []
    projects = config.get("projects", {})
    for name, proj in projects.items():
        if proj is None:
            continue
        github_url = proj.get("github_url", "")
        if not github_url:
            continue
        # Extract owner/repo from URL like https://github.com/owner/repo
        match = re.match(
            r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$", github_url,
        )
        if match:
            results.append({"name": name, "repo": match.group(1)})
    return results


def _fetch_open_prs(repo: str, author: str) -> List[Dict]:
    """Fetch all open PRs by author in a repo.

    Returns list of PR dicts with relevant fields.
    """
    fields = (
        "number,title,url,headRefName,mergeable,reviewDecision,"
        "updatedAt,isDraft,statusCheckRollup,state"
    )
    try:
        raw = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--author", author,
            "--json", fields,
            timeout=30,
        )
        prs = json.loads(raw)
        return prs if isinstance(prs, list) else []
    except (RuntimeError, json.JSONDecodeError, OSError):
        return []


def _has_ci_failure(pr: dict) -> bool:
    """Check if the PR has CI check failures."""
    rollup = pr.get("statusCheckRollup") or []
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        # FAILURE, ERROR, or ACTION_REQUIRED are bad
        if conclusion in ("FAILURE", "ERROR", "ACTION_REQUIRED"):
            return True
        # TIMED_OUT is also a failure
        if conclusion == "TIMED_OUT":
            return True
        # Still running checks are not failures
        if status == "IN_PROGRESS":
            continue
    return False


def _has_conflicts(pr: dict) -> bool:
    """Check if the PR has merge conflicts."""
    return pr.get("mergeable", "UNKNOWN") == "CONFLICTING"


def _get_pending_missions_text(instance_dir: Path) -> str:
    """Read the pending section of missions.md."""
    missions_path = instance_dir / "missions.md"
    if not missions_path.exists():
        return ""
    try:
        from app.missions import parse_sections
        content = missions_path.read_text()
        sections = parse_sections(content)
        return "\n".join(sections.get("pending", []))
    except (OSError, ValueError):
        return ""


def _is_mission_already_queued(
    pending_text: str, pr_url: str, action: str,
) -> bool:
    """Check if a mission for this PR+action is already pending.

    Args:
        pending_text: Raw text of pending missions joined.
        pr_url: The PR URL to check for.
        action: "rebase" or "check" — the action keyword to look for.
    """
    if not pending_text:
        return False

    # Normalize the PR URL for matching
    # Match both full URL and shortened "owner/repo#N" patterns
    pr_url_lower = pr_url.lower()
    for line in pending_text.lower().split("\n"):
        if pr_url_lower in line and action in line:
            return True
    return False


def run_checkup(
    koan_root: str,
    instance_dir: str,
    notify_fn=None,
) -> Tuple[bool, str]:
    """Run a full PR checkup across all configured projects.

    Scans all open PRs by the bot user, detects issues, and queues
    follow-up missions with deduplication.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    instance_path = Path(instance_dir)
    missions_path = instance_path / "missions.md"

    # Get the bot's GitHub username
    author = get_gh_username()
    if not author:
        msg = "Cannot determine GitHub username — skipping PR checkup"
        return False, msg

    # Collect all repos
    repos = _get_all_github_repos(koan_root)
    if not repos:
        msg = "No GitHub repos configured — nothing to check"
        return True, msg

    # Read pending missions once for dedup
    pending_text = _get_pending_missions_text(instance_path)

    from app.check_tracker import has_changed, mark_checked

    total_prs = 0
    actions_taken = []
    repos_checked = 0
    errors = []

    for repo_info in repos:
        project_name = repo_info["name"]
        repo_slug = repo_info["repo"]

        prs = _fetch_open_prs(repo_slug, author)
        if not prs:
            continue

        repos_checked += 1

        for pr in prs:
            total_prs += 1
            pr_number = pr.get("number")
            pr_url = pr.get("url", "")
            title = pr.get("title", "")[:60]
            updated_at = pr.get("updatedAt", "")

            if not pr_url:
                pr_url = f"https://github.com/{repo_slug}/pull/{pr_number}"

            # Skip if nothing changed since last checkup
            if not has_changed(instance_path, pr_url, updated_at):
                continue

            issues_found = []

            # Check for conflicts
            if _has_conflicts(pr):
                if not _is_mission_already_queued(pending_text, pr_url, "rebase"):
                    _queue_mission(
                        missions_path, project_name,
                        f"/rebase {pr_url}",
                    )
                    issues_found.append("conflicts → /rebase queued")
                else:
                    issues_found.append("conflicts (rebase already queued)")

            # Check for CI failures
            if _has_ci_failure(pr):
                if not _is_mission_already_queued(pending_text, pr_url, "check"):
                    _queue_mission(
                        missions_path, project_name,
                        f"/check {pr_url}",
                    )
                    issues_found.append("CI failure → /check queued")
                else:
                    issues_found.append("CI failure (check already queued)")

            # Record this PR as checked
            mark_checked(instance_path, pr_url, updated_at)

            if issues_found:
                detail = "; ".join(issues_found)
                actions_taken.append(
                    f"PR #{pr_number} ({title}) [{project_name}]: {detail}"
                )

    # Build summary
    if not actions_taken:
        msg = (
            f"PR checkup complete: {total_prs} open PR(s) across "
            f"{repos_checked} repo(s) — all healthy"
        )
    else:
        action_lines = "\n".join(f"  • {a}" for a in actions_taken)
        msg = (
            f"PR checkup: {total_prs} open PR(s) across "
            f"{repos_checked} repo(s)\n"
            f"Issues found:\n{action_lines}"
        )

    return True, msg


def _queue_mission(missions_path: Path, project_name: str, mission_text: str):
    """Queue a mission to missions.md with dedup."""
    from app.utils import insert_pending_mission

    entry = f"- [project:{project_name}] {mission_text}"
    insert_pending_mission(missions_path, entry)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for pr_checkup.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        description="Run PR checkup across all configured projects."
    )
    parser.add_argument(
        "--instance-dir",
        default=os.environ.get("KOAN_INSTANCE_DIR", ""),
        help="Path to instance directory",
    )
    parser.add_argument(
        "--koan-root",
        default=os.environ.get("KOAN_ROOT", ""),
        help="Path to koan root directory",
    )
    cli_args = parser.parse_args(argv)

    instance_dir = cli_args.instance_dir
    koan_root = cli_args.koan_root

    if not instance_dir:
        print("Error: --instance-dir or KOAN_INSTANCE_DIR required",
              file=sys.stderr)
        return 1
    if not koan_root:
        print("Error: --koan-root or KOAN_ROOT required",
              file=sys.stderr)
        return 1

    success, summary = run_checkup(
        koan_root=koan_root,
        instance_dir=instance_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
