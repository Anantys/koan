"""
Kōan -- Code review runner.

Performs a read-only code review of a GitHub PR and posts findings as a
comment. Unlike /pr (which modifies code and pushes), /review only reads
and comments.

Pipeline:
1. Fetch PR metadata, diff, and existing comments from GitHub
2. Build a review prompt with PR context
3. Run Claude Code CLI (read-only tools) to analyze the code
4. Parse Claude's review output
5. Post the review as a GitHub comment

CLI:
    python3 -m app.review_runner <github-pr-url> --project-path <path>
"""

import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from app.github import run_gh
from app.prompts import load_prompt_or_skill
from app.rebase_pr import fetch_pr_context


def build_review_prompt(
    context: dict,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
) -> str:
    """Build a prompt for Claude to review a PR."""
    prompt_name = "review-architecture" if architecture else "review"
    return load_prompt_or_skill(
        skill_dir, prompt_name,
        TITLE=context["title"],
        AUTHOR=context["author"],
        BRANCH=context["branch"],
        BASE=context["base"],
        BODY=context["body"],
        DIFF=context["diff"],
        REVIEW_COMMENTS=context["review_comments"],
        REVIEWS=context["reviews"],
        ISSUE_COMMENTS=context["issue_comments"],
    )


def _run_claude_review(prompt: str, project_path: str, timeout: int = 600) -> str:
    """Run Claude CLI with read-only tools and return the output text.

    Args:
        prompt: The review prompt.
        project_path: Path to the project for codebase context.
        timeout: Maximum seconds to wait.

    Returns:
        Claude's review text, or empty string on failure.
    """
    from app.claude_step import run_claude
    from app.cli_provider import build_full_command
    from app.config import get_model_config

    models = get_model_config()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=["Read", "Glob", "Grep"],
        model=models["mission"],
        fallback=models["fallback"],
        max_turns=15,
    )

    result = run_claude(cmd, project_path, timeout=timeout)
    if result["success"]:
        return result["output"]
    return ""


def _extract_review_body(raw_output: str) -> str:
    """Extract structured review from Claude's raw output.

    Tries to find markdown-structured review content. Falls back to the
    full output if no structure is detected.
    """
    # Look for the new format: ## PR Review — ...
    match = re.search(r'(## PR Review\b.*)', raw_output, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Legacy format: ## Summary
    match = re.search(r'(## Summary\b.*)', raw_output, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fall back to full output (Claude may format differently)
    return raw_output.strip()


def _post_review_comment(
    owner: str, repo: str, pr_number: str, review_text: str,
) -> bool:
    """Post the review as a comment on the PR.

    Returns True on success.
    """
    # Truncate if too long for GitHub (max ~65536 chars)
    max_len = 60000
    if len(review_text) > max_len:
        review_text = review_text[:max_len] + "\n\n_(Review truncated)_"

    # If body already starts with a ## heading, don't add another
    if review_text.startswith("## "):
        body = f"{review_text}\n\n---\n_Automated review by Kōan_"
    else:
        body = f"## Code Review\n\n{review_text}\n\n---\n_Automated review by Kōan_"

    try:
        run_gh(
            "pr", "comment", pr_number,
            "--repo", f"{owner}/{repo}",
            "--body", body,
        )
        return True
    except Exception as e:
        print(f"[review_runner] failed to post comment: {e}", file=sys.stderr)
        return False


def run_review(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
) -> Tuple[bool, str]:
    """Execute a read-only code review on a PR.

    Args:
        owner: GitHub owner.
        repo: GitHub repo name.
        pr_number: PR number as string.
        project_path: Local path to the project.
        notify_fn: Optional callback for progress notifications.
        skill_dir: Optional path to the review skill directory for prompts.
        architecture: If True, use architecture-focused review prompt.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    full_repo = f"{owner}/{repo}"

    # Step 1: Fetch PR context
    notify_fn(f"Reviewing PR #{pr_number} ({full_repo})...")
    try:
        context = fetch_pr_context(owner, repo, pr_number)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    if not context.get("diff"):
        return False, f"PR #{pr_number} has no diff — nothing to review."

    # Step 2: Build review prompt
    prompt = build_review_prompt(context, skill_dir=skill_dir, architecture=architecture)

    # Step 3: Run Claude review (read-only)
    notify_fn(f"Analyzing code changes on `{context['branch']}`...")
    raw_output = _run_claude_review(prompt, project_path)
    if not raw_output:
        return False, f"Claude review produced no output for PR #{pr_number}."

    # Step 4: Extract structured review
    review_body = _extract_review_body(raw_output)

    # Step 5: Post review comment
    notify_fn(f"Posting review on PR #{pr_number}...")
    posted = _post_review_comment(owner, repo, pr_number, review_body)

    if posted:
        summary = f"Review posted on PR #{pr_number} ({full_repo})."
        return True, summary
    else:
        return False, f"Review generated but failed to post comment on PR #{pr_number}."


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m app.review_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for review_runner.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse

    from app.github_url_parser import parse_pr_url

    parser = argparse.ArgumentParser(
        description="Review a GitHub PR and post findings as a comment."
    )
    parser.add_argument("url", help="GitHub PR URL")
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--architecture", action="store_true",
        help="Use architecture-focused review (SOLID, layering, coupling)",
    )
    cli_args = parser.parse_args(argv)

    try:
        owner, repo, pr_number = parse_pr_url(cli_args.url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "review"

    success, summary = run_review(
        owner, repo, pr_number, cli_args.project_path,
        skill_dir=skill_dir,
        architecture=cli_args.architecture,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
