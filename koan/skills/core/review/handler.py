"""Kōan review skill -- queue a code review mission."""

import re
from typing import Optional, Tuple

from app.github_url_parser import parse_github_url
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
    if re.search(r'github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d+', args):
        return None

    match = re.search(r'https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?(?=/|\s|$)', args)
    if not match:
        return None

    owner = match.group(1)
    repo = match.group(2)
    url = f"https://github.com/{owner}/{repo}"

    if repo in ("issues", "pull", "pulls", "actions", "settings", "wiki"):
        return None

    return url, owner, repo


def _parse_limit(args: str) -> Optional[int]:
    """Extract --limit=N from args. Returns None if not specified."""
    match = _LIMIT_PATTERN.search(args)
    if match:
        return int(match.group(1))
    return None


def _list_open_prs(owner: str, repo: str, limit: Optional[int] = None) -> list:
    """List open pull requests from a GitHub repo using gh CLI.

    Returns list of dicts with 'number', 'title', and 'url' keys,
    ordered by most recently created first.
    """
    import json
    from app.github import run_gh

    gh_limit = str(limit) if limit else "100"
    output = run_gh(
        "pr", "list",
        "--repo", f"{owner}/{repo}",
        "--state", "open",
        "--limit", gh_limit,
        "--json", "number,title,url",
    )
    if not output.strip():
        return []
    return json.loads(output)


def handle(ctx):
    """Handle /review command -- queue a code review mission.

    Usage:
        /review https://github.com/owner/repo/pull/42
        /review https://github.com/owner/repo/issues/42
        /review https://github.com/owner/repo              (batch: all open PRs)
        /review https://github.com/owner/repo --limit=5    (batch: 5 most recent)
    """
    args = ctx.args.strip() if ctx.args else ""

    # Check for batch mode: repo URL without issue/PR number
    repo_match = _parse_repo_url(args)
    if repo_match:
        return _handle_batch(ctx, args, repo_match)

    # Single PR/issue mode: delegate to unified handler
    return handle_github_skill(
        ctx,
        command="review",
        url_type="pr-or-issue",
        parse_func=parse_github_url,
        success_prefix="Review queued",
    )


def _handle_batch(ctx, args: str, repo_match: Tuple[str, str, str]) -> str:
    """Handle batch /review: list open PRs from repo and queue a review for each."""
    url, owner, repo = repo_match
    limit = _parse_limit(args)

    # Resolve to local project
    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    # Fetch open PRs
    try:
        prs = _list_open_prs(owner, repo, limit=limit)
    except (RuntimeError, ValueError) as e:
        return f"\u274c Failed to list PRs for {owner}/{repo}: {e}"

    if not prs:
        return f"No open PRs found in {owner}/{repo}."

    # Queue a /review mission for each PR
    queued = 0
    for pr in prs:
        pr_url = pr.get("url") or f"https://github.com/{owner}/{repo}/pull/{pr['number']}"
        queue_github_mission(ctx, "review", pr_url, project_name)
        queued += 1

    limit_note = f" (limited to {limit})" if limit else ""
    return f"Queued {queued} /review missions for {owner}/{repo}{limit_note}."
