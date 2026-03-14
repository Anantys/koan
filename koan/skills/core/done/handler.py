"""Koan done skill — list merged and open PRs from the last 24 hours."""

import json
import re
from datetime import datetime, timedelta, timezone


def handle(ctx):
    """Handle /done command — list recently merged and open PRs across projects."""
    args = ctx.args.strip() if ctx.args else ""
    project_filter, hours = _parse_args(args)

    from app.github import get_gh_username, run_gh
    from app.utils import get_known_projects

    author = get_gh_username()
    if not author:
        return "Cannot determine GitHub username. Check GH_TOKEN or GITHUB_USER."

    projects = get_known_projects()
    if not projects:
        return "No projects configured."

    # Filter to specific project if requested
    if project_filter:
        matched = [
            (n, p) for n, p in projects if n.lower() == project_filter.lower()
        ]
        if not matched:
            return f"Project '{project_filter}' not found."
        projects = matched

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # {project_name: {"merged": [...], "open": [...]}}
    by_project = {}

    for name, path in projects:
        repo = _get_repo_slug(name, path)
        if not repo:
            continue

        merged = _fetch_merged_prs(repo, author, since)
        opened = _fetch_open_prs(repo, author, since)

        if merged or opened:
            by_project[name] = {"merged": merged, "open": opened}

    if not by_project:
        period = f"{hours}h" if hours != 24 else "24h"
        scope = f" for {project_filter}" if project_filter else ""
        return f"No activity in the last {period}{scope}."

    return _format_output(by_project, hours)


def _parse_args(args):
    """Parse arguments: /done [project] [--hours=N].

    Returns:
        (project_name, hours)
    """
    project = ""
    hours = 24

    if not args:
        return project, hours

    for part in args.split():
        match = re.match(r"--hours=(\d+)", part)
        if match:
            hours = max(1, min(int(match.group(1)), 168))  # cap at 7 days
        elif not project:
            project = part

    return project, hours


def _get_repo_slug(project_name, project_path):
    """Get owner/repo slug for a project."""
    from app.utils import get_github_remote

    return get_github_remote(project_path)


def _fetch_merged_prs(repo, author, since):
    """Fetch merged PRs for a repo since a given datetime.

    Returns:
        List of dicts with keys: number, title, url, merged_at.
    """
    from app.github import run_gh

    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        output = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "merged",
            "--author", author,
            "--search", f"merged:>={since_str}",
            "--json", "number,title,url,mergedAt",
            "--limit", "50",
            timeout=15,
        )
    except (RuntimeError, OSError):
        return []

    if not output:
        return []

    try:
        prs = json.loads(output)
        if not isinstance(prs, list):
            return []
        result = []
        for pr in prs:
            merged_at = pr.get("mergedAt", "")
            if merged_at:
                try:
                    merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                    if merged_dt >= since:
                        result.append({
                            "number": pr.get("number", 0),
                            "title": pr.get("title", ""),
                            "url": pr.get("url", ""),
                            "merged_at": merged_at,
                        })
                except (ValueError, TypeError):
                    pass
        return result
    except (json.JSONDecodeError, TypeError):
        return []


def _fetch_open_prs(repo, author, since):
    """Fetch open PRs for a repo created since a given datetime.

    Returns:
        List of dicts with keys: number, title, url, created_at.
    """
    from app.github import run_gh

    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        output = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--author", author,
            "--search", f"created:>={since_str}",
            "--json", "number,title,url,createdAt",
            "--limit", "50",
            timeout=15,
        )
    except (RuntimeError, OSError):
        return []

    if not output:
        return []

    try:
        prs = json.loads(output)
        if not isinstance(prs, list):
            return []
        result = []
        for pr in prs:
            created_at = pr.get("createdAt", "")
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if created_dt >= since:
                        result.append({
                            "number": pr.get("number", 0),
                            "title": pr.get("title", ""),
                            "url": pr.get("url", ""),
                            "created_at": created_at,
                        })
                except (ValueError, TypeError):
                    pass
        return result
    except (json.JSONDecodeError, TypeError):
        return []


def _format_output(by_project, hours):
    """Format PR list for Telegram output, grouped by project."""
    period = f"{hours}h" if hours != 24 else "24h"

    total_merged = sum(len(v["merged"]) for v in by_project.values())
    total_open = sum(len(v["open"]) for v in by_project.values())

    # Build summary line
    parts = []
    if total_merged:
        parts.append(f"{total_merged} merged")
    if total_open:
        parts.append(f"{total_open} open")
    summary = ", ".join(parts)

    lines = [f"Work (last {period}): {summary}"]
    lines.append("")

    for project in sorted(by_project):
        data = by_project[project]
        lines.append(f"{project}:")

        for pr in data["merged"]:
            title = _truncate_title(pr["title"])
            lines.append(f"  ✅ #{pr['number']} {title}")

        for pr in data["open"]:
            title = _truncate_title(pr["title"])
            lines.append(f"  ⏳ #{pr['number']} {title}")

    return "\n".join(lines)


def _truncate_title(title):
    """Truncate title to 70 chars max."""
    if len(title) > 70:
        return title[:67] + "..."
    return title
