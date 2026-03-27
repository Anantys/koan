"""Kōan implement skill -- queue an implementation mission for a GitHub issue or PR."""

from app.github_url_parser import parse_github_url
from app.github_skill_helpers import handle_github_skill


def handle(ctx):
    """Handle /implement command -- queue a mission to implement a GitHub issue or PR.

    Usage:
        /implement https://github.com/owner/repo/issues/42
        /implement https://github.com/owner/repo/pull/42
        /implement https://github.com/owner/repo/issues/42 phase 1 only
    """
    return handle_github_skill(
        ctx,
        command="implement",
        url_type="pr-or-issue",
        parse_func=parse_github_url,
        success_prefix="Implementation queued",
    )
