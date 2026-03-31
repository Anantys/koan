"""Koan /branches skill -- list koan branches + open PRs with merge recommendations."""

import json
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


def handle(ctx):
    """Handle /branches command.

    Lists koan/* branches and open PRs, recommends merge order based on
    size, age, review status, and conflict risk.
    """
    args = ctx.args.strip() if ctx.args else ""

    # Resolve project path
    project_name, project_path = _resolve_project(args, ctx)
    if not project_path:
        if project_name.startswith("_prompt_"):
            names = project_name[len("_prompt_"):]
            return f"Which project? Usage: /branches <project>\nAvailable: {names}"
        return "No project found. Usage: /branches <project_name>"

    # Gather data
    branches_info = _get_branches_info(project_path)
    prs_info = _get_open_prs(project_path)

    if not branches_info and not prs_info:
        return f"No koan branches or open PRs for {project_name}."

    # Cross-reference branches and PRs
    enriched = _enrich_and_merge(branches_info, prs_info)

    # Sort by recommended merge order
    ordered = _recommend_merge_order(enriched)

    # Format output
    return _format_output(project_name, ordered)


def _resolve_project(args: str, ctx) -> Tuple[str, Optional[str]]:
    """Resolve project name and path from args or context.

    A project name argument is required when multiple projects exist.
    When only one project is configured, it is used automatically.
    """
    from app.utils import get_known_projects

    projects = get_known_projects()  # list of (name, path) tuples
    if not projects:
        return "", None

    # Build a dict for easy lookup
    proj_dict = dict(projects)

    if args:
        # Match by name
        for name, path in proj_dict.items():
            if name.lower() == args.lower():
                return name, path
        return args, None

    # No args: auto-select only when there's a single project
    if len(proj_dict) == 1:
        name = next(iter(proj_dict))
        return name, proj_dict[name]

    # Multiple projects: require explicit selection
    names = ", ".join(sorted(proj_dict.keys()))
    return f"_prompt_{names}", None


def _get_branches_info(project_path: str) -> List[Dict]:
    """Get info about local koan/* branches."""
    from app.config import get_branch_prefix
    from app.git_utils import run_git

    prefix = get_branch_prefix()
    branches = []

    # List local branches
    rc, output, _ = run_git("branch", "--list", f"{prefix}*", cwd=project_path)
    if rc != 0 or not output:
        return []

    for line in output.splitlines():
        name = line.strip().lstrip("* ")
        if not name.startswith(prefix):
            continue
        branches.append(name)

    if not branches:
        return []

    # Batch fetch age/timestamp via single for-each-ref (O(1) instead of O(N))
    # Use TAB delimiter to handle spaces in relative dates like "3 days ago"
    rc, ref_output, _ = run_git(
        "for-each-ref",
        "--format=%(committerdate:unix)\t%(committerdate:relative)\t%(refname:short)",
        f"refs/heads/{prefix}*",
        cwd=project_path,
    )

    age_data = {}  # branch_name -> {"timestamp": int, "age": str}
    if rc == 0 and ref_output:
        for line in ref_output.splitlines():
            parts = line.strip().split("\t", 2)
            if len(parts) == 3:
                ts_str, relative, ref_name = parts
                try:
                    age_data[ref_name] = {
                        "timestamp": int(ts_str),
                        "age": relative,
                    }
                except ValueError:
                    pass


    result = []
    for branch in sorted(branches):
        info = {"branch": branch, "has_pr": False}

        # Commit count ahead of main
        rc, ahead, _ = run_git(
            "rev-list", "--count", f"origin/main..{branch}",
            cwd=project_path, timeout=5,
        )
        if rc == 0 and ahead.strip().isdigit():
            info["commits"] = int(ahead.strip())
        else:
            info["commits"] = 0

        # Age and timestamp from batch for-each-ref data
        ref = age_data.get(branch, {})
        info["age"] = ref.get("age", "")
        info["timestamp"] = ref.get("timestamp", 0)

        # Skip branches fully merged into origin/main (0 commits ahead)
        if info["commits"] == 0:
            continue

        # Diff stat (additions + deletions)
        rc, stat, _ = run_git(
            "diff", "--shortstat", f"origin/main...{branch}",
            cwd=project_path, timeout=10,
        )
        if rc == 0 and stat.strip():
            info["diffstat"] = _parse_shortstat(stat.strip())
        else:
            info["diffstat"] = (0, 0, 0)

        info["conflicts"] = _check_conflicts(project_path, branch)

        result.append(info)

    return result


def _check_conflicts(project_path: str, branch: str) -> Optional[bool]:
    """Check if a branch would conflict when merged into main.

    Returns True if conflicts detected, False if clean, None if
    the check failed (merge-base error, timeout, etc.).
    """
    import subprocess

    try:
        # Get merge-base
        result = subprocess.run(
            ["git", "merge-base", "origin/main", branch],
            capture_output=True, text=True, cwd=project_path, timeout=5,
        )
        if result.returncode != 0:
            return None  # Can't determine

        base = result.stdout.strip()
        if not base:
            return None

        # Use merge-tree to simulate merge
        result = subprocess.run(
            ["git", "merge-tree", base, "origin/main", branch],
            capture_output=True, text=True, cwd=project_path, timeout=10,
        )
        # merge-tree outputs conflict markers if there are conflicts
        return "<<<<<<" in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def _parse_shortstat(stat: str) -> Tuple[int, int, int]:
    """Parse git diff --shortstat output into (files, insertions, deletions)."""
    import re

    files = insertions = deletions = 0
    m = re.search(r"(\d+) file", stat)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+) insertion", stat)
    if m:
        insertions = int(m.group(1))
    m = re.search(r"(\d+) deletion", stat)
    if m:
        deletions = int(m.group(1))
    return files, insertions, deletions


def _get_open_prs(project_path: str) -> List[Dict]:
    """Get open PRs for the repo via gh CLI."""
    try:
        from app.github import run_gh

        raw = run_gh(
            "pr", "list",
            "--state", "open",
            "--limit", "50",
            "--json", "number,title,headRefName,additions,deletions,createdAt,"
                      "isDraft,reviewDecision,reviews,labels,url",
            cwd=project_path,
            timeout=30,
        )
    except (RuntimeError, OSError) as exc:
        log.debug("Failed to list PRs: %s", exc)
        return []

    try:
        prs = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []

    if not isinstance(prs, list):
        return []

    result = []
    for pr in prs:
        info = {
            "number": pr.get("number", 0),
            "title": pr.get("title", ""),
            "branch": pr.get("headRefName", ""),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "created_at": pr.get("createdAt", ""),
            "is_draft": pr.get("isDraft", False),
            "review_decision": pr.get("reviewDecision", ""),
            "has_reviews": bool(pr.get("reviews")),
            "labels": [l.get("name", "") for l in (pr.get("labels") or [])],
            "url": pr.get("url", ""),
        }
        result.append(info)

    return result


def _enrich_and_merge(
    branches: List[Dict],
    prs: List[Dict],
) -> List[Dict]:
    """Cross-reference branches and PRs into unified entries."""
    # Index PRs by branch name
    pr_by_branch = {}
    for pr in prs:
        pr_by_branch[pr["branch"]] = pr

    enriched = []

    # Process branches (may or may not have PRs)
    seen_branches = set()
    for branch_info in branches:
        name = branch_info["branch"]
        seen_branches.add(name)

        entry = dict(branch_info)
        if name in pr_by_branch:
            pr = pr_by_branch[name]
            entry["has_pr"] = True
            entry["pr_number"] = pr["number"]
            entry["pr_title"] = pr["title"]
            entry["pr_additions"] = pr["additions"]
            entry["pr_deletions"] = pr["deletions"]
            entry["pr_is_draft"] = pr["is_draft"]
            entry["pr_review_decision"] = pr["review_decision"]
            entry["pr_has_reviews"] = pr["has_reviews"]
            entry["pr_labels"] = pr["labels"]
            entry["pr_url"] = pr.get("url", "")
        enriched.append(entry)

    # PRs without local branches (from other contributors/forks)
    from app.config import get_branch_prefix
    prefix = get_branch_prefix()

    for pr in prs:
        branch = pr["branch"]
        if branch not in seen_branches and branch.startswith(prefix):
            entry = {
                "branch": branch,
                "has_pr": True,
                "commits": 0,
                "age": "",
                "timestamp": 0,
                "diffstat": (0, 0, 0),
                "conflicts": False,
                "pr_number": pr["number"],
                "pr_title": pr["title"],
                "pr_additions": pr["additions"],
                "pr_deletions": pr["deletions"],
                "pr_is_draft": pr["is_draft"],
                "pr_review_decision": pr["review_decision"],
                "pr_has_reviews": pr["has_reviews"],
                "pr_labels": pr["labels"],
                "pr_url": pr.get("url", ""),
            }
            enriched.append(entry)

    return enriched


def _merge_score(entry: Dict) -> Tuple:
    """Score an entry for merge priority (lower = merge first).

    Criteria (in order):
    1. Approved PRs first
    2. No conflicts first
    3. Smaller changes first (fewer total lines)
    4. Older branches first (lower timestamp = older)
    """
    # Approved = top priority
    is_approved = entry.get("pr_review_decision") == "APPROVED"
    has_reviews = entry.get("pr_has_reviews", False)

    # Size: total lines changed
    if entry.get("has_pr"):
        size = entry.get("pr_additions", 0) + entry.get("pr_deletions", 0)
    else:
        _, ins, dels = entry.get("diffstat", (0, 0, 0))
        size = ins + dels

    conflict_status = entry.get("conflicts")
    timestamp = entry.get("timestamp", 0)

    # Conflict sort: 0 = clean, 1 = unknown, 2 = conflicts
    if conflict_status is True:
        conflict_score = 2
    elif conflict_status is None:
        conflict_score = 1
    else:
        conflict_score = 0

    return (
        0 if is_approved else (1 if has_reviews else 2),  # review status
        conflict_score,                                    # conflicts
        size,                                               # change size
        timestamp,                                          # age (older first)
    )


def _recommend_merge_order(entries: List[Dict]) -> List[Dict]:
    """Sort entries by recommended merge order."""
    return sorted(entries, key=_merge_score)


def _format_output(project_name: str, entries: List[Dict]) -> str:
    """Format the final Telegram-friendly output."""
    if not entries:
        return f"No koan branches for {project_name}."

    lines = [f"Branches & PRs ({project_name})"]
    lines.append(f"{len(entries)} branch(es) to review\n")

    lines.append("Recommended merge order:")

    for i, entry in enumerate(entries, 1):
        branch = entry["branch"]
        short_branch = branch.split("/", 1)[-1] if "/" in branch else branch

        # Status indicators
        indicators = []

        if entry.get("has_pr"):
            pr_num = entry.get("pr_number", "?")
            if entry.get("pr_is_draft"):
                indicators.append(f"PR #{pr_num} draft")
            else:
                indicators.append(f"PR #{pr_num}")

            decision = entry.get("pr_review_decision", "")
            if decision == "APPROVED":
                indicators.append("approved")
            elif decision == "CHANGES_REQUESTED":
                indicators.append("changes requested")
            elif entry.get("pr_has_reviews"):
                indicators.append("reviewed")
        else:
            indicators.append("no PR")

        conflict_status = entry.get("conflicts")
        if conflict_status is True:
            indicators.append("conflicts")
        elif conflict_status is None:
            indicators.append("conflicts unknown")

        # Size info
        if entry.get("has_pr"):
            adds = entry.get("pr_additions", 0)
            dels = entry.get("pr_deletions", 0)
            size_str = f"+{adds}/-{dels}"
        else:
            _, ins, dels = entry.get("diffstat", (0, 0, 0))
            size_str = f"+{ins}/-{dels}"

        # Age
        age = entry.get("age", "")

        # Build line
        status = ", ".join(indicators)
        title = entry.get("pr_title", "")

        pr_url = entry.get("pr_url", "")

        if title:
            lines.append(f"\n{i}. {short_branch}")
            lines.append(f"   {title}")
            lines.append(f"   {size_str} | {age} | {status}")
        else:
            lines.append(f"\n{i}. {short_branch}")
            lines.append(f"   {size_str} | {age} | {status}")

        if pr_url:
            lines.append(f"   {pr_url}")

    # Summary stats
    total_prs = sum(1 for e in entries if e.get("has_pr"))
    approved = sum(1 for e in entries if e.get("pr_review_decision") == "APPROVED")
    with_conflicts = sum(1 for e in entries if e.get("conflicts") is True)
    drafts = sum(1 for e in entries if e.get("pr_is_draft"))
    no_pr = sum(1 for e in entries if not e.get("has_pr"))

    lines.append("\n---")
    stats = []
    if approved:
        stats.append(f"{approved} approved")
    if drafts:
        stats.append(f"{drafts} draft")
    if no_pr:
        stats.append(f"{no_pr} no PR")
    if with_conflicts:
        stats.append(f"{with_conflicts} with conflicts")

    lines.append(f"PRs: {total_prs} open | " + " | ".join(stats))

    return "\n".join(lines)
