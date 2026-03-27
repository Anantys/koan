"""Koan fix skill -- queue a fix mission for a GitHub issue."""

import re
from typing import Optional, Tuple

from app.github_url_parser import parse_issue_url
from app.github_skill_helpers import (
    handle_github_skill,
    resolve_project_for_repo,
    format_project_not_found_error,
    queue_github_mission,
)


_LIMIT_PATTERN = re.compile(r'--limit[=\s]+(\d+)', re.IGNORECASE)


def _parse_repo_url(args: str) -> Optional[Tuple[str, str, str]]:
    """Try to extract a repo-only URL (no issue/PR number) from args.

    Returns (url, owner, repo) or None if args contain an issue/PR URL
    or no valid repo URL.
    """
    # If there's already an issue or PR URL, don't treat as batch
    if re.search(r'github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d+', args):
        return None

    match = re.search(r'https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?(?=/|\s|$)', args)
    if not match:
        return None

    owner = match.group(1)
    repo = match.group(2)
    url = f"https://github.com/{owner}/{repo}"

    # Reject if the "repo" part looks like a sub-path (issues, pull, etc.)
    if repo in ("issues", "pull", "pulls", "actions", "settings", "wiki"):
        return None

    return url, owner, repo


def _parse_limit(args: str) -> Optional[int]:
    """Extract --limit=N from args. Returns None if not specified."""
    match = _LIMIT_PATTERN.search(args)
    if match:
        return int(match.group(1))
    return None


def _list_open_issues(owner: str, repo: str, limit: Optional[int] = None) -> list:
    """List open issues from a GitHub repo using gh CLI.

    Returns list of dicts with 'number', 'title', and 'url' keys,
    ordered by most recently created first.
    """
    import json
    from app.github import run_gh

    gh_limit = str(limit) if limit else "100"
    output = run_gh(
        "issue", "list",
        "--repo", f"{owner}/{repo}",
        "--state", "open",
        "--limit", gh_limit,
        "--json", "number,title,url",
    )
    if not output.strip():
        return []
    return json.loads(output)


def handle(ctx):
    """Handle /fix command -- queue a mission to fix a GitHub issue.

    Usage:
        /fix https://github.com/owner/repo/issues/42
        /fix https://github.com/owner/repo/issues/42 focus on backend only
        /fix https://github.com/owner/repo              (batch: all open issues)
        /fix https://github.com/owner/repo --limit=5    (batch: 5 most recent)
    """
    args = ctx.args.strip() if ctx.args else ""

    # Check for batch mode: repo URL without issue number
    repo_match = _parse_repo_url(args)
    if repo_match:
        return _handle_batch(ctx, args, repo_match)

    # Single issue mode: delegate to existing handler
    return handle_github_skill(
        ctx,
        command="fix",
        url_type="issue",
        parse_func=parse_issue_url,
        success_prefix="Fix queued",
    )


def _handle_batch(ctx, args: str, repo_match: Tuple[str, str, str]) -> str:
    """Handle batch /fix: list issues from repo and queue a fix for each."""
    url, owner, repo = repo_match
    limit = _parse_limit(args)

    # Resolve to local project
    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    # Fetch open issues
    try:
        issues = _list_open_issues(owner, repo, limit=limit)
    except (RuntimeError, ValueError) as e:
        return f"\u274c Failed to list issues for {owner}/{repo}: {e}"

    if not issues:
        return f"No open issues found in {owner}/{repo}."

    # Queue a /fix mission for each issue
    queued = 0
    for issue in issues:
        issue_url = issue.get("url") or f"https://github.com/{owner}/{repo}/issues/{issue['number']}"
        queue_github_mission(ctx, "fix", issue_url, project_name)
        queued += 1

    limit_note = f" (limited to {limit})" if limit else ""
    return f"Queued {queued} /fix missions for {owner}/{repo}{limit_note}."
