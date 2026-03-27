"""Kōan PR review skill — review and update GitHub pull requests."""

import re
from pathlib import Path


def handle(ctx):
    """Handle /pr command — review and update a pull request.

    Usage:
        /pr https://github.com/owner/repo/pull/123

    Performs a full pipeline: rebase, address feedback, refactor, review,
    test, push, and comment on the PR.
    """
    args = ctx.args
    send = ctx.send_message

    if not args:
        return (
            "Usage: /pr <github-pr-url>\n"
            "Ex: /pr https://github.com/sukria/koan/pull/29\n\n"
            "Full pipeline: rebase → address feedback → refactor → "
            "review → test → push → comment."
        )

    # Extract URL from args
    url_match = re.search(r'https?://github\.com/[^\s]+/pull/\d+', args)
    if not url_match:
        return (
            "❌ No valid GitHub PR URL found.\n"
            "Ex: /pr https://github.com/owner/repo/pull/123"
        )

    pr_url = url_match.group(0).split("#")[0]

    from app.github_url_parser import parse_pr_url
    from app.utils import resolve_project_path
    from app.pr_review import run_pr_review

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        return str(e)

    # Determine project path
    project_path = resolve_project_path(repo, owner=owner)
    if not project_path:
        from app.utils import get_known_projects
        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return (
            f"❌ Could not find local project matching repo '{repo}'.\n"
            f"Known projects: {known}"
        )

    if send:
        send(f"🔄 Starting PR review pipeline for #{pr_number} ({owner}/{repo})...")

    try:
        success, summary = run_pr_review(
            owner, repo, pr_number, project_path,
            skill_dir=Path(__file__).parent,
        )
        if success:
            if send:
                send(f"✅ PR #{pr_number} updated.\n\n{summary[:400]}")
            return None  # already sent
        else:
            return f"❌ PR #{pr_number} review failed: {summary[:400]}"
    except Exception as e:
        return f"⚠️ PR review error: {str(e)[:300]}"
