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

import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from app.github import run_gh
from app.github_url_parser import ISSUE_URL_PATTERN
from app.prompts import load_prompt_or_skill
from app.rebase_pr import fetch_pr_context
from app.review_schema import validate_review

_ISSUE_URL_RE = re.compile(ISSUE_URL_PATTERN)


def fetch_repliable_comments(
    owner: str, repo: str, pr_number: str,
) -> List[dict]:
    """Fetch PR comments with their IDs for reply targeting.

    Returns a list of dicts with keys: id, type, user, body, path (for
    inline comments only). Excludes bot comments and the PR author's own
    inline comments to reduce noise.
    """
    full_repo = f"{owner}/{repo}"
    comments: List[dict] = []

    # Inline review comments (code-level)
    try:
        raw = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | {id: .id, user: .user.login, body: .body, path: .path, line: (.line // .original_line), user_type: .user.type}',
        )
        if raw.strip():
            for line in raw.strip().split("\n"):
                try:
                    item = json.loads(line)
                    if item.get("user_type") == "Bot":
                        continue
                    comments.append({
                        "id": item["id"],
                        "type": "review_comment",
                        "user": item["user"],
                        "body": item["body"],
                        "path": item.get("path", ""),
                        "line": item.get("line"),
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
    except RuntimeError:
        pass

    # Issue-level comments (conversation thread)
    try:
        raw = run_gh(
            "api", f"repos/{full_repo}/issues/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | {id: .id, user: .user.login, body: .body, user_type: .user.type}',
        )
        if raw.strip():
            for line in raw.strip().split("\n"):
                try:
                    item = json.loads(line)
                    if item.get("user_type") == "Bot":
                        continue
                    comments.append({
                        "id": item["id"],
                        "type": "issue_comment",
                        "user": item["user"],
                        "body": item["body"],
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
    except RuntimeError:
        pass

    return comments


def _format_repliable_comments(comments: List[dict]) -> str:
    """Format repliable comments for inclusion in the review prompt."""
    if not comments:
        return "(No comments to reply to.)"

    lines = []
    for c in comments:
        header = f"[id={c['id']}] @{c['user']}"
        if c["type"] == "review_comment" and c.get("path"):
            loc = c["path"]
            if c.get("line"):
                loc += f":{c['line']}"
            header += f" ({loc})"
        header += f" [{c['type']}]"
        # Truncate very long comment bodies in the prompt
        body = c["body"]
        if len(body) > 500:
            body = body[:500] + "..."
        lines.append(f"{header}:\n{body}")
    return "\n\n".join(lines)


def _detect_plan_url(body: str) -> Optional[str]:
    """Extract the first GitHub issue URL from a PR body.

    Returns the full issue URL string if found, or None.
    Only matches issue URLs (not PR URLs) — /issues/ not /pull/.
    """
    match = _ISSUE_URL_RE.search(body)
    if not match:
        return None
    return match.group(0)


def _fetch_plan_body(owner: str, repo: str, issue_number: str) -> str:
    """Fetch the body of a GitHub issue, checking that it has a 'plan' label.

    Returns the plan text (with footer stripped), or empty string if:
    - The issue cannot be fetched
    - The issue does not have a 'plan' label

    Also checks the latest issue comment for an updated plan iteration.
    If the last comment contains '### Implementation Phases', it is treated
    as the authoritative plan (newer than the issue body).
    """
    full_repo = f"{owner}/{repo}"

    try:
        raw = run_gh("api", f"repos/{full_repo}/issues/{issue_number}")
        issue = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError, ValueError):
        return ""

    labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
    if "plan" not in labels:
        return ""

    plan_body = issue.get("body", "") or ""

    # Check latest comment for an updated plan iteration
    try:
        raw_comments = run_gh(
            "api", f"repos/{full_repo}/issues/{issue_number}/comments",
            "--paginate", "--jq",
            r'.[] | {body: .body}',
        )
        if raw_comments.strip():
            for line in reversed(raw_comments.strip().split("\n")):
                try:
                    comment = json.loads(line)
                    comment_body = comment.get("body", "")
                    if "### Implementation Phases" in comment_body:
                        plan_body = comment_body
                        break
                except (json.JSONDecodeError, KeyError):
                    continue
    except RuntimeError:
        pass

    # Strip plan footer added by /plan skill
    footer_marker = "\n---\n*Generated by Kōan /plan"
    if footer_marker in plan_body:
        plan_body = plan_body[:plan_body.index(footer_marker)].rstrip()

    return plan_body


def _truncate_plan(plan_body: str) -> str:
    """Truncate a plan to its key sections (Summary + Implementation Phases).

    Used when the combined plan + diff context is very large (>80K chars).
    Extracts Summary and Implementation Phases sections; falls back to the
    first 5000 chars if those sections cannot be found.
    """
    sections = []
    for section_title in ("## Summary", "### Summary", "### Implementation Phases"):
        idx = plan_body.find(section_title)
        if idx == -1:
            continue
        remaining = plan_body[idx:]
        # Find next ## heading to delimit the section
        end_match = re.search(r'\n##\s', remaining[1:])
        if end_match:
            sections.append(remaining[:end_match.start() + 1])
        else:
            sections.append(remaining)

    if sections:
        return "\n\n".join(sections)
    return plan_body[:5000] + "\n\n...(plan truncated)"


def build_review_prompt(
    context: dict,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
    repliable_comments: Optional[List[dict]] = None,
    plan_body: Optional[str] = None,
) -> str:
    """Build a prompt for Claude to review a PR.

    When plan_body is provided, selects the plan-aware prompt variant
    (review-with-plan) regardless of the architecture flag. When architecture
    is True but no plan is present, uses the architecture prompt.
    """
    if plan_body:
        if architecture:
            print(
                "[review_runner] --architecture ignored: plan alignment takes priority",
                file=sys.stderr,
            )
        prompt_name = "review-with-plan"
    elif architecture:
        prompt_name = "review-architecture"
    else:
        prompt_name = "review"

    repliable_text = _format_repliable_comments(repliable_comments or [])

    kwargs: dict = dict(
        TITLE=context["title"],
        AUTHOR=context["author"],
        BRANCH=context["branch"],
        BASE=context["base"],
        BODY=context["body"],
        DIFF=context["diff"],
        REVIEW_COMMENTS=context["review_comments"],
        REVIEWS=context["reviews"],
        ISSUE_COMMENTS=context["issue_comments"],
        REPLIABLE_COMMENTS=repliable_text,
    )

    if plan_body:
        # Truncate plan if combined context would be too large
        combined_len = len(context.get("diff", "")) + len(plan_body)
        if combined_len > 80_000:
            plan_body = _truncate_plan(plan_body)
        kwargs["PLAN"] = plan_body

    return load_prompt_or_skill(skill_dir, prompt_name, **kwargs)


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

    Tries to find markdown-structured review content. If the output
    looks like JSON, attempts to parse and format it as markdown.
    Falls back to the full output if no structure is detected.
    """
    # Look for the new format: ## PR Review — ...
    match = re.search(r'(## PR Review\b.*)', raw_output, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Legacy format: ## Summary
    match = re.search(r'(## Summary\b.*)', raw_output, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Safety net: if the output contains JSON, try to parse and format it
    # rather than posting raw JSON to GitHub.
    json_text = _extract_json_text(raw_output)
    if json_text is not None:
        try:
            data = json.loads(json_text)
            is_valid, _ = validate_review(data)
            if is_valid:
                return _format_review_as_markdown(data)
        except (json.JSONDecodeError, ValueError):
            pass

    # Fall back to full output (Claude may format differently)
    return raw_output.strip()


def _extract_json_text(text: str) -> Optional[str]:
    """Extract a JSON object string from text that may contain surrounding prose.

    Tries multiple strategies:
    1. Direct parse of the full text (pure JSON)
    2. Strip markdown code fences (```json ... ```)
    3. Extract JSON from code fences anywhere in the text
    4. Find the outermost { ... } in the text
    """
    stripped = text.strip()

    # Strategy 1: pure JSON
    try:
        json.loads(stripped)
        return stripped
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: text wrapped entirely in code fences
    fence_stripped = stripped
    if fence_stripped.startswith("```json"):
        fence_stripped = fence_stripped[len("```json"):]
    elif fence_stripped.startswith("```"):
        fence_stripped = fence_stripped[len("```"):]
    if fence_stripped.endswith("```"):
        fence_stripped = fence_stripped[:-3]
    fence_stripped = fence_stripped.strip()
    if fence_stripped != stripped:
        try:
            json.loads(fence_stripped)
            return fence_stripped
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: code fences embedded in surrounding text
    fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', stripped, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 4: find outermost { ... } with brace matching
    start = stripped.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(stripped)):
            c = stripped[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start:i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except (json.JSONDecodeError, ValueError):
                        break
    return None


def _parse_review_json(raw_output: str) -> Optional[dict]:
    """Attempt to parse and validate JSON review output.

    Handles JSON wrapped in markdown code fences or surrounded by
    preamble/postamble text. Returns the validated review dict, or
    None if parsing/validation fails.
    """
    json_text = _extract_json_text(raw_output)
    if json_text is None:
        return None

    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return None

    is_valid, errors = validate_review(data)
    if not is_valid:
        print(
            f"[review_runner] JSON validation errors: {errors}",
            file=sys.stderr,
        )
        return None
    return data


_SEVERITY_EMOJI = {
    "critical": "🔴",
    "warning": "🟡",
    "suggestion": "🟢",
}

_SEVERITY_HEADING = {
    "critical": "Blocking",
    "warning": "Important",
    "suggestion": "Suggestions",
}


def _format_review_as_markdown(review_data: dict, title: str = "") -> str:
    """Convert validated review JSON into the markdown format for GitHub.

    Produces the standard ## PR Review format with an optional plan alignment
    section (when present), followed by severity sections, checklist, and summary.
    """
    comments = review_data["file_comments"]
    summary_data = review_data["review_summary"]

    lines: list = []

    # Header
    header = f"## PR Review — {title}" if title else "## PR Review"
    lines.append(header)
    lines.append("")
    lines.append(summary_data["summary"])
    lines.append("")
    lines.append("---")
    lines.append("")

    # Plan alignment section (only present when review was done with a plan)
    plan_alignment = review_data.get("plan_alignment")
    if plan_alignment and isinstance(plan_alignment, dict):
        lines.append("### Plan Alignment")
        lines.append("")
        met = plan_alignment.get("requirements_met") or []
        missing = plan_alignment.get("requirements_missing") or []
        out_of_scope = plan_alignment.get("out_of_scope") or []
        if met:
            lines.append(f"✅ **Met** ({len(met)})")
            lines.append("")
            for req in met:
                lines.append(f"- {req}")
            lines.append("")
        if missing:
            lines.append(f"❌ **Missing** ({len(missing)})")
            lines.append("")
            for req in missing:
                lines.append(f"- {req}")
            lines.append("")
        if out_of_scope:
            lines.append(f"📋 **Out of scope** ({len(out_of_scope)})")
            lines.append("")
            for item in out_of_scope:
                lines.append(f"- {item}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # Group comments by severity
    by_severity: dict = {"critical": [], "warning": [], "suggestion": []}
    for c in comments:
        sev = c.get("severity", "suggestion")
        by_severity.setdefault(sev, []).append(c)

    # Emit severity sections (skip empty ones)
    for sev in ("critical", "warning", "suggestion"):
        items = by_severity.get(sev, [])
        if not items:
            continue
        emoji = _SEVERITY_EMOJI[sev]
        heading = _SEVERITY_HEADING[sev]
        lines.append(f"### {emoji} {heading}")
        lines.append("")
        for i, item in enumerate(items, 1):
            loc = f"`{item['file']}`"
            if item.get("line_start") and item["line_start"] > 0:
                loc += f", L{item['line_start']}"
                if item.get("line_end") and item["line_end"] != item["line_start"]:
                    loc += f"-{item['line_end']}"
            lines.append(f"**{i}. {item['title']}** ({loc})")
            lines.append(item["comment"])
            if item.get("code_snippet"):
                lines.append("")
                lines.append("```")
                lines.append(item["code_snippet"])
                lines.append("```")
            lines.append("")

    # Checklist
    checklist = summary_data.get("checklist", [])
    if checklist:
        lines.append("---")
        lines.append("")
        lines.append("### Checklist")
        lines.append("")
        for ci in checklist:
            mark = "x" if ci["passed"] else " "
            ref = f" — {ci['finding_ref']}" if ci.get("finding_ref") else ""
            lines.append(f"- [{mark}] {ci['item']}{ref}")
        lines.append("")

    # Summary (always present)
    lines.append("---")
    lines.append("")
    lines.append("### Summary")
    lines.append("")
    lines.append(summary_data["summary"])

    return "\n".join(lines)


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


def _post_comment_replies(
    owner: str,
    repo: str,
    pr_number: str,
    replies: list,
    repliable_comments: list,
) -> int:
    """Post individual replies to PR comments.

    For review_comment types, uses the pull request review comment reply API.
    For issue_comment types, posts a new issue comment quoting the original.

    Returns the number of replies successfully posted.
    """
    if not replies:
        return 0

    full_repo = f"{owner}/{repo}"
    # Build lookup of comment IDs to their metadata
    comment_map = {c["id"]: c for c in repliable_comments}
    posted = 0

    for reply_item in replies:
        comment_id = reply_item.get("comment_id")
        reply_text = reply_item.get("reply", "")
        if not comment_id or not reply_text:
            continue

        original = comment_map.get(comment_id)
        if not original:
            print(
                f"[review_runner] reply target id={comment_id} not found, skipping",
                file=sys.stderr,
            )
            continue

        try:
            if original["type"] == "review_comment":
                # Reply to an inline review comment via the API
                run_gh(
                    "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
                    "-X", "POST",
                    "-f", f"body={reply_text}",
                    "-F", f"in_reply_to={comment_id}",
                )
            else:
                # For issue comments, post a new comment quoting the original
                user = original.get("user", "someone")
                quote_line = original["body"].split("\n")[0]
                if len(quote_line) > 100:
                    quote_line = quote_line[:100] + "..."
                body = f"> @{user}: {quote_line}\n\n{reply_text}"
                run_gh(
                    "pr", "comment", pr_number,
                    "--repo", full_repo,
                    "--body", body,
                )
            posted += 1
        except Exception as e:
            print(
                f"[review_runner] failed to post reply to comment {comment_id}: {e}",
                file=sys.stderr,
            )

    return posted


def _resolve_plan_body(plan_url: Optional[str], pr_body: str) -> str:
    """Fetch the plan body from an explicit URL or auto-detect from the PR body.

    When plan_url is provided, fetches that issue directly (skipping label check
    only for explicit URLs, to allow non-labelled issues when the user explicitly
    specifies them). When plan_url is None, searches the PR body for issue URLs
    and fetches the first one that has the 'plan' label.

    Returns the plan text, or empty string if no plan is found.
    """
    from app.github_url_parser import parse_issue_url

    if plan_url:
        try:
            p_owner, p_repo, p_number = parse_issue_url(plan_url)
        except ValueError:
            print(
                f"[review_runner] invalid --plan-url '{plan_url}', skipping plan alignment",
                file=sys.stderr,
            )
            return ""
        # For explicit URLs, fetch without label requirement
        try:
            raw = run_gh("api", f"repos/{p_owner}/{p_repo}/issues/{p_number}")
            issue = json.loads(raw)
        except (RuntimeError, json.JSONDecodeError, ValueError):
            return ""
        plan_body = issue.get("body", "") or ""
        # Still check for latest iteration in comments
        try:
            raw_comments = run_gh(
                "api", f"repos/{p_owner}/{p_repo}/issues/{p_number}/comments",
                "--paginate", "--jq", r'.[] | {body: .body}',
            )
            if raw_comments.strip():
                for line in reversed(raw_comments.strip().split("\n")):
                    try:
                        comment = json.loads(line)
                        comment_body = comment.get("body", "")
                        if "### Implementation Phases" in comment_body:
                            plan_body = comment_body
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
        except RuntimeError:
            pass
        footer_marker = "\n---\n*Generated by Kōan /plan"
        if footer_marker in plan_body:
            plan_body = plan_body[:plan_body.index(footer_marker)].rstrip()
        return plan_body

    # Auto-detect from PR body
    detected_url = _detect_plan_url(pr_body)
    if not detected_url:
        return ""

    try:
        p_owner, p_repo, p_number = parse_issue_url(detected_url)
    except ValueError:
        return ""

    return _fetch_plan_body(p_owner, p_repo, p_number)


def run_review(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
    plan_url: Optional[str] = None,
) -> Tuple[bool, str, Optional[dict]]:
    """Execute a read-only code review on a PR.

    Args:
        owner: GitHub owner.
        repo: GitHub repo name.
        pr_number: PR number as string.
        project_path: Local path to the project.
        notify_fn: Optional callback for progress notifications.
        skill_dir: Optional path to the review skill directory for prompts.
        architecture: If True, use architecture-focused review prompt.
        plan_url: Optional explicit GitHub issue URL for the plan to check
            alignment against. When None, auto-detection from PR body is used.

    Returns:
        (success, summary, review_data) tuple. review_data is the validated
        JSON review dict, or None if JSON parsing failed (fallback was used).
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
        return False, f"Failed to fetch PR context: {e}", None

    if not context.get("diff"):
        return False, f"PR #{pr_number} has no diff — nothing to review.", None

    # Step 1b: Fetch repliable comments (with IDs for reply targeting)
    repliable_comments = fetch_repliable_comments(owner, repo, pr_number)

    # Step 1c: Detect and fetch plan body for alignment checking
    plan_body = _resolve_plan_body(plan_url, context.get("body", ""))

    # Step 2: Build review prompt
    prompt = build_review_prompt(
        context, skill_dir=skill_dir, architecture=architecture,
        repliable_comments=repliable_comments, plan_body=plan_body or None,
    )

    # Step 3: Run Claude review (read-only)
    notify_fn(f"Analyzing code changes on `{context['branch']}`...")
    raw_output = _run_claude_review(prompt, project_path)
    if not raw_output:
        return False, f"Claude review produced no output for PR #{pr_number}.", None

    # Step 4: Parse structured JSON review (with retry)
    review_data = _parse_review_json(raw_output)
    if review_data is None:
        # Retry once with explicit JSON instruction
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Your previous response was not valid JSON. "
            "You MUST respond with ONLY a valid JSON object matching the "
            "schema described above. No markdown, no text, just JSON."
        )
        retry_output = _run_claude_review(retry_prompt, project_path)
        if retry_output:
            review_data = _parse_review_json(retry_output)

    # Step 5: Convert to markdown for posting
    if review_data is not None:
        review_body = _format_review_as_markdown(
            review_data, title=context.get("title", ""),
        )
    else:
        # Fallback: use regex extraction for non-JSON responses
        print(
            "[review_runner] JSON parsing failed, falling back to regex extraction",
            file=sys.stderr,
        )
        review_body = _extract_review_body(raw_output)

    # Step 6: Post review comment
    notify_fn(f"Posting review on PR #{pr_number}...")
    posted = _post_review_comment(owner, repo, pr_number, review_body)

    # Step 7: Post replies to user comments
    reply_count = 0
    if review_data and review_data.get("comment_replies") and repliable_comments:
        reply_count = _post_comment_replies(
            owner, repo, pr_number,
            review_data["comment_replies"],
            repliable_comments,
        )
        if reply_count:
            print(
                f"[review_runner] posted {reply_count} reply(ies) to user comments",
                file=sys.stderr,
            )

    if posted:
        summary = f"Review posted on PR #{pr_number} ({full_repo})."
        if reply_count:
            summary += f" Replied to {reply_count} comment(s)."
        return True, summary, review_data
    else:
        return False, f"Review generated but failed to post comment on PR #{pr_number}.", review_data


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
    parser.add_argument(
        "--plan-url",
        help="GitHub issue URL for the plan to check alignment against. "
             "When omitted, auto-detects from the PR body.",
    )
    cli_args = parser.parse_args(argv)

    try:
        owner, repo, pr_number = parse_pr_url(cli_args.url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "review"

    success, summary, _review_data = run_review(
        owner, repo, pr_number, cli_args.project_path,
        skill_dir=skill_dir,
        architecture=cli_args.architecture,
        plan_url=cli_args.plan_url,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
