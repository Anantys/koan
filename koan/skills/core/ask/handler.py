"""Handler for the /ask skill.

Fetches the question from a GitHub comment URL, gathers PR/issue context,
generates an AI reply using Claude, and posts it back to GitHub.

Mission format (from GitHub @mention):
    - [project:X] /ask https://github.com/owner/repo/issues/42#issuecomment-NNN 📬

The question text is NOT stored in missions.md — it is read back from the
GitHub comment at execution time to avoid injecting uncontrolled characters
into the mission queue.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Matches GitHub comment URL fragments for issue comments and PR review comments
_COMMENT_FRAGMENT_RE = re.compile(r'#issuecomment-(\d+)|#discussion_r(\d+)')

# Matches GitHub PR or issue URL (with optional fragment)
_GITHUB_URL_RE = re.compile(
    r'https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/(?:pull|issues)/(\d+)'
)


def handle(ctx):
    """Handle /ask: fetch question from GitHub comment, generate reply, post it."""
    from app import github_reply
    from app.github_skill_helpers import (
        format_project_not_found_error,
        resolve_project_for_repo,
    )
    from app.prompts import load_skill_prompt

    args = ctx.args.strip() if ctx.args else ""

    if not args:
        return (
            "Usage: /ask <github-comment-url>\n"
            "Ex: /ask https://github.com/owner/repo/issues/42#issuecomment-123456\n\n"
            "Posts an AI-generated answer to the GitHub comment thread."
        )

    # Extract the comment URL (first GitHub URL in args)
    comment_url = _extract_comment_url(args)
    if not comment_url:
        return (
            "❌ No GitHub URL found in arguments.\n"
            "Ex: /ask https://github.com/owner/repo/issues/42#issuecomment-123456"
        )

    # Parse owner/repo/issue_number from URL
    parsed = _parse_github_url(comment_url)
    if not parsed:
        return f"❌ Could not parse GitHub URL: {comment_url}"

    owner, repo, issue_number = parsed

    # Resolve project
    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    # Extract comment ID from URL fragment
    comment_id = _extract_comment_id(comment_url)
    if not comment_id:
        return (
            "❌ URL must include a comment fragment (#issuecomment-NNN or #discussion_rNNN).\n"
            "Ex: /ask https://github.com/owner/repo/issues/42#issuecomment-123456"
        )

    # Fetch thread context (title, body, recent comments, diff)
    thread_context = github_reply.fetch_thread_context(owner, repo, issue_number)

    # Fetch the question text from the specific comment
    question_text, comment_author = _fetch_question_and_author(
        comment_id, owner, repo, comment_url
    )
    if not question_text:
        return "❌ Original comment no longer available or could not fetch question text."

    # Normalise multi-line question text
    question_text = " ".join(question_text.split())

    # Generate reply using the ask-specific prompt
    reply_text = _generate_reply(
        question_text, thread_context, owner, repo, issue_number,
        comment_author or "unknown", project_path, load_skill_prompt,
    )

    if not reply_text:
        return "❌ Failed to generate reply. Check logs for details."

    # Post reply to GitHub
    if not github_reply.post_reply(owner, repo, issue_number, reply_text):
        return "❌ Failed to post reply to GitHub."

    # Build Telegram notification
    issue_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"
    summary = reply_text[:200] + ("..." if len(reply_text) > 200 else "")
    return f"✅ Reply posted to {owner}/{repo}#{issue_number}\n\n{summary}\n\n{issue_url}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_comment_url(args: str) -> Optional[str]:
    """Extract the first GitHub URL from args (including fragment if present)."""
    match = re.search(r'https://github\.com/[^\s]+', args)
    return match.group(0) if match else None


def _parse_github_url(url: str) -> Optional[Tuple[str, str, str]]:
    """Parse owner, repo, issue_number from a GitHub URL.

    Returns (owner, repo, issue_number) or None.
    """
    match = _GITHUB_URL_RE.search(url)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _extract_comment_id(comment_url: str) -> Optional[str]:
    """Extract comment ID from URL fragment.

    Handles:
    - #issuecomment-NNN → returns "NNN"
    - #discussion_rNNN → returns "NNN"
    """
    match = _COMMENT_FRAGMENT_RE.search(comment_url)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _fetch_question_and_author(
    comment_id: Optional[str],
    owner: str,
    repo: str,
    comment_url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch the comment body and author from GitHub.

    Tries issue comment endpoint first, then PR review comment endpoint.
    Returns (body, author) or (None, None) on failure.
    """
    from app.github import api

    if not comment_id:
        return None, None

    # Determine endpoint based on URL fragment format
    is_review_comment = "#discussion_r" in comment_url

    endpoints = []
    if is_review_comment:
        # PR review / line comment
        endpoints = [
            f"repos/{owner}/{repo}/pulls/comments/{comment_id}",
            f"repos/{owner}/{repo}/issues/comments/{comment_id}",
        ]
    else:
        # Regular issue or PR timeline comment
        endpoints = [
            f"repos/{owner}/{repo}/issues/comments/{comment_id}",
            f"repos/{owner}/{repo}/pulls/comments/{comment_id}",
        ]

    for endpoint in endpoints:
        try:
            raw = api(endpoint)
            if not raw:
                continue
            data = json.loads(raw)
            body = data.get("body", "").strip()
            author = data.get("user", {}).get("login", "")
            if body:
                return body, author
        except (RuntimeError, json.JSONDecodeError, KeyError):
            continue

    return None, None


def _generate_reply(
    question: str,
    thread_context: dict,
    owner: str,
    repo: str,
    issue_number: str,
    comment_author: str,
    project_path: str,
    load_skill_prompt,
) -> Optional[str]:
    """Generate an AI reply using the ask-specific prompt, falling back to github-reply."""
    from app import github_reply
    from app.cli_provider import run_command

    kind = "pull request" if thread_context.get("is_pr") else "issue"
    title = thread_context.get("title", "")
    body = thread_context.get("body", "")
    comments = thread_context.get("comments", [])
    diff_summary = thread_context.get("diff_summary", "")

    comments_text = ""
    if comments:
        comments_text = "\n\n".join(
            f"@{c['author']}: {c['body']}" for c in comments
        )

    prompt = load_skill_prompt(
        Path(__file__).parent,
        "ask",
        REPO=f"{owner}/{repo}",
        ISSUE_NUMBER=issue_number,
        KIND=kind,
        TITLE=title,
        BODY=body,
        COMMENTS=comments_text,
        DIFF_SUMMARY=diff_summary,
        QUESTION=question,
        AUTHOR=comment_author,
    )
    try:
        raw = run_command(
            prompt=prompt,
            project_path=project_path,
            allowed_tools=["Read", "Glob", "Grep"],
            model_key="chat",
            max_turns=3,
            timeout=120,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        log.warning("ask: reply generation failed: %s", e)
        return None
    if not raw:
        log.warning("ask: reply generation returned empty output")
        return None
    return github_reply.clean_reply(raw)
