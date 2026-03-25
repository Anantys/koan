"""Kōan review+rebase combo skill -- queue /review then /rebase for a PR."""

from app.github_url_parser import parse_pr_url
from app.github_skill_helpers import (
    extract_github_url,
    format_project_not_found_error,
    format_success_message,
    queue_github_mission,
    resolve_project_for_repo,
)


def handle(ctx):
    """Handle /reviewrebase (alias /rr) -- queue review then rebase for a PR.

    Usage:
        /rr https://github.com/owner/repo/pull/123

    Queues two missions in order:
    1. /review <url> — generates review insights and learnings
    2. /rebase <url> — rebases the PR, informed by the fresh review
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage: /rr <github-pr-url>\n"
            "Ex: /rr https://github.com/sukria/koan/pull/42\n\n"
            "Queues /review then /rebase — review insights feed the rebase."
        )

    result = extract_github_url(args, url_type="pr")
    if not result:
        return (
            "\u274c No valid GitHub PR URL found.\n"
            "Ex: /rr https://github.com/owner/repo/pull/123"
        )

    pr_url, context = result

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        return f"\u274c {e}"

    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    # Queue review first, then rebase — review learnings inform the rebase
    queue_github_mission(ctx, "review", pr_url, project_name, context)
    queue_github_mission(ctx, "rebase", pr_url, project_name)

    return (
        f"Review + rebase combo queued for "
        f"{format_success_message('PR', pr_number, owner, repo)}"
    )
