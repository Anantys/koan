"""GitHub AI-powered reply handler.

When an authorized admin user @mentions the bot with a question or request
(not a recognized command), this module generates a contextual reply using
Claude and posts it as a comment.

Flow:
1. Extract freeform text from the @mention comment
2. Fetch issue/PR context (title, body, recent comments)
3. Build prompt with context + question
4. Call Claude CLI to generate a concise reply
5. Post reply as a GitHub comment
"""

import json
import logging
import re
from typing import Optional

from app.cli_provider import run_command
from app.github import api
from app.prompts import load_prompt
from app.utils import truncate_text

log = logging.getLogger(__name__)

# Regex for stripping code blocks before mention extraction
_CODE_BLOCK_RE = re.compile(r'```.*?```|`[^`]+`', re.DOTALL)

# Maximum context chars to prevent prompt overflow
_MAX_CONTEXT_CHARS = 8000
_MAX_COMMENTS = 10


def extract_mention_text(comment_body: str, nickname: str) -> Optional[str]:
    """Extract freeform text after an @mention.

    Unlike parse_mention_command which expects a command word, this extracts
    everything after @nickname as a single text block.

    Args:
        comment_body: The full comment text.
        nickname: The bot's GitHub username (without @).

    Returns:
        The text after @nickname, or None if no mention found.
    """
    if not comment_body or not nickname:
        return None

    # Remove code blocks to avoid matching mentions in code
    clean_body = _CODE_BLOCK_RE.sub('', comment_body)

    # Match @nickname followed by any text (greedy, multiline)
    pattern = rf'@{re.escape(nickname)}\s+(.*?)$'
    match = re.search(pattern, clean_body, re.IGNORECASE | re.DOTALL)
    if not match:
        return None

    text = match.group(1).strip()
    return text if text else None


def fetch_thread_context(
    owner: str,
    repo: str,
    issue_number: str,
    bot_username: str = "",
) -> dict:
    """Fetch issue/PR context for reply generation.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue/PR number.
        bot_username: If provided, comments from this user are excluded
            from the context to prevent self-reply loops.

    Returns:
        Dict with keys: title, body, comments, is_pr, diff_summary.
        Empty/default values on API errors.
    """
    context = {
        "title": "",
        "body": "",
        "comments": [],
        "is_pr": False,
        "diff_summary": "",
    }

    # Fetch issue/PR metadata
    try:
        raw = api(
            f"repos/{owner}/{repo}/issues/{issue_number}",
            jq='{"title": .title, "body": .body, "pull_request": .pull_request}',
        )
        data = json.loads(raw) if raw else {}
        context["title"] = data.get("title", "")
        context["body"] = truncate_text(data.get("body", "") or "", _MAX_CONTEXT_CHARS)
        context["is_pr"] = data.get("pull_request") is not None
    except (RuntimeError, json.JSONDecodeError):
        pass

    # Fetch recent comments
    try:
        raw = api(
            f"repos/{owner}/{repo}/issues/{issue_number}/comments",
            jq=f'[.[-{_MAX_COMMENTS}:] | .[] | {{author: .user.login, body: .body}}]',
        )
        comments = json.loads(raw) if raw else []
        if isinstance(comments, list):
            bot_lower = bot_username.lower() if bot_username else ""
            context["comments"] = [
                {"author": c.get("author", "?"), "body": truncate_text(c.get("body", ""), 500)}
                for c in comments
                if not (bot_lower and c.get("author", "").lower() == bot_lower)
            ]
    except (RuntimeError, json.JSONDecodeError):
        pass

    # For PRs, fetch a diff summary (file list only, not full diff)
    if context["is_pr"]:
        try:
            raw = api(
                f"repos/{owner}/{repo}/pulls/{issue_number}/files",
                jq='[.[] | {filename: .filename, status: .status, additions: .additions, deletions: .deletions}]',
            )
            files = json.loads(raw) if raw else []
            if isinstance(files, list):
                lines = []
                for f in files[:30]:  # Cap at 30 files
                    lines.append(
                        f"  {f.get('status', '?')} {f.get('filename', '?')} "
                        f"(+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                    )
                context["diff_summary"] = "\n".join(lines)
        except (RuntimeError, json.JSONDecodeError):
            pass

    return context


def build_reply_prompt(
    question: str,
    thread_context: dict,
    owner: str,
    repo: str,
    issue_number: str,
    comment_author: str,
) -> str:
    """Build the prompt for Claude to generate a reply.

    Args:
        question: The user's question/request text.
        thread_context: Dict from fetch_thread_context().
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue/PR number.
        comment_author: GitHub username of the person asking.

    Returns:
        The complete prompt string.
    """
    kind = "pull request" if thread_context.get("is_pr") else "issue"
    title = thread_context.get("title", "")
    body = thread_context.get("body", "")
    comments = thread_context.get("comments", [])
    diff_summary = thread_context.get("diff_summary", "")

    # Format comments for context
    comments_text = ""
    if comments:
        comment_lines = []
        for c in comments:
            comment_lines.append(f"@{c['author']}: {c['body']}")
        comments_text = "\n\n".join(comment_lines)

    return load_prompt(
        "github-reply",
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


def generate_reply(
    question: str,
    thread_context: dict,
    owner: str,
    repo: str,
    issue_number: str,
    comment_author: str,
    project_path: str,
) -> Optional[str]:
    """Generate an AI reply using Claude CLI.

    Args:
        question: The user's question.
        thread_context: Context from fetch_thread_context().
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue/PR number.
        comment_author: Who asked the question.
        project_path: Local path to the project (for CLI cwd).

    Returns:
        The reply text, or None on failure.
    """
    prompt = build_reply_prompt(
        question, thread_context, owner, repo, issue_number, comment_author,
    )

    try:
        reply = run_command(
            prompt=prompt,
            project_path=project_path,
            allowed_tools=["Read", "Glob", "Grep"],
            model_key="chat",
            max_turns=3,
            timeout=120,
        )
        return clean_reply(reply) if reply else None
    except Exception as e:
        log.warning("GitHub reply generation failed: %s", e)
        return None


def post_reply(
    owner: str,
    repo: str,
    issue_number: str,
    body: str,
) -> bool:
    """Post a comment reply to a GitHub issue or PR.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue/PR number.
        body: Comment body (markdown).

    Returns:
        True if posted successfully.
    """
    try:
        api(
            f"repos/{owner}/{repo}/issues/{issue_number}/comments",
            method="POST",
            extra_args=["-f", f"body={body}"],
        )
        return True
    except RuntimeError as e:
        log.warning("Failed to post GitHub reply: %s", e)
        return False


def clean_reply(text: str) -> str:
    """Clean Claude CLI output artifacts from the reply."""
    lines = text.strip().splitlines()
    # Remove CLI noise lines
    lines = [l for l in lines if not re.match(r"^Error:.*max turns", l, re.IGNORECASE)]
    return "\n".join(lines).strip()
