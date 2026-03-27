"""Koan squash skill -- queue a PR squash mission."""

from app.github_url_parser import parse_pr_url
from app.github_skill_helpers import (
    extract_github_url,
    format_project_not_found_error,
    format_success_message,
    queue_github_mission,
    resolve_project_for_repo,
)


def handle(ctx):
    """Handle /squash command -- queue a squash mission for a PR.

    Usage:
        /squash https://github.com/owner/repo/pull/123

    Squashes all commits on the PR into a single commit with a clean
    message, force-pushes, and updates the PR title and description.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage: /squash <github-pr-url>\n"
            "Ex: /squash https://github.com/sukria/koan/pull/42\n\n"
            "Squashes all commits into one, updates the commit message, "
            "PR title, and description, then force-pushes."
        )

    result = extract_github_url(args, url_type="pr")
    if not result:
        return (
            "\u274c No valid GitHub PR URL found.\n"
            "Ex: /squash https://github.com/owner/repo/pull/123"
        )

    pr_url, _ = result

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        return f"\u274c {e}"

    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    queue_github_mission(ctx, "squash", pr_url, project_name)

    return f"Squash queued for {format_success_message('PR', pr_number, owner, repo)}"
