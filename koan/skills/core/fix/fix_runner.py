"""
Koan -- Fix runner.

Reads a GitHub issue, builds a fix prompt, and invokes Claude to fix it.
Unlike implement_runner (which requires a pre-existing plan), fix_runner
takes a raw issue and lets Claude handle the full pipeline: understand,
plan, test, fix, and submit a PR.

CLI:
    python3 -m skills.core.fix.fix_runner --project-path <path> --issue-url <url>
    python3 -m skills.core.fix.fix_runner --project-path <path> --issue-url <url> --context "backend only"
"""

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from app.github import fetch_issue_with_comments
from app.github_url_parser import parse_issue_url
from app.pr_submit import (
    get_current_branch,
    guess_project_name,
    submit_draft_pr,
)
from app.prompts import load_prompt_or_skill

logger = logging.getLogger(__name__)


def run_fix(
    project_path: str,
    issue_url: str,
    context: Optional[str] = None,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the fix pipeline.

    Fetches the GitHub issue, builds a fix prompt, and invokes Claude to
    understand, plan, test, and fix the issue.

    Args:
        project_path: Local path to the project repository.
        issue_url: GitHub issue URL.
        context: Optional additional context (e.g. "backend only").
        notify_fn: Notification function (defaults to send_telegram).
        skill_dir: Path to the fix skill directory for prompt loading.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    # Parse issue URL
    try:
        owner, repo, issue_number = parse_issue_url(issue_url)
    except ValueError as e:
        return False, str(e)

    context_label = f" ({context})" if context else ""
    notify_fn(
        f"\U0001f527 Fixing issue #{issue_number} "
        f"({owner}/{repo}){context_label}..."
    )

    # Fetch issue content
    try:
        title, body, comments = fetch_issue_with_comments(
            owner, repo, issue_number
        )
    except Exception as e:
        return False, f"Failed to fetch issue: {str(e)[:300]}"

    if not body and not comments:
        return False, f"Issue #{issue_number} has no content."

    # Build full issue body (include relevant comments)
    full_body = _build_issue_body(body, comments)

    # Invoke Claude with the fix prompt
    try:
        output = _execute_fix(
            project_path=project_path,
            issue_url=issue_url,
            issue_title=title,
            issue_body=full_body,
            context=context or "Fix the issue completely.",
            skill_dir=skill_dir,
            issue_number=str(issue_number),
        )
    except Exception as e:
        return False, f"Fix failed: {str(e)[:300]}"

    if not output:
        return False, "Claude returned empty output."

    # Post-fix: submit draft PR
    pr_url = _submit_fix_pr(
        project_path=project_path,
        owner=owner,
        repo=repo,
        issue_number=str(issue_number),
        issue_title=title,
        issue_url=issue_url,
    )

    # Build notification and summary
    branch = get_current_branch(project_path)
    if pr_url:
        notify_fn(
            f"\u2705 Fix complete for issue #{issue_number}"
            f"{context_label}\nDraft PR: {pr_url}"
        )
        summary = (
            f"Fix complete for #{issue_number}{context_label}"
            f"\nDraft PR: {pr_url}"
        )
    elif branch not in ("main", "master"):
        notify_fn(
            f"\u2705 Fix complete for issue #{issue_number}"
            f"{context_label}\nBranch: {branch} (PR creation failed)"
        )
        summary = (
            f"Fix complete for #{issue_number}{context_label}"
            f"\nBranch: {branch}"
        )
    else:
        notify_fn(
            f"\u26a0\ufe0f Fix complete for issue #{issue_number}"
            f"{context_label} \u2014 changes landed on {branch}, no PR created"
        )
        summary = (
            f"Fix complete for #{issue_number}{context_label}"
            f" (on {branch}, no PR)"
        )

    return True, summary


def _build_issue_body(body: str, comments: List[dict]) -> str:
    """Build full issue content including relevant comments.

    Includes the issue body and any comments that add context
    (e.g. reproduction steps, additional details). Skips bot comments
    and very short comments.
    """
    parts = [body.strip()] if body else []

    for comment in comments:
        comment_body = comment.get("body", "").strip()
        author = comment.get("author", "")

        # Skip bot comments and very short comments
        if "[bot]" in author or len(comment_body) < 20:
            continue

        parts.append(f"\n---\n**Comment by {author}**:\n{comment_body}")

    return "\n".join(parts)


def _execute_fix(
    project_path: str,
    issue_url: str,
    issue_title: str,
    issue_body: str,
    context: str,
    skill_dir: Optional[Path] = None,
    issue_number: str = "",
) -> str:
    """Execute the fix via Claude CLI."""
    from app.config import get_branch_prefix
    branch_prefix = get_branch_prefix()

    prompt = _build_prompt(
        issue_url, issue_title, issue_body, context, skill_dir,
        branch_prefix=branch_prefix,
        issue_number=issue_number,
    )

    from app.cli_provider import CLAUDE_TOOLS, run_command_streaming
    from app.config import get_skill_max_turns, get_skill_timeout
    return run_command_streaming(
        prompt, project_path,
        allowed_tools=sorted(CLAUDE_TOOLS),
        max_turns=get_skill_max_turns(), timeout=get_skill_timeout(),
    )


def _build_prompt(
    issue_url: str,
    issue_title: str,
    issue_body: str,
    context: str,
    skill_dir: Optional[Path] = None,
    branch_prefix: str = "koan/",
    issue_number: str = "",
) -> str:
    """Build the fix prompt from the issue content."""
    template_vars = dict(
        ISSUE_URL=issue_url,
        ISSUE_TITLE=issue_title,
        ISSUE_BODY=issue_body,
        CONTEXT=context,
        BRANCH_PREFIX=branch_prefix,
        ISSUE_NUMBER=issue_number,
    )

    return load_prompt_or_skill(skill_dir, "fix", **template_vars)


# ---------------------------------------------------------------------------
# Post-fix: draft PR submission (delegates to app.pr_submit)
# ---------------------------------------------------------------------------

def _submit_fix_pr(
    project_path: str,
    owner: str,
    repo: str,
    issue_number: str,
    issue_title: str,
    issue_url: str,
) -> Optional[str]:
    """Build fix-specific PR title/body and delegate to shared submit."""
    from app.pr_submit import get_commit_subjects
    from app.projects_config import resolve_base_branch

    project_name = guess_project_name(project_path)
    base_branch = resolve_base_branch(project_name, project_path)
    commits = get_commit_subjects(project_path, base_branch=base_branch)
    commits_text = "\n".join(f"- {s}" for s in commits)

    pr_title = f"fix: {issue_title}"[:70]
    pr_body = (
        f"## Summary\n\n"
        f"Fixes {issue_url}\n\n"
        f"## Changes\n\n{commits_text}\n\n"
        f"---\n*Generated by Koan /fix*"
    )

    try:
        return submit_draft_pr(
            project_path=project_path,
            project_name=project_name,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            pr_title=pr_title,
            pr_body=pr_body,
            issue_url=issue_url,
        )
    except Exception as e:
        logger.warning("PR submission failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for fix_runner."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Fix a GitHub issue end-to-end."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--issue-url", required=True,
        help="GitHub issue URL to fix",
    )
    parser.add_argument(
        "--context",
        help="Additional context (e.g. 'backend only')",
        default=None,
    )
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_fix(
        project_path=cli_args.project_path,
        issue_url=cli_args.issue_url,
        context=cli_args.context,
        skill_dir=skill_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
