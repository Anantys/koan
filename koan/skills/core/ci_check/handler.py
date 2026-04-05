"""Koan /ci_check skill -- queue a CI check-and-fix mission for a PR.

Usually auto-injected by ci_queue_runner.drain_one() when CI fails,
but can also be triggered manually via Telegram.
"""

from app.github_url_parser import parse_pr_url
import app.github_skill_helpers as _gh_helpers


def handle(ctx):
    """Handle /ci_check command -- queue a CI fix mission for a PR.

    Usage:
        /ci_check https://github.com/owner/repo/pull/123

    Checks CI status for the PR and attempts to fix failures
    using Claude. Typically auto-triggered after a rebase, but
    can be invoked manually.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage: /ci_check <github-pr-url>\n"
            "Ex: /ci_check https://github.com/owner/repo/pull/42\n\n"
            "Checks CI status and attempts to fix failures using Claude."
        )

    result = _gh_helpers.extract_github_url(args, url_type="pr")
    if not result:
        return (
            "\u274c No valid GitHub PR URL found.\n"
            "Ex: /ci_check https://github.com/owner/repo/pull/123"
        )

    pr_url, _ = result

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        return f"\u274c {e}"

    project_path, project_name = _gh_helpers.resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return _gh_helpers.format_project_not_found_error(repo, owner=owner)

    try:
        owned, head_branch = _gh_helpers.is_own_pr(owner, repo, pr_number)
    except Exception as e:
        return f"\u274c Failed to check PR ownership: {str(e)[:200]}"

    if not owned:
        return (
            f"\u274c Not my PR \u2014 branch `{head_branch}` was not created by "
            f"this instance. I only run CI checks on my own pull requests."
        )

    _gh_helpers.queue_github_mission(ctx, "ci_check", pr_url, project_name)

    return f"\U0001f527 CI check queued for {_gh_helpers.format_success_message('PR', pr_number, owner, repo)}"
