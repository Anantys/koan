"""Runner for /ask skill — bridges GitHub @mention missions to the ask handler.

When a GitHub user @mentions the bot with "ask <question>", the notification
handler creates a mission:
    - [project:X] /ask https://github.com/owner/repo/issues/42#issuecomment-NNN

This runner is auto-discovered by skill_dispatch._discover_runner_module()
and invoked as a subprocess.  It delegates to the ask handler which fetches
the question from GitHub, generates a reply, and posts it back.
"""

import argparse
import sys
from pathlib import Path
from typing import Tuple


def _resolve_bot_username(instance_dir: str) -> str:
    """Read the bot's GitHub nickname from config.yaml."""
    try:
        from app.utils import load_config
        config = load_config()
        github = config.get("github") or {}
        return str(github.get("nickname", "")).strip()
    except Exception as e:
        print(f"[ask_runner] could not resolve bot username: {e}", file=sys.stderr)
    return ""


def run_ask(
    comment_url: str,
    project_path: str,
    project_name: str,
    instance_dir: str,
) -> Tuple[bool, str]:
    """Execute the /ask flow: fetch question, generate reply, post to GitHub.

    Args:
        comment_url: GitHub comment URL with fragment (issuecomment or discussion_r).
        project_path: Local path to the project repository.
        project_name: Name of the project.
        instance_dir: Path to the instance directory.

    Returns:
        (success, summary) tuple.
    """
    from skills.core.ask.handler import (
        _extract_comment_url,
        _parse_github_url,
        _extract_comment_id,
        _fetch_question_and_author,
        _generate_reply,
    )
    from app import github_reply
    from app.prompts import load_skill_prompt

    skill_dir = Path(__file__).parent

    # Validate URL
    url = _extract_comment_url(comment_url)
    if not url:
        return False, f"No GitHub URL found in: {comment_url}"

    parsed = _parse_github_url(url)
    if not parsed:
        return False, f"Could not parse GitHub URL: {url}"

    owner, repo, issue_number = parsed

    comment_id = _extract_comment_id(url)
    if not comment_id:
        return False, f"URL must include a comment fragment: {url}"

    print(f"→ Fetching question from {owner}/{repo}#{issue_number} (comment {comment_id})")

    # Fetch thread context (exclude bot's own comments to avoid self-reply)
    bot_username = _resolve_bot_username(instance_dir)
    thread_context = github_reply.fetch_thread_context(
        owner, repo, issue_number, bot_username=bot_username,
    )

    # Fetch the question text
    question_text, comment_author = _fetch_question_and_author(
        comment_id, owner, repo, url,
    )
    if not question_text:
        return False, "Original comment no longer available or could not fetch question text."

    question_text = " ".join(question_text.split())
    print(f"→ Question from @{comment_author or 'unknown'}: {question_text[:120]}...")

    # Generate reply
    print("→ Generating reply...")
    reply_text = _generate_reply(
        question_text, thread_context, owner, repo, issue_number,
        comment_author or "unknown", project_path, load_skill_prompt,
    )
    if not reply_text:
        return False, "Failed to generate reply."

    # Post reply to GitHub
    print(f"→ Posting reply to {owner}/{repo}#{issue_number}")
    if not github_reply.post_reply(owner, repo, issue_number, reply_text):
        return False, "Failed to post reply to GitHub."

    issue_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"
    summary = (
        f"Reply posted to {owner}/{repo}#{issue_number}\n"
        f"Question: {question_text[:100]}...\n"
        f"Reply: {reply_text[:200]}...\n"
        f"{issue_url}"
    )
    return True, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run /ask skill")
    parser.add_argument("--project-path", required=True, help="Path to the project")
    parser.add_argument("--project-name", required=True, help="Project name")
    parser.add_argument("--instance-dir", required=True, help="Path to instance dir")
    parser.add_argument(
        "--context-file",
        help="File containing the comment URL (written by skill_dispatch)",
    )
    args = parser.parse_args(argv)

    # Read comment URL from context file (written by _build_generic_runner_cmd)
    comment_url = ""
    if args.context_file:
        try:
            comment_url = Path(args.context_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"Error reading context file: {e}", file=sys.stderr)
            sys.exit(1)

    if not comment_url:
        print("No comment URL provided. Use --context-file.", file=sys.stderr)
        sys.exit(1)

    success, summary = run_ask(
        comment_url=comment_url,
        project_path=args.project_path,
        project_name=args.project_name,
        instance_dir=args.instance_dir,
    )

    print(summary)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
