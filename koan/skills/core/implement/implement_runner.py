"""
Kōan -- Implement runner.

Reads a GitHub issue containing a plan and invokes Claude to implement it.
The runner extracts the most recent plan iteration from the issue (body or
latest plan comment), ignoring older content, and feeds it to Claude with
an optional user-provided context (e.g. "Phase 1 to 3").

CLI:
    python3 -m skills.core.implement.implement_runner --project-path <path> --issue-url <url>
    python3 -m skills.core.implement.implement_runner --project-path <path> --issue-url <url> --context "Phase 1 to 3"
"""

import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from app.github import fetch_issue_with_comments
from app.github_url_parser import parse_github_url, parse_issue_url
from app.pr_submit import (
    get_current_branch,
    guess_project_name,
    submit_draft_pr,
)
from app.prompts import load_prompt_or_skill

logger = logging.getLogger(__name__)


# Regex pattern matching plan structure markers
_PLAN_MARKER_RE = re.compile(
    r"^#{2,}\s+(?:Implementation Phases|Phase \d+|Summary|Changes in this iteration)",
    re.MULTILINE | re.IGNORECASE,
)


def run_implement(
    project_path: str,
    issue_url: str,
    context: Optional[str] = None,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the implement pipeline.

    Fetches the GitHub issue, extracts the most recent plan, and invokes
    Claude to implement it.

    Args:
        project_path: Local path to the project repository.
        issue_url: GitHub issue URL containing the plan.
        context: Optional additional context (e.g. "Phase 1 to 3").
        notify_fn: Notification function (defaults to send_telegram).
        skill_dir: Path to the implement skill directory for prompt loading.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    # Parse issue or PR URL (GitHub's issues API works for PRs too)
    try:
        owner, repo, _url_type, issue_number = parse_github_url(issue_url)
    except ValueError as e:
        return False, str(e)

    context_label = f" ({context})" if context else ""
    notify_fn(
        f"\U0001f528 Implementing issue #{issue_number} "
        f"({owner}/{repo}){context_label}..."
    )

    # Fetch issue content
    try:
        title, body, comments = fetch_issue_with_comments(
            owner, repo, issue_number
        )
    except Exception as e:
        return False, f"Failed to fetch issue: {str(e)[:300]}"

    # Extract the most recent plan
    plan = _extract_latest_plan(body, comments)
    if not plan:
        return False, (
            f"No plan found in issue #{issue_number}. "
            "The issue should contain implementation phases."
        )

    # Invoke Claude with the plan
    try:
        output = _execute_implementation(
            project_path=project_path,
            issue_url=issue_url,
            issue_title=title,
            plan=plan,
            context=context or "Implement the full plan.",
            skill_dir=skill_dir,
            issue_number=str(issue_number),
        )
    except Exception as e:
        return False, f"Implementation failed: {str(e)[:300]}"

    if not output:
        return False, "Claude returned empty output."

    # Post-implementation: submit draft PR
    pr_url = None
    try:
        pr_url = _submit_implement_pr(
            project_path=project_path,
            owner=owner,
            repo=repo,
            issue_number=str(issue_number),
            issue_title=title,
            issue_url=issue_url,
            skill_dir=skill_dir,
        )
    except Exception as e:
        logger.warning("PR submission failed: %s", e)

    # Build notification and summary
    branch = get_current_branch(project_path)
    if pr_url:
        notify_fn(
            f"\u2705 Implementation complete for issue #{issue_number}"
            f"{context_label}\nDraft PR: {pr_url}"
        )
        summary = (
            f"Implementation complete for #{issue_number}{context_label}"
            f"\nDraft PR: {pr_url}"
        )
    elif branch not in ("main", "master"):
        notify_fn(
            f"\u2705 Implementation complete for issue #{issue_number}"
            f"{context_label}\nBranch: {branch} (PR creation failed)"
        )
        summary = (
            f"Implementation complete for #{issue_number}{context_label}"
            f"\nBranch: {branch}"
        )
    else:
        notify_fn(
            f"\u26a0\ufe0f Implementation complete for issue #{issue_number}"
            f"{context_label} \u2014 changes landed on {branch}, no PR created"
        )
        summary = (
            f"Implementation complete for #{issue_number}{context_label}"
            f" (on {branch}, no PR)"
        )

    return True, summary


def _is_plan_content(text: str) -> bool:
    """Check if text contains plan structure markers.

    Args:
        text: Text to check for plan markers.

    Returns:
        True if text contains markdown headings indicating a plan structure.
    """
    if not text:
        return False
    return bool(_PLAN_MARKER_RE.search(text))


def _extract_latest_plan(body: str, comments: List[dict]) -> str:
    """Extract the most recent plan from issue body and comments.

    Strategy: scan comments from newest to oldest. The first comment
    that contains plan markers is the latest plan iteration. If no
    comment has a plan, fall back to the issue body.

    Args:
        body: Issue body text.
        comments: List of comment dicts with keys: author, date, body.

    Returns:
        The plan text, or empty string if no plan found.
    """
    # Check comments from newest to oldest
    for comment in reversed(comments):
        comment_body = comment.get("body", "")
        if _is_plan_content(comment_body):
            return comment_body

    # Fall back to issue body if it has plan markers
    if _is_plan_content(body):
        return body

    # If no plan markers found, assume the entire body is the plan
    # (allows non-standard plan formats)
    return body.strip()


def _build_prompt(
    issue_url: str,
    issue_title: str,
    plan: str,
    context: str,
    skill_dir: Optional[Path] = None,
    branch_prefix: str = "koan/",
    issue_number: str = "",
) -> str:
    """Build the implementation prompt from the issue and plan."""
    template_vars = dict(
        ISSUE_URL=issue_url,
        ISSUE_TITLE=issue_title,
        PLAN=plan,
        CONTEXT=context,
        BRANCH_PREFIX=branch_prefix,
        ISSUE_NUMBER=issue_number,
    )

    return load_prompt_or_skill(skill_dir, "implement", **template_vars)


def _generate_pr_summary(
    project_path: str,
    issue_title: str,
    issue_url: str,
    commit_subjects: List[str],
    skill_dir: Optional[Path] = None,
) -> str:
    """Generate a PR summary using the lightweight model.

    Falls back to a bullet list of commit subjects if the model call
    fails or times out.
    """
    commits_text = "\n".join(f"- {s}" for s in commit_subjects) or "(no commits)"
    fallback = f"Implements {issue_url}\n\n{commits_text}"

    try:
        prompt = load_prompt_or_skill(
            skill_dir, "pr_summary",
            ISSUE_URL=issue_url,
            ISSUE_TITLE=issue_title,
            COMMIT_SUBJECTS=commits_text,
        )

        from app.cli_provider import run_command
        output = run_command(
            prompt, project_path,
            allowed_tools=[],
            model_key="lightweight",
            max_turns=1,
            timeout=300,
        )
        return output.strip() if output and output.strip() else fallback
    except Exception as e:
        logger.debug("PR summary generation failed: %s", e)
        return fallback


def _execute_implementation(
    project_path: str,
    issue_url: str,
    issue_title: str,
    plan: str,
    context: str,
    skill_dir: Optional[Path] = None,
    issue_number: str = "",
) -> str:
    """Execute the implementation via Claude CLI."""
    from app.config import get_branch_prefix
    branch_prefix = get_branch_prefix()

    prompt = _build_prompt(
        issue_url, issue_title, plan, context, skill_dir,
        branch_prefix=branch_prefix,
        issue_number=issue_number,
    )

    from app.cli_provider import CLAUDE_TOOLS, run_command_streaming
    from app.config import get_skill_timeout
    return run_command_streaming(
        prompt, project_path,
        allowed_tools=sorted(CLAUDE_TOOLS),
        max_turns=50, timeout=get_skill_timeout(),
    )


# ---------------------------------------------------------------------------
# Post-implementation: draft PR submission (delegates to app.pr_submit)
# ---------------------------------------------------------------------------

def _submit_implement_pr(
    project_path: str,
    owner: str,
    repo: str,
    issue_number: str,
    issue_title: str,
    issue_url: str,
    skill_dir: Optional[Path] = None,
) -> Optional[str]:
    """Build implement-specific PR title/body and delegate to shared submit."""
    from app.pr_submit import get_commit_subjects
    from app.projects_config import resolve_base_branch

    project_name = guess_project_name(project_path)
    base_branch = resolve_base_branch(project_name, project_path)
    commits = get_commit_subjects(project_path, base_branch=base_branch)

    summary = _generate_pr_summary(
        project_path, issue_title, issue_url, commits, skill_dir,
    )

    pr_title = f"Implement: {issue_title}"[:70]
    pr_body = (
        f"## Summary\n\n{summary}\n\n"
        f"Closes {issue_url}\n\n"
        f"---\n*Generated by Kōan /implement*"
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
# CLI entry point -- python3 -m app.implement_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for implement_runner."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Implement a plan from a GitHub issue."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--issue-url", required=True,
        help="GitHub issue URL containing the plan",
    )
    parser.add_argument(
        "--context",
        help="Additional context (e.g. 'Phase 1 to 3')",
        default=None,
    )
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_implement(
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
