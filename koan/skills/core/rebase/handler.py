"""Kōan rebase skill -- queue a PR rebase mission."""

from app.github_url_parser import parse_pr_url
from app.github_skill_helpers import (
    extract_github_url,
    format_project_not_found_error,
    format_success_message,
    queue_github_mission,
    resolve_project_for_repo,
)


def handle(ctx):
    """Handle /rebase command -- queue a rebase mission for a PR.

    Usage:
        /rebase https://github.com/owner/repo/pull/123

    Queues a mission that rebases the PR branch onto its target,
    reads all comments for context, and pushes the result.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage: /rebase <github-pr-url>\n"
            "Ex: /rebase https://github.com/sukria/koan/pull/42\n\n"
            "Queues a mission that rebases the PR branch onto its target, "
            "reads comments for context, and force-pushes the result."
        )

    result = extract_github_url(args, url_type="pr")
    if not result:
        return (
            "\u274c No valid GitHub PR URL found.\n"
            "Ex: /rebase https://github.com/owner/repo/pull/123"
        )

    pr_url, _ = result

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        return f"\u274c {e}"

    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    queue_github_mission(ctx, "rebase", pr_url, project_name)

    return f"Rebase queued for {format_success_message('PR', pr_number, owner, repo)}"
