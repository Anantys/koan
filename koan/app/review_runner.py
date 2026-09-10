"""
Kōan -- Code review runner.

Performs a read-only code review of a GitHub PR and posts findings as a
comment. Unlike /pr (which modifies code and pushes), /review only reads
and comments.

Pipeline:
1. Fetch PR metadata, diff, and existing comments from GitHub
2. Build a review prompt with PR context
3. Run the configured provider CLI (read-only tools) to analyze the code
4. Parse the provider's review output
5. Post the review as a GitHub comment

CLI:
    python3 -m app.review_runner <github-pr-url> --project-path <path>
"""

import base64
import contextlib
import html
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote

from app.claude_step import resolve_pr_location
from app.config import get_review_bot_triage_config, get_review_compressor_token_budget, get_review_history_config, get_review_inline_comments_config, get_review_max_diff_chars, get_review_reply_config, get_review_uncompressed_max_diff_chars, get_review_verdict_config, is_review_compressor_enabled
from app.run_log import log
from app.diff_compressor import compress_diff
from app.github import run_gh, sanitize_github_comment, find_bot_comment
from app.github_url_parser import ISSUE_URL_PATTERN
from app.project_koan import get_review_always_check
from app.prompts import load_prompt, load_prompt_or_skill, load_skill_prompt
from app.github_alerts import build_alert
from app.rebase_pr import fetch_pr_context
from app.utils import KOAN_ROOT, truncate_diff_with_skips
from app.review_markers import (
    SUMMARY_TAG,
    COMMIT_IDS_START,
    COMMIT_IDS_END,
    extract_between_markers,
    extract_commit_shas,
    extract_prior_review_body,
    replace_commit_block,
    replace_section,
)
from app.review_schema import validate_review, _VALID_REPLY_ACTIONS

_ISSUE_URL_RE = re.compile(ISSUE_URL_PATTERN)
_QUOTE_RE = re.compile(r'^>\s*@(\S+):\s*(.+)')


def _resolve_bot_username() -> str:
    """Read the bot's GitHub nickname from config.yaml.

    Returns empty string if not configured (filtering is then skipped).
    """
    try:
        from app.utils import load_config
        config = load_config()
        github = config.get("github") or {}
        return str(github.get("nickname", "")).strip()
    except Exception as e:
        print(f"[review_runner] could not resolve bot username: {e}", file=sys.stderr)
        return ""


def _is_bot_user(item: dict, bot_username: str) -> bool:
    """Return True if the comment author is a bot or the configured bot user."""
    if item.get("user_type") == "Bot":
        return True
    if bot_username and item.get("user", "").lower() == bot_username.lower():
        return True
    return False


_BOT_NOISE_PATTERNS = [
    re.compile(r"https?://[\w.-]*deploy[\w.-]*\.(netlify|vercel|herokuapp|surge)", re.I),
    re.compile(r"coverage[:\s]+\d+(\.\d+)?%", re.I),
    re.compile(r"^##\s+(summary|walkthrough|changelog)\b", re.I | re.M),
]

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|.*\|\s*$")


def _is_table_only(body: str) -> bool:
    """Return True if every non-blank line in *body* is a markdown table row."""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    return bool(lines) and all(_TABLE_ROW_RE.match(ln) for ln in lines)


def _pre_filter_bot_noise(comments: List[dict]) -> List[dict]:
    """Drop bot comments that are known meta-noise (deployments, coverage, summaries)."""
    filtered = []
    for c in comments:
        body = (c.get("body") or "").strip()
        if not body:
            continue
        if any(pat.search(body) for pat in _BOT_NOISE_PATTERNS):
            continue
        if _is_table_only(body):
            continue
        filtered.append(c)
    return filtered


def _filter_threads(
    human_comments: List[dict],
    all_comments: list,
    bot_username: str,
    max_thread_depth: int,
) -> List[dict]:
    """Remove comments where the bot already replied with no human follow-up,
    or where the thread has reached max depth.

    For inline review comments, threads are identified by ``in_reply_to_id``
    (all replies point to the root comment). A comment is excluded when:

    1. The bot is the last poster in the thread and no human posted after, OR
    2. The total number of comments in the thread >= ``max_thread_depth``.
    """
    if not bot_username and max_thread_depth <= 0:
        return human_comments

    thread_members: dict = {}
    for c in all_comments:
        root_id = c.get("in_reply_to_id") or c["id"]
        thread_members.setdefault(root_id, []).append(c)

    excluded_threads: set = set()
    for root_id, members in thread_members.items():
        if max_thread_depth > 0 and len(members) >= max_thread_depth:
            excluded_threads.add(root_id)
            continue
        if bot_username and members:
            last = members[-1]
            if last.get("user", "").lower() == bot_username.lower():
                excluded_threads.add(root_id)

    if not excluded_threads:
        return human_comments

    filtered = []
    for c in human_comments:
        root_id = c.get("in_reply_to_id") or c["id"]
        if root_id not in excluded_threads:
            filtered.append(c)
    return filtered


def _exclude_replied_issue_comments(
    human_comments: List[dict],
    bot_comments: list,
) -> List[dict]:
    """Exclude issue comments the bot already replied to.

    Issue comments are flat (no ``in_reply_to_id``), so ``_filter_threads``
    cannot detect self-replies.  Bot replies to issue comments use the
    format ``> @user: first_line...``.  Match that quote pattern against
    human comments to detect prior replies.
    """
    replied_quotes: list = []
    for bc in bot_comments:
        body = bc.get("body", "")
        first_line = body.split("\n")[0]
        m = _QUOTE_RE.match(first_line)
        if not m:
            continue
        user = m.group(1).lower()
        text = m.group(2).strip()
        truncated = text.endswith("...")
        if truncated:
            text = text[:-3].rstrip()
        if text:
            replied_quotes.append((user, text.lower(), truncated))

    if not replied_quotes:
        return human_comments

    filtered = []
    for hc in human_comments:
        user = hc.get("user", "").lower()
        first_line = hc.get("body", "").split("\n")[0].strip().lower()
        already_replied = any(
            user == ru and (
                first_line[:len(rp)] == rp if was_truncated
                else first_line == rp
            )
            for ru, rp, was_truncated in replied_quotes
        )
        if not already_replied:
            filtered.append(hc)
    return filtered


def _fetch_raw_inline_items(full_repo: str, pr_number: str) -> List[dict]:
    """Fetch raw inline review comment items from the GitHub API."""
    all_items: list = []
    try:
        raw = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | {id: .id, user: .user.login, body: .body, path: .path, line: (.line // .original_line), user_type: .user.type, in_reply_to_id: .in_reply_to_id}',
        )
        if raw.strip():
            for line in raw.strip().split("\n"):
                try:
                    all_items.append(json.loads(line))
                except (json.JSONDecodeError, KeyError):
                    continue
    except RuntimeError:
        pass
    return all_items


def _partition_inline_comments(
    items: List[dict],
    bot_username: str,
    extra_bot_usernames: Optional[List[str]] = None,
) -> tuple:
    """Partition parsed API items into (human, bot) comment lists.

    Self-bot comments (matching ``bot_username``) are excluded from BOTH
    lists to prevent reply loops.  Comments from users in
    ``extra_bot_usernames`` are treated as bot-authored even when
    ``user_type`` is ``"User"``.
    """
    extra_lower = {u.lower() for u in (extra_bot_usernames or [])}
    human: List[dict] = []
    bot: List[dict] = []
    for item in items:
        entry = {
            "id": item["id"],
            "type": "review_comment",
            "user": item["user"],
            "body": item["body"],
            "path": item.get("path", ""),
            "line": item.get("line"),
            "in_reply_to_id": item.get("in_reply_to_id"),
        }
        user_lower = item.get("user", "").lower()
        if bot_username and user_lower == bot_username.lower():
            continue
        if item.get("user_type") == "Bot" or user_lower in extra_lower:
            bot.append(entry)
        else:
            human.append(entry)
    return human, bot


def _fetch_inline_review_comments(
    full_repo: str, pr_number: str, bot_username: str = "",
    max_thread_depth: int = 0,
) -> List[dict]:
    """Fetch inline review comments (code-level) for a PR.

    Returns only human-authored comments.  Bot comments are partitioned
    out.  When ``bot_username`` is set, threads where the bot was the last
    poster (with no human follow-up) are excluded.  When
    ``max_thread_depth`` > 0, threads with that many or more total comments
    are excluded entirely.
    """
    all_items = _fetch_raw_inline_items(full_repo, pr_number)
    human_comments, _ = _partition_inline_comments(all_items, bot_username)

    if bot_username or max_thread_depth > 0:
        return _filter_threads(human_comments, all_items, bot_username, max_thread_depth)
    return human_comments


def _fetch_bot_inline_comments(
    full_repo: str,
    pr_number: str,
    bot_username: str = "",
    extra_bot_usernames: Optional[List[str]] = None,
) -> List[dict]:
    """Fetch inline review comments authored by bots, with noise pre-filtered.

    Returns structured dicts for bot comments that pass the noise filter.
    Self-bot comments are excluded, and bot comments whose thread already
    contains a reply from ``bot_username`` are dropped to prevent duplicate
    replies on reruns.
    """
    all_items = _fetch_raw_inline_items(full_repo, pr_number)
    _, bot_comments = _partition_inline_comments(
        all_items, bot_username, extra_bot_usernames,
    )

    if bot_username and bot_comments:
        bot_lower = bot_username.lower()
        replied_roots: set = set()
        for item in all_items:
            if item.get("user", "").lower() == bot_lower:
                root_id = item.get("in_reply_to_id") or item["id"]
                replied_roots.add(root_id)
        bot_comments = [
            c for c in bot_comments
            if (c.get("in_reply_to_id") or c["id"]) not in replied_roots
        ]

    return _pre_filter_bot_noise(bot_comments)


def _fetch_issue_comments(
    full_repo: str, pr_number: str, bot_username: str = "",
) -> List[dict]:
    """Fetch issue-level comments (conversation thread) for a PR.

    Collects bot comments separately and uses them to detect prior replies.
    Human comments that the bot already replied to (matching quote pattern)
    are excluded from the returned list.
    """
    human: List[dict] = []
    bot_replies: list = []
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
                    if _is_bot_user(item, bot_username):
                        bot_replies.append(item)
                        continue
                    human.append({
                        "id": item["id"],
                        "type": "issue_comment",
                        "user": item["user"],
                        "body": item["body"],
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
    except RuntimeError:
        pass

    if bot_replies and human:
        return _exclude_replied_issue_comments(human, bot_replies)
    return human


def fetch_repliable_comments(
    owner: str, repo: str, pr_number: str,
    parallel: bool = True,
    bot_username: str = "",
) -> List[dict]:
    """Fetch PR comments with their IDs for reply targeting.

    Returns a list of dicts with keys: id, type, user, body, path (for
    inline comments only). Excludes bot comments, threads where the bot
    was the last poster (self-reply guard), and threads that have reached
    the configured ``max_thread_depth``.

    Args:
        owner: GitHub owner/org.
        repo: Repository name.
        pr_number: PR number as string.
        parallel: When True (default), fetch inline and issue comments
            concurrently using two threads. Set to False to force sequential
            fetching (useful in tests or single-threaded contexts).
        bot_username: If provided, comments from this user are excluded
            to prevent self-reply loops.
    """
    reply_cfg = get_review_reply_config()
    max_depth = reply_cfg["max_thread_depth"]

    full_repo = f"{owner}/{repo}"
    comments: List[dict] = []

    if parallel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_inline = pool.submit(
                _fetch_inline_review_comments, full_repo, pr_number,
                bot_username, max_depth,
            )
            f_issue = pool.submit(_fetch_issue_comments, full_repo, pr_number, bot_username)
            comments.extend(f_inline.result())
            comments.extend(f_issue.result())
    else:
        comments.extend(
            _fetch_inline_review_comments(full_repo, pr_number, bot_username, max_depth),
        )
        comments.extend(_fetch_issue_comments(full_repo, pr_number, bot_username))

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

    from app.pr_footer import strip_legacy_footers
    plan_body = strip_legacy_footers(plan_body)

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


def _build_review_session_memory(project_name: str, task_text: str) -> str:
    """Return an opt-in block of recent typed project memory for reviews.

    Pulls recent non-learning session entries (decisions, observations, etc.)
    from the persistent FTS5 memory index, ranked against the PR content.
    Learnings are excluded here because they are already injected via the
    project-memory block (``build_memory_block_for_skill``). Returns "" when
    the feature is disabled, the project is unscoped, or there is nothing to
    show — so callers can append the result unconditionally.
    """
    if not project_name:
        return ""
    from app.config import get_review_memory_config

    cfg = get_review_memory_config()
    if not cfg["enabled"] or cfg["max_entries"] <= 0:
        return ""

    from app.memory_manager import read_memory_window

    instance = str(KOAN_ROOT / "instance")
    try:
        entries = read_memory_window(
            instance,
            project_name,
            max_entries=cfg["max_entries"],
            query_text=task_text,
            current_skill="review",
        )
    except Exception as exc:  # memory retrieval must never break a review
        print(
            f"[review_runner] session memory lookup failed: {exc}",
            file=sys.stderr,
        )
        return ""

    lines = [
        f"[{e.get('ts', '')}] {e.get('type', '')}: {e.get('content', '')}"
        for e in entries
        if e.get("type") != "learning"
    ]
    if not lines:
        return ""

    body = "\n".join(lines)
    return (
        "\n\n<session-memory>\n# Recent project memory\n\n"
        f"{body}\n</session-memory>\n"
    )


def _strip_bot_summary_from_thread(issue_comments: str) -> str:
    """Remove the bot's own ``koan-summary`` comment from the flattened thread.

    The conversation thread is a string of ``@login: body`` blocks joined by
    newlines (a single body may span many lines). When the prior structured
    review is surfaced in its own ``{PRIOR_REVIEW}`` slot, we drop it from the
    thread so the (recency-truncated) thread budget serves human discussion
    instead of echoing the bot's own review.

    Best-effort: a body line that itself begins with ``@`` could, worst case,
    cause one adjacent comment to be trimmed — preferable to echoing the whole
    review. Returns the input unchanged when no summary marker is present.
    """
    if not issue_comments or SUMMARY_TAG not in issue_comments:
        return issue_comments
    tag_idx = issue_comments.find(SUMMARY_TAG)
    start = issue_comments.rfind("\n@", 0, tag_idx)
    block_start = 0 if start == -1 else start + 1
    nxt = issue_comments.find("\n@", tag_idx)
    block_end = len(issue_comments) if nxt == -1 else nxt + 1
    cleaned = issue_comments[:block_start] + issue_comments[block_end:]
    return cleaned.strip("\n")


def _format_prior_review(prior_review: Optional[str], max_chars: int) -> str:
    """Render the dedicated prior-review block, head-preserving and budgeted."""
    text = (prior_review or "").strip()
    if not text:
        return "(No prior automated review.)"
    if max_chars and len(text) > max_chars:
        from app.utils import truncate_text
        text = truncate_text(text, max_chars)
    return text


def _resolve_issue_context(
    context: dict,
    project_name: str = "",
    project_path: str = "",
) -> str:
    """Fetch tracker issue context for the review prompt.

    Best-effort: returns "" when enrichment is disabled, no project is known,
    or any fetch fails. Never raises — a tracker problem must not abort a
    review.
    """
    if not project_name:
        return ""
    try:
        from app.config import get_review_issue_context_config

        if not get_review_issue_context_config()["enabled"]:
            return ""
        from app.issue_tracker.enrichment import fetch_issue_context

        return fetch_issue_context(
            context.get("body") or "",
            project_name=project_name,
            project_path=project_path or "",
        )
    except Exception as e:
        log("review", f"issue context enrichment skipped: {e}")
        return ""


def _build_coverage_note(
    fetch_skipped: list,
    compressor_skipped: list,
    triaged_files: Optional[list],
) -> str:
    """Build ONE unified coverage note used for both the review prompt's
    {SKIPPED_FILES} slot and the note prepended to the posted GitHub review.

    - fetch_skipped:      files cut at diff-fetch time (oversized-diff backstop)
    - compressor_skipped: files packed out by the token-budget compressor
    - triaged_files:      trivial files intentionally skipped (informational)

    Returning a single value (no copy-then-append) guarantees the prompt and
    the posted body never diverge.
    """
    # dict.fromkeys preserves order and dedupes (a file cut at fetch never
    # reaches the compressor, but dedupe defensively).
    omitted = list(dict.fromkeys([*(fetch_skipped or []), *(compressor_skipped or [])]))
    parts: list[str] = []
    if omitted:
        listing = ", ".join(f"`{f}`" for f in omitted)
        parts.append(
            f"> ⚠️ **Partial review** — {len(omitted)} file(s) omitted "
            f"due to diff size and NOT reviewed: {listing}"
        )
    if triaged_files:
        triaged_list = ", ".join(f"`{t.path}` ({t.reason})" for t in triaged_files)
        parts.append(
            f"> ℹ️ Triaged {len(triaged_files)} trivial file(s) "
            f"(not reviewed): {triaged_list}"
        )
    return ("\n>\n".join(parts) + "\n\n") if parts else ""


def _build_repo_conventions_block(
    project_path: Optional[str], project_name: str = "",
) -> str:
    """Assemble the framed ``{REPO_CONVENTIONS}`` block, or "" when disabled/empty.

    Native, always-available repo steering: reads the reviewed checkout's own
    convention docs (AGENTS.md/CLAUDE.md/CONTRIBUTING.md + an OKF ``docs/``
    bundle) and frames them as authoritative-but-fenced repo context, so the
    reviewer applies repo conventions instead of raising convention-based false
    positives. Fully additive; degrades to "" when the feature is off or no docs
    are found. Never raises.
    """
    if not project_path:
        return ""
    try:
        from app.config import get_review_convention_docs_config
        cfg = get_review_convention_docs_config(project_name or "")
        if not cfg["enabled"]:
            return ""
        from app.project_koan import read_repo_convention_docs
        raw = read_repo_convention_docs(
            project_path,
            well_known=tuple(cfg["well_known"]),
            okf_docs_dir=cfg["okf_docs_dir"],
            include_topic_indexes=cfg["include_topic_indexes"],
            auto_detect_okf=cfg["auto_detect_okf"],
            max_block_chars=cfg["max_chars"],
        )
        if not raw:
            return ""
        from app.prompt_guard import fence_external_data
        fenced = fence_external_data(raw, "repo convention docs")
        block = load_prompt("review-repo-conventions", REPO_CONVENTION_DOCS=fenced)
        return f"\n\n{block}\n"
    except Exception as e:
        log("review", f"repo conventions injection skipped: {e}")
        return ""


def _build_discovery_block(project_name: str = "") -> str:
    """Assemble the ``{COMPREHENSIVE_DISCOVERY}`` block, or "" when disabled.

    Injects the opt-in multi-perspective discovery guidance (spec 010, US3) into
    the review prompt when ``review_discovery.enabled`` is set. OFF by default, so
    the block is "" and the prompt is byte-identical to the single-pass review
    (SC-008). Never raises — degrades to "".
    """
    try:
        from app.config import get_review_discovery_config
        if not get_review_discovery_config(project_name or "")["enabled"]:
            return ""
        return "\n\n" + load_prompt("review-comprehensive-discovery") + "\n"
    except Exception as e:
        log("review", f"comprehensive-discovery injection skipped: {e}")
        return ""


def _build_dispositions_block(project_name: str = "") -> str:
    """Assemble the ``{DISPOSITIONS}`` block, or "" when disabled.

    Injects the human-disposition guidance (honor dismiss/defer decisions humans
    leave as PR comments; spec 010, US7) into the review prompt. Config-gated by
    ``review_dispositions.enabled`` (default on) so a security-conscious operator
    can turn off the "any commenter can dispose of a finding" posture. Never
    raises — degrades to "" so the review proceeds normally.
    """
    try:
        from app.config import get_review_dispositions_config
        if not get_review_dispositions_config(project_name or "")["enabled"]:
            return ""
        return "\n\n" + load_prompt("review-dispositions") + "\n"
    except Exception as e:
        log("review", f"dispositions guidance injection skipped: {e}")
        return ""


def _pinned_files_in_diff(raw_diff: str, patterns: list) -> list:
    """Repo-relative paths in *raw_diff* matching a ``review.always_check`` glob.

    Used only to log which files the repo pinned; returns [] when *patterns* is
    empty so an absent config produces no log line.
    """
    if not patterns:
        return []
    from app.diff_compressor import path_matches_any
    names = re.findall(r'^diff --git a/\S+ b/(\S+)', raw_diff, flags=re.MULTILINE)
    return [n for n in names if path_matches_any(n, patterns)]


def build_review_prompt(
    context: dict,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
    comments: bool = False,
    repliable_comments: Optional[List[dict]] = None,
    plan_body: Optional[str] = None,
    project_path: Optional[str] = None,
    triaged_files: Optional[list] = None,
    project_name: str = "",
    prior_review: Optional[str] = None,
    issue_context: Optional[str] = None,
) -> Tuple[str, str]:
    """Build a prompt for Claude to review a PR.

    When plan_body is provided, selects the plan-aware prompt variant
    (review-with-plan) regardless of the architecture flag. When architecture
    is True but no plan is present, uses the architecture prompt.

    When ``project_path`` is set, project memory (filtered learnings +
    human-curated context + priorities) is injected via
    :func:`app.skill_memory.build_memory_block_for_skill`.
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
    elif comments:
        prompt_name = "review-comments"
    else:
        prompt_name = "review"

    repliable_text = _format_repliable_comments(repliable_comments or [])

    # Dedicated prior-review slot: surface the bot's last structured review as
    # authoritative context (head-preserving budget) and drop it from the
    # recency-truncated conversation thread so it no longer competes with — or
    # is evicted by — human discussion.
    from app.config import get_review_context_config
    ctx_cfg = get_review_context_config()
    if not ctx_cfg["include_bot_feedback"]:
        prior_review = None
    issue_comments_text = context.get("issue_comments", "")
    if prior_review:
        issue_comments_text = _strip_bot_summary_from_thread(issue_comments_text)
    prior_review_block = _format_prior_review(
        prior_review, ctx_cfg["prior_review_max_chars"],
    )

    project_memory = ""
    if project_path:
        from app.skill_memory import build_memory_block_for_skill
        # Score learnings against the PR's actual content (title + body +
        # diff slice), not just title + branch. Branch names are mostly
        # autogenerated noise (e.g. ``koan/fix-issue-123``) that produce
        # near-zero Jaccard signal; the diff is where filenames, modules,
        # and recurring patterns live — exactly what the learnings file
        # tends to index against. Cap the diff slice at ~2K chars so the
        # tokenizer doesn't churn on giant PRs.
        diff = context.get("diff", "") or ""
        task_text = "\n".join(filter(None, (
            context.get("title", ""),
            context.get("body", ""),
            diff[:2000],
        )))
        project_memory = build_memory_block_for_skill(
            project_path, task_text, project_name=project_name,
        )
        project_memory += _build_review_session_memory(project_name, task_text)

    raw_diff = context["diff"]
    # Repo-owner pin set (.koan/config.yaml review.always_check): files matching
    # a glob are kept ahead of budgeted files so diff-size reduction never
    # silently drops them. Empty/absent config ⇒ [] ⇒ byte-identical no-op.
    pinned_patterns = get_review_always_check(project_path) if project_path else []
    pinned_now = _pinned_files_in_diff(raw_diff, pinned_patterns)
    if pinned_now:
        log(
            "review",
            f"Pinned {len(pinned_now)} file(s) via .koan review.always_check: "
            + ", ".join(pinned_now),
        )
    # Files skipped to fit the token budget — either packed out by the
    # compressor (on-path) or cut by the token-safe backstop (off-path).
    budget_skipped: list = []
    if is_review_compressor_enabled():
        compressed = compress_diff(
            raw_diff, get_review_compressor_token_budget(), pinned_patterns
        )
        raw_diff = compressed.diff_text
        budget_skipped = compressed.skipped_files
        if budget_skipped:
            log(
                "review",
                f"Diff compressed — {len(budget_skipped)} file(s) skipped: "
                + ", ".join(budget_skipped),
            )
    else:
        # Compressor off: no packer re-shrinks the fetch-time diff, so apply a
        # token-safe backstop here or the raw diff (up to the generous fetch
        # cap) could overflow the model context and hard-fail the review. Skips
        # flow into the same coverage note as compressor skips.
        raw_diff, budget_skipped = truncate_diff_with_skips(
            raw_diff, get_review_uncompressed_max_diff_chars(), pinned_patterns
        )
        if budget_skipped:
            log(
                "review",
                f"Compressor off — diff truncated, {len(budget_skipped)} "
                f"file(s) skipped: " + ", ".join(budget_skipped),
            )

    # ONE unified coverage note — the same value feeds the {SKIPPED_FILES}
    # prompt slot AND is returned for prepending to the posted review, so the
    # prompt and the posted body can never diverge.
    coverage_note = _build_coverage_note(
        fetch_skipped=context.get("diff_skipped_files", []),
        compressor_skipped=budget_skipped,
        triaged_files=triaged_files,
    )

    if issue_context is None:
        issue_context = _resolve_issue_context(context, project_name, project_path)

    # Native repo-convention context: the reviewed repo's own AGENTS.md/CLAUDE.md
    # + OKF docs/ bundle, framed as authoritative-but-fenced conventions so the
    # reviewer stops raising convention-based false positives. Empty when absent.
    repo_conventions = _build_repo_conventions_block(project_path, project_name)

    kwargs: dict = dict(
        TITLE=context["title"],
        AUTHOR=context["author"],
        BRANCH=context["branch"],
        BASE=context["base"],
        BODY=context["body"],
        DIFF=raw_diff,
        REVIEW_COMMENTS=context["review_comments"],
        REVIEWS=context["reviews"],
        ISSUE_COMMENTS=issue_comments_text,
        REPLIABLE_COMMENTS=repliable_text,
        PRIOR_REVIEW=prior_review_block,
        PROJECT_MEMORY=project_memory,
        SKIPPED_FILES=coverage_note,   # same value returned below
        ISSUE_CONTEXT=issue_context or "",
        REPO_CONVENTIONS=repo_conventions,
        DISPOSITIONS=_build_dispositions_block(project_name),
        COMPREHENSIVE_DISCOVERY=_build_discovery_block(project_name),
    )

    if plan_body:
        # Truncate plan if combined context would be too large
        combined_len = len(context.get("diff", "")) + len(plan_body)
        if combined_len > 80_000:
            plan_body = _truncate_plan(plan_body)
        kwargs["PLAN"] = plan_body

    prompt = load_prompt_or_skill(
        skill_dir, prompt_name, project_path=project_path, **kwargs
    )
    return prompt, coverage_note


def _review_attribution(project_name: str = "") -> Tuple[str, str]:
    """Return ``(provider_name, model)`` the review actually runs on.

    Resolves the ``review_mode`` role against the same provider the review CLI
    uses (``resolve_role_provider`` — including any launch-fallback swap) and
    reads that provider's model section (``review_mode`` then ``mission``).
    Single source of truth for both the review invocation and the footer
    attribution, so the displayed provider/model can never drift from the
    binary/model actually used.

    ``provider_name`` is the binary basename when the review_mode role pins a
    custom CLI path (e.g. ``claude-deep`` from ``cli.review_mode:
    claude:/root/.local/bin/claude-deep``), else the provider flavor name — so
    the footer advertises the binary that actually ran, not just its flavor.
    """
    from app.cli_provider import resolve_role_provider
    from app.config import get_model_config
    from app.provider import provider_cli_display

    provider = resolve_role_provider("review_mode", project_name)
    models = get_model_config(
        project_name,
        role_providers={
            "review_mode": provider.name,
            "mission": provider.name,
        },
    )
    return (
        provider_cli_display(provider),
        (models.get("review_mode") or models.get("mission", "")),
    )


def _review_stall_timeout() -> int:
    """Seconds of provider silence that end a single review pass.

    Derived from ``first_output_timeout`` (run.py's outer skill-runner
    watchdog) rather than configured separately, because the only value the
    inner bound can usefully take is one strictly below the outer one — at or
    above it the outer watchdog fires first and SIGKILLs the whole runner, and
    the inner bound is decorative. Half the outer budget leaves room for the
    pass to fail, be reported, and let the review continue.

    Returns 0 (no inner bound) in the two cases where one cannot help:

    - the operator disabled the outer watchdog (``first_output_timeout: 0``),
      i.e. asked for no stall killing at all;
    - the outer budget is already tighter than the 60s floor below which a
      brief legitimate pause would be misread as a stall. The outer watchdog
      governs there, so an inner bound would only ever be decorative.

    The postcondition is therefore exact: the result is either 0, or a value
    strictly below ``first_output_timeout``.
    """
    from app.config import get_first_output_timeout

    outer = get_first_output_timeout()
    if outer <= 0:
        return 0
    inner = outer // 2
    return inner if inner >= 60 else 0


def _run_claude_review(
    prompt: str,
    project_path: str,
    timeout: int = 600,
    model: Optional[str] = None,
    project_name: str = "",
) -> Tuple[str, str]:
    """Run provider CLI with read-only tools and return the output text.

    The ``review_mode`` role is in :data:`app.provider.READ_ONLY_ROLES`, so the
    provider layer additionally *denies* the side-effecting tools rather than
    merely leaving them off ``allowed_tools`` — an allowlist alone does not
    withhold anything (see :data:`app.provider.base.SIDE_EFFECT_TOOLS`). The
    boundary fails closed: if the resolved ``review_mode`` provider can enforce
    neither a per-tool denial nor a read-only sandbox, the provider layer raises
    :class:`~app.provider.base.ReadOnlyUnenforceable` (a ``RuntimeError``) and
    the review is reported as failed rather than run with write access.

    Args:
        prompt: The review prompt.
        project_path: Path to the project for codebase context.
        timeout: Maximum seconds to wait (default 600s — large PRs need
                 more time than the old 300s default).
        model: Optional model override. When None, the review_mode model is
               resolved against the review_mode PROVIDER's section (so it
               matches the binary the review runs on), falling back to that
               provider's mission model.
        project_name: Project name for per-project ``cli.review_mode`` overrides.

    Returns:
        (output, error) tuple. output is the provider's review text (empty on
        failure), error is the failure reason (empty on success).
    """
    from app.cli_provider import run_command_streaming
    from app.config import get_skill_max_turns

    idle_timeout = _review_stall_timeout()
    if model is None:
        # Resolve the model against the review_mode provider (not the global
        # one) so it matches the binary the review runs on — see
        # _review_attribution, shared with the footer attribution below.
        _, model = _review_attribution(project_name)

    try:
        # Resolve the review-path CLI via the "review_mode" role (the cli:
        # config section). This pins a review-specific provider/binary — e.g.
        # cli.review_mode: claude:/path/to/review-claude — for every review
        # call (main pass, reflect, error-hunter, bot triage) without affecting
        # other missions or the write-capable fix step.
        output = run_command_streaming(
            prompt=prompt,
            project_path=project_path,
            # Bash is deliberately absent: it is granted by READ_ONLY_TOOLS and
            # adjudicated per-command by the review_bash_guard hook. Listing it
            # here would pre-approve it and defeat the gate.
            allowed_tools=["Read", "Glob", "Grep"],
            model_key="review_mode",
            model=model,
            max_turns=get_skill_max_turns(),
            timeout=timeout,
            project_name=project_name,
            # The reviewed tree is untrusted input. Without this the session
            # loads <project_path>/.claude/settings.json, which can define
            # SessionStart/PreToolUse hooks -- code execution on the review host
            # triggered by opening a pull request. Repo conventions still reach
            # the model via the fenced {REPO_CONVENTIONS} prompt block.
            project_context=False,
            # Bound inactivity, not wall-clock: a healthy pass streams progress
            # for many minutes, but a provider that prints its session banner
            # and then goes silent must fail *this pass* rather than let the
            # outer skill-runner watchdog SIGKILL the whole review.
            idle_timeout=idle_timeout,
        )
        return output, ""
    except RuntimeError as e:
        error = str(e) or "unknown error"
        print(
            f"[review_runner] Provider review failed: {error}",
            file=sys.stderr,
        )
        return "", error


def _write_review_findings_sidecar(
    instance_dir: str,
    owner: str,
    repo: str,
    pr_number: str,
    file_comments: list,
    *,
    base_ref: str,
    head_sha: str,
    base_sha: str = "",
    request_signature: Optional[dict] = None,
    project_name: str = "",
    review_summary: Optional[dict] = None,
    review_comment: Optional[dict] = None,
) -> None:
    """Write structured review findings + summary to a sidecar JSON.

    Consumed by pr_review_learning (post-merge outcome tracking, reads
    file_comments) and by the REST API result resolver (reads
    file_comments, review_summary, and review_comment). review_summary and
    review_comment are additive; review_comment carries the posted comment's
    {id, html_url} (or None when no ref was captured).

    ``base_sha`` (the base/merge-base commit SHA) and ``request_signature`` (the
    focus-flags + discovery-enabled equivalence signature) are persisted for the
    spec-010 reuse decision (:func:`app.review_reuse.should_reuse`); both are
    additive and default to empty so pre-010 readers are unaffected.
    """
    try:
        import time as _time

        from app.review_identity import finding_key
        sidecar_dir = Path(instance_dir) / ".review-findings"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = sidecar_dir / f"{owner}_{repo}_{pr_number}.json"
        # Stamp each finding with its stable identity key (spec 010, FR-002) so a
        # later review can reconcile against these without re-deriving identity.
        # Copy rather than mutate the caller's dicts.
        stamped_comments = [
            {**fc, "identity_key": finding_key(fc)}
            if isinstance(fc, dict) else fc
            for fc in (file_comments or [])
        ]
        data = {
            "schema_version": 1,
            "pr_key": f"{owner}/{repo}#{pr_number}",
            "project_name": project_name,
            "base_ref": base_ref,
            "head_sha": head_sha,
            "base_sha": base_sha or "",
            "request_signature": request_signature or {},
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "file_comments": stamped_comments,
            "review_summary": review_summary or {},
            "review_comment": review_comment or None,
        }
        from app.utils import atomic_write_json
        atomic_write_json(sidecar_path, data, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        print(
            f"[review_runner] failed to write review findings sidecar: {exc}",
            file=sys.stderr,
        )


def _load_calibration_hints(project_name: Optional[str]) -> str:
    """Load calibration hints from learnings.md for the given project."""
    if not project_name:
        return ""
    try:
        learnings_path = (
            KOAN_ROOT / "instance" / "memory" / "projects"
            / project_name / "learnings.md"
        )
        if not learnings_path.is_file():
            return ""
        text = learnings_path.read_text()
        last_section: list = []
        in_section = False
        for line in text.splitlines():
            # Writer (_append_lessons_to_learnings) emits a level-2 heading:
            # "## Review calibration (YYYY-MM-DD)". Match that, and end the
            # section at the next level-2 heading.
            if line.startswith("## Review calibration"):
                in_section = True
                last_section = [line]
            elif in_section:
                if line.startswith("## "):
                    in_section = False
                else:
                    last_section.append(line)
        return "\n".join(last_section).strip()
    except OSError as exc:
        print(
            f"[review_runner] failed to load calibration hints: {exc}",
            file=sys.stderr,
        )
        return ""


def _reflect_findings(
    findings: list,
    diff: str,
    project_path: str,
    model: Optional[str],
    threshold: int,
    skill_dir: Optional[Path] = None,
    calibration_hints: str = "",
    project_name: str = "",
) -> Tuple[list, list]:
    """Run a second-pass reflection on review findings and filter low-signal ones.

    Calls Claude with a lightweight reflection prompt to score each finding
    0-10. Returns ``(findings, retained_original_indices)`` for findings whose
    score meets the threshold. On any parse or validation failure, returns the
    original findings and all of their indices unchanged (fail-open).

    Args:
        findings: List of file_comment dicts from the first-pass review.
        diff: PR diff string for context.
        project_path: Path to the project for codebase context.
        model: Model override for the reflection call (uses lightweight default).
        threshold: Minimum score (0-10) for a finding to be kept.
        calibration_hints: Optional calibration hints from outcome tracking.

    Returns:
        Filtered findings and their indices in the original findings list.
    """
    # Clamp threshold to valid range
    threshold = max(0, min(10, threshold))

    if not findings or threshold <= 0:
        return findings, list(range(len(findings)))

    if skill_dir is None:
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "review"

    try:
        findings_json = json.dumps(findings, indent=2)
        prompt = load_skill_prompt(
            skill_dir, "reflect",
            project_path=project_path,
            FINDINGS_JSON=findings_json,
            DIFF=diff or "(diff not available)",
            CALIBRATION_HINTS=calibration_hints or "(no calibration data available)",
        )
    except Exception as e:
        print(f"[reflect] prompt build failed: {e}", file=sys.stderr)
        return findings, list(range(len(findings)))

    raw_output, error = _run_claude_review(
        prompt, project_path, model=model, project_name=project_name,
    )
    if not raw_output:
        return findings, list(range(len(findings)))

    # Parse and validate response
    try:
        # Strip markdown fences if present
        text = raw_output.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        scores = json.loads(text)
    except json.JSONDecodeError:
        return findings, list(range(len(findings)))

    if not isinstance(scores, list):
        return findings, list(range(len(findings)))

    # Build index → score map; skip out-of-range indices
    score_map: dict = {}
    for entry in scores:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("finding_index")
        score = entry.get("score")
        if not isinstance(idx, (int, float)) or not isinstance(score, (int, float)):
            continue
        idx = int(idx)
        score = int(score)
        if 0 <= idx < len(findings):
            score_map[idx] = score

    # Keep findings whose score meets threshold (or whose index wasn't scored)
    retained_indices = [
        i for i in range(len(findings))
        if score_map.get(i, threshold) >= threshold
    ]
    filtered = [findings[i] for i in retained_indices]

    return filtered, retained_indices


def _reconcile_review_after_reflection(
    review_data: dict,
    reflected_findings: list,
    retained_indices: list,
) -> dict:
    """Finalize a coherent review after authoritative reflection filtering.

    A valid reflection result determines the final findings. Malformed or
    inconsistent reflection metadata preserves the primary review unchanged.
    """
    original_findings = review_data.get("file_comments")
    summary = review_data.get("review_summary")
    if not isinstance(original_findings, list) or not isinstance(summary, dict):
        return review_data

    if not isinstance(reflected_findings, list) or not isinstance(
        retained_indices, list
    ):
        log(
            "review",
            "Invalid reflection metadata types; preserving primary review",
        )
        return review_data

    if len(reflected_findings) != len(retained_indices):
        log(
            "review",
            "Reflection result/index length mismatch; preserving primary review",
        )
        return review_data

    if any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= len(original_findings)
        for index in retained_indices
    ):
        log(
            "review",
            "Invalid retained finding index; preserving primary review",
        )
        return review_data

    if len(set(retained_indices)) != len(retained_indices):
        log(
            "review",
            "Duplicate retained finding index; preserving primary review",
        )
        return review_data

    expected_findings = [
        original_findings[index] for index in retained_indices
    ]
    if reflected_findings != expected_findings:
        log(
            "review",
            "Reflection finding/index mismatch; preserving primary review",
        )
        return review_data

    checklist = summary.get("checklist")
    if not isinstance(checklist, list):
        log(
            "review",
            "Malformed checklist metadata; preserving primary review",
        )
        return review_data

    for entry in checklist:
        if not isinstance(entry, dict):
            log(
                "review",
                "Malformed checklist entry; preserving primary review",
            )
            return review_data
        refs = entry.get("finding_refs", [])
        if not isinstance(refs, list):
            log(
                "review",
                "Malformed checklist finding_refs; preserving primary review",
            )
            return review_data

    old_to_new = {
        old_index: new_index
        for new_index, old_index in enumerate(retained_indices)
    }
    reconciled_checklist = []

    for entry in checklist:
        refs = entry.get("finding_refs", [])
        remapped_refs = []
        seen = set()
        for old_index in refs:
            if isinstance(old_index, bool) or not isinstance(old_index, int):
                continue
            new_index = old_to_new.get(old_index)
            if new_index is not None and new_index not in seen:
                seen.add(new_index)
                remapped_refs.append(new_index)

        if entry.get("passed") is False and refs and not remapped_refs:
            continue

        reconciled_entry = dict(entry)
        if "finding_refs" in entry:
            reconciled_entry["finding_refs"] = remapped_refs
        reconciled_checklist.append(reconciled_entry)

    review_data["file_comments"] = expected_findings
    summary["checklist"] = reconciled_checklist
    summary["lgtm"] = not any(
        isinstance(finding, dict)
        and finding.get("severity") in ("critical", "warning")
        for finding in expected_findings
    )

    return review_data


def _read_file_at_sha(
    owner: str, repo: str, sha: str, path: str, project_path: str,
    timeout: int = 20,
) -> Optional[str]:
    """Return the file content at the reviewed SHA, or None if unavailable.

    Read-only and non-mutating. Fast path: ``git show <sha>:<path>`` against the
    local clone (offline, no fetch, no checkout). If the object is absent
    locally — the common ``/review`` case, where only the diff was fetched —
    fall back to the GitHub contents API pinned to the SHA. Returns None on
    deletion-at-HEAD (404) or any error, so callers treat "unavailable" as
    fail-open (never a false drop).
    """
    if not (sha and path):
        return None
    # 1) Local object DB — offline, no network, no working-tree mutation.
    if project_path:
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            r = subprocess.run(
                ["git", "show", f"{sha}:{path}"],
                cwd=project_path, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
            if r.returncode == 0:
                return r.stdout
    # 2) GitHub contents API pinned to the SHA (works without local objects).
    #    Still fail-open (return None), but leave a breadcrumb: a systematic
    #    failure here (auth, rate limit, decode) otherwise silently disables
    #    snippet validation and reconciliation for the whole review.
    try:
        raw = run_gh(
            "api", f"repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
            "-X", "GET", "-f", f"ref={sha}", "--jq", ".content // empty",
            timeout=timeout,
        )
        if raw and raw.strip():
            return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception as e:
        # Broad by design: this is a fail-open boundary (return None on any
        # error), matching sibling enrichment helpers. Log so a systematic
        # failure is observable instead of silently degrading the review.
        log("review", f"contents fetch failed for {path}@{sha[:12]}: {e}")
    return None


def _is_line_subsequence(needle: list, haystack: list) -> bool:
    """True when ``needle`` is an ordered subsequence of ``haystack``."""
    it = iter(haystack)
    return all(line in it for line in needle)


def _snippet_matches_at_anchor(
    file_text: str, line_start: int, line_end: int, snippet: str,
    fuzz_lines: int = 3,
) -> Tuple[bool, str]:
    """Return ``(matches, authoritative_lines)``.

    Compares the model snippet against the file's actual ``[line_start,
    line_end]`` window (1-based), tolerating indentation and blank-line drift by
    normalizing each line (strip + drop blanks) and widening the window by
    ``fuzz_lines`` on each side before declaring a mismatch. ``authoritative_lines``
    is the verbatim current text of the anchor window — what callers render so
    the quote can never diverge from the ``file:line`` permalink.
    """
    lines = file_text.splitlines()
    n = len(lines)
    ls = max(1, min(line_start, n)) if n else 0
    le = line_end if (line_end and line_end >= ls) else ls
    le = min(le, n)
    authoritative = "\n".join(lines[ls - 1:le]) if n and ls >= 1 else ""
    if line_start <= 0 or line_start > n:
        return False, authoritative
    w_start = max(1, line_start - fuzz_lines)
    w_end = min(n, le + fuzz_lines)
    window_norm = [ln.strip() for ln in lines[w_start - 1:w_end] if ln.strip()]
    snip_norm = [ln.strip() for ln in snippet.splitlines() if ln.strip()]
    if not snip_norm:
        return True, authoritative
    return _is_line_subsequence(snip_norm, window_norm), authoritative


def _remap_findings_after_drop(review_data: dict, drop_indices: set) -> None:
    """Drop findings by index and re-sync checklist refs + the derived verdict.

    Mirrors the index-remap tail of ``_reconcile_review_after_reflection`` so
    ``checklist.finding_refs`` and the severity-derived ``lgtm`` stay coherent
    after findings are removed. Mutates ``review_data`` in place.
    """
    original = review_data.get("file_comments")
    if not isinstance(original, list) or not drop_indices:
        return
    kept = [i for i in range(len(original)) if i not in drop_indices]
    old_to_new = {old: new for new, old in enumerate(kept)}
    review_data["file_comments"] = [original[i] for i in kept]

    summary = review_data.get("review_summary")
    if isinstance(summary, dict):
        checklist = summary.get("checklist") or []
        for entry in checklist:
            if not isinstance(entry, dict) or not isinstance(entry.get("finding_refs"), list):
                continue
            remapped: list = []
            seen: set = set()
            for old_index in entry["finding_refs"]:
                if isinstance(old_index, bool) or not isinstance(old_index, int):
                    continue
                new_index = old_to_new.get(old_index)
                if new_index is not None and new_index not in seen:
                    seen.add(new_index)
                    remapped.append(new_index)
            entry["finding_refs"] = remapped
        # Severity is the sole source of truth for the formal verdict.
        summary["lgtm"] = not any(
            isinstance(f, dict) and f.get("severity") in ("critical", "warning")
            for f in review_data["file_comments"]
        )


def _validate_finding_snippets(
    review_data: dict, owner: str, repo: str, sha: str, project_path: str,
    on_mismatch: str = "resync",
) -> dict:
    """Reconcile each finding's ``code_snippet`` with authoritative SHA content.

    The quoted block is model-authored; without this gate it can show pre-fix
    source under a ``file:line`` permalink that points at the fixed code. For
    each finding with a file + positive ``line_start`` + non-empty
    ``code_snippet``:

      - unavailable content (transient error / can't fetch) -> keep (fail-open)
      - anchor line out of range at HEAD -> drop (location no longer exists)
      - snippet matches at/near anchor -> keep
      - else apply ``on_mismatch``: "resync" (replace the quote with the real
        current lines), "drop", "annotate" (append current source to comment),
        or "off" (leave untouched).

    Findings with no line range or empty snippet are left untouched
    (illustrative / whole-file safe). Mutates ``review_data`` in place.
    """
    findings = review_data.get("file_comments")
    if not isinstance(findings, list) or not findings:
        return review_data
    cache: dict = {}

    def _fetch(p: str) -> Optional[str]:
        if p not in cache:
            cache[p] = _read_file_at_sha(owner, repo, sha, p, project_path)
        return cache[p]

    drop_indices: set = set()
    for idx, item in enumerate(findings):
        if not isinstance(item, dict):
            continue
        path = item.get("file")
        line_start = item.get("line_start") or 0
        snippet = item.get("code_snippet") or ""
        if not path or line_start <= 0 or not snippet:
            continue
        content = _fetch(path)
        if content is None:
            continue  # fail-open: never drop on a transient fetch failure
        line_count = len(content.splitlines())
        if line_start > line_count:
            if on_mismatch != "off":
                drop_indices.add(idx)
            continue
        matches, authoritative = _snippet_matches_at_anchor(
            content, line_start, item.get("line_end") or 0, snippet,
        )
        if matches:
            continue
        if on_mismatch == "resync":
            item["code_snippet"] = authoritative
        elif on_mismatch == "drop":
            drop_indices.add(idx)
        elif on_mismatch == "annotate":
            extra = "\n".join(f"> {ln}" for ln in authoritative.splitlines()[:20])
            item["comment"] = (item.get("comment") or "") + (
                "\n\n> ⓘ Current source at the reviewed commit:\n" + extra
            )
        # "off": leave the snippet untouched.
    if drop_indices:
        _remap_findings_after_drop(review_data, drop_indices)
    return review_data


def _read_prior_findings_sidecar(
    instance_dir: str, owner: str, repo: str, pr_number: str,
) -> Tuple[list, str]:
    """Return ``(prior_file_comments, prior_head_sha)`` from the review sidecar.

    Reads the file written by :func:`_write_review_findings_sidecar` for the
    *previous* run of this PR. Best-effort; any read/parse error yields
    ``([], "")``.
    """
    try:
        sidecar_path = (
            Path(instance_dir) / ".review-findings"
            / f"{owner}_{repo}_{pr_number}.json"
        )
        if not sidecar_path.is_file():
            return [], ""
        data = json.loads(sidecar_path.read_text())
        file_comments = data.get("file_comments")
        if not isinstance(file_comments, list):
            file_comments = []
        return file_comments, str(data.get("head_sha") or "")
    except (OSError, ValueError, TypeError) as exc:
        log("review", f"could not read prior findings sidecar: {exc}")
        return [], ""


def _read_prior_review_record(
    instance_dir: str, owner: str, repo: str, pr_number: str,
) -> Optional[dict]:
    """Return the full prior-review sidecar record (or ``None``), fail-open.

    Unlike :func:`_read_prior_findings_sidecar` (which returns just the findings
    and head SHA for reconciliation), this exposes the whole record — including
    ``base_sha``, ``request_signature``, ``review_summary`` and ``review_comment``
    — needed for the spec-010 reuse decision (:func:`app.review_reuse.should_reuse`).
    Any missing/corrupt file yields ``None`` so the caller falls back to a fresh
    review (FR-007).
    """
    try:
        sidecar_path = (
            Path(instance_dir) / ".review-findings"
            / f"{owner}_{repo}_{pr_number}.json"
        )
        if not sidecar_path.is_file():
            return None
        data = json.loads(sidecar_path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError) as exc:
        log("review", f"could not read prior review record: {exc}")
        return None


def _line_ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True when two 1-based inclusive line ranges overlap."""
    a_end = a_end or a_start
    b_end = b_end or b_start
    if a_start <= 0 or b_start <= 0:
        return False
    return a_start <= b_end and b_start <= a_end


def _reconcile_prior_findings(
    review_data: dict, prior_findings: list,
    owner: str, repo: str, sha: str, project_path: str,
) -> list:
    """Suppress current findings that re-raise an already-resolved prior finding.

    Uses the SAME pinned source as :func:`_validate_finding_snippets`. A prior
    finding is "resolved" when its snippet no longer matches at/near its prior
    anchor at HEAD (or its file/lines are gone). A current finding is dropped
    only when it is a near-duplicate of a resolved prior finding (same file +
    overlapping lines or identical title) AND its own snippet also fails its
    live-source check — so a genuinely-still-broken finding is never suppressed.
    Fail-open throughout. Returns the list of resolved prior findings (for the
    "Resolved since last review" note). Must run BEFORE snippet resync, which
    would otherwise rewrite current snippets to match live source.
    """
    resolved: list = []
    if not isinstance(prior_findings, list) or not prior_findings:
        return resolved
    current = review_data.get("file_comments")
    if not isinstance(current, list):
        return resolved
    cache: dict = {}

    def _fetch(p: str) -> Optional[str]:
        if p not in cache:
            cache[p] = _read_file_at_sha(owner, repo, sha, p, project_path)
        return cache[p]

    def _still_present(f: Optional[str], ls: int, le: int, snip: str) -> Optional[bool]:
        """True/False if present; None when undecidable (fail-open)."""
        if not f or ls <= 0 or not snip:
            return None
        content = _fetch(f)
        if content is None:
            return None
        if ls > len(content.splitlines()):
            return False
        matches, _ = _snippet_matches_at_anchor(content, ls, le, snip)
        return matches

    for pf in prior_findings:
        if not isinstance(pf, dict):
            continue
        present = _still_present(
            pf.get("file"), pf.get("line_start") or 0,
            pf.get("line_end") or 0, pf.get("code_snippet") or "",
        )
        if present is False:
            resolved.append(pf)
    if not resolved:
        return resolved

    drop: set = set()
    for idx, cf in enumerate(current):
        if not isinstance(cf, dict):
            continue
        cf_file = cf.get("file")
        cf_ls = cf.get("line_start") or 0
        cf_le = cf.get("line_end") or 0
        cf_title = (cf.get("title") or "").strip().lower()
        for pf in resolved:
            if pf.get("file") != cf_file:
                continue
            same_title = bool(cf_title) and cf_title == (pf.get("title") or "").strip().lower()
            if not (_line_ranges_overlap(
                cf_ls, cf_le, pf.get("line_start") or 0, pf.get("line_end") or 0,
            ) or same_title):
                continue
            present = _still_present(cf_file, cf_ls, cf_le, cf.get("code_snippet") or "")
            if present is False:
                drop.add(idx)
                break
    if drop:
        _remap_findings_after_drop(review_data, drop)
    return resolved


def _apply_review_accuracy_gate(
    review_data: dict, *, owner: str, repo: str, sha: str,
    project_path: str, project_name: str, pr_number: str,
) -> None:
    """Reconcile a parsed review against the reviewed HEAD before it is used.

    Runs prior-finding reconciliation (suppress re-raised, already-fixed
    findings) THEN snippet validation (resync/drop stale quotes) — the order
    matters, since resync would otherwise rewrite current snippets to match live
    source and mask a re-raise. Both passes are config-gated and fail-open.
    Mutates ``review_data`` in place; stashes resolved priors under
    ``_resolved_prior`` for rendering.
    """
    if not isinstance(review_data, dict) or not sha:
        return
    from app.config import (
        get_review_consistency_config,
        get_review_reconcile_config,
        get_review_snippet_validation_config,
    )
    # Read the prior review's findings + head once; both reconcile and the
    # spec-010 freeze use them.
    prior_findings, prior_head = _read_prior_findings_sidecar(
        str(KOAN_ROOT / "instance"), owner, repo, pr_number,
    )
    rc_cfg = get_review_reconcile_config(project_name or "")
    if rc_cfg["enabled"] and prior_findings:
        resolved = _reconcile_prior_findings(
            review_data, prior_findings, owner, repo, sha, project_path,
        )
        if resolved and rc_cfg["show_resolved"]:
            review_data["_resolved_prior"] = resolved
    sv_cfg = get_review_snippet_validation_config(project_name or "")
    if sv_cfg["enabled"]:
        _validate_finding_snippets(
            review_data, owner, repo, sha, project_path,
            on_mismatch=sv_cfg["on_mismatch"],
        )
    # Spec 010 (US1, FR-003): re-review freeze. On a re-review (head moved),
    # suppress first-time non-critical findings on files unchanged since the
    # prior review — the "review whiplash" case — keeping a critical (labelled
    # pre-existing). Fail-open: no prior head / undetermined changeset -> no-op.
    cc_cfg = get_review_consistency_config(project_name or "")
    if cc_cfg["freeze_enabled"] and prior_head and prior_head != sha:
        from app.review_reconcile import compute_freeze
        changed_files = _compare_changed_files(owner, repo, prior_head, sha)
        drop_indices, freeze_summary = compute_freeze(
            review_data, prior_findings, changed_files, prior_head=prior_head,
        )
        if drop_indices:
            _remap_findings_after_drop(review_data, drop_indices)
        if freeze_summary["suppressed"] or freeze_summary["kept_pre_existing_critical"]:
            review_data["_freeze_summary"] = freeze_summary
            log(
                "review",
                f"freeze: suppressed {freeze_summary['suppressed']} first-time "
                f"finding(s) on unchanged code, kept "
                f"{freeze_summary['kept_pre_existing_critical']} pre-existing critical",
            )
    # Spec 010 (US6, FR-027/028): deterministically enforce the pre-existing rule
    # on findings the reviewer tagged `[Pre-Existing Issue]` — demote non-critical
    # ones to a non-blocking suggestion, keep criticals, and re-derive lgtm. Runs
    # AFTER the freeze so FR-030 "freeze wins" holds. Fail-open.
    from app.review_triage import enforce_deferred, enforce_pre_existing
    pe_summary = enforce_pre_existing(review_data)
    if pe_summary["demoted"] or pe_summary["critical_labeled"]:
        review_data["_pre_existing_summary"] = pe_summary
        log(
            "review",
            f"pre-existing: demoted {pe_summary['demoted']} to recommendation, "
            f"labelled {pe_summary['critical_labeled']} critical",
        )
    # Spec 010 (US7, FR-034): human-deferred findings ([Deferred]) are forced to
    # a non-blocking suggestion. Detection is prompt-driven from live PR comments
    # (re-read each review, so a disposition is sticky until retracted). Fail-open.
    df_summary = enforce_deferred(review_data)
    if df_summary["deferred"]:
        review_data["_deferred_summary"] = df_summary
        log("review", f"deferred: {df_summary['deferred']} finding(s) -> recommendation")


_ERROR_PATTERN_RE = re.compile(
    r'try:|except |catch\(|\.catch\(|on_error',
    re.IGNORECASE,
)


def _should_run_error_hunter(diff: str) -> bool:
    """Return True if added lines in the diff contain error-handling patterns."""
    added_lines = '\n'.join(
        line for line in diff.splitlines() if line.startswith('+')
    )
    return bool(_ERROR_PATTERN_RE.search(added_lines))


def _run_error_hunter(
    diff: str, project_path: str, skill_dir: Optional[Path],
    owner: str = "", repo: str = "", head_sha: str = "",
    project_name: str = "", start_id: int = 1,
) -> str:
    """Run the silent-failure-hunter pass and return formatted markdown section.

    Returns an empty string if no findings are produced.
    """
    if skill_dir is not None:
        prompt = load_skill_prompt(
            skill_dir, "silent-failure-hunter",
            project_path=project_path, DIFF=diff,
        )
    else:
        prompt = load_prompt("silent-failure-hunter", DIFF=diff)

    raw_output, error = _run_claude_review(prompt, project_path, project_name=project_name)
    if not raw_output:
        print(
            f"[review_runner] silent-failure-hunter pass failed: {error}",
            file=sys.stderr,
        )
        return ""

    # Parse JSON array of findings
    findings = _parse_error_hunter_output(raw_output)
    if not findings:
        return ""

    return _format_error_hunter_findings(
        findings, owner=owner, repo=repo, head_sha=head_sha, start_id=start_id,
    )


def _parse_error_hunter_output(raw_output: str) -> list:
    """Parse the JSON array returned by the silent-failure-hunter prompt."""
    # Try to find a JSON array in the output
    match = re.search(r'\[\s*\{.*?\}\s*\]', raw_output, re.DOTALL)
    if match:
        try:
            findings = json.loads(match.group(0))
            if isinstance(findings, list):
                return findings
        except json.JSONDecodeError:
            pass

    # Try parsing the whole output as JSON
    stripped = raw_output.strip()
    # Remove markdown code fences if present
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        stripped = "\n".join(lines[1:-1]) if len(lines) > 2 else stripped

    try:
        findings = json.loads(stripped)
        if isinstance(findings, list):
            return findings
    except json.JSONDecodeError:
        pass

    print(
        "[review_runner] silent-failure-hunter: could not parse JSON output",
        file=sys.stderr,
    )
    return []


_ERROR_HUNTER_SEVERITY_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}


def _format_error_hunter_findings(
    findings: list, owner: str = "", repo: str = "", head_sha: str = "",
    start_id: int = 1,
) -> str:
    """Format error-hunter findings as a markdown section with collapsible details."""
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    findings = sorted(findings, key=lambda f: severity_order.get(f.get("severity", "MEDIUM"), 2))

    lines = ["## Silent Failure Analysis", ""]
    for finding_id, f in enumerate(findings, start_id):
        severity = f.get("severity", "?")
        emoji = _ERROR_HUNTER_SEVERITY_EMOJI.get(severity, "⚪")
        pattern = f.get("pattern", "unknown pattern")
        file_path = f.get("file", "")
        line_hint = f.get("line_hint", "")
        location = f"{file_path}:{line_hint}" if line_hint else file_path
        snippet = f.get("snippet", "")
        explanation = f.get("explanation", "")
        suggestion = f.get("suggestion", "")

        title = f"{emoji} **{finding_id}. {severity}** — {pattern}"
        # Link the location on its own line when line_hint is numeric and a
        # head-SHA permalink can be built; else keep the plain-text suffix.
        loc_line = ""
        if location:
            line_start, line_end = _parse_line_hint(line_hint)
            url = _github_blob_url(owner, repo, head_sha, file_path, line_start, line_end)
            if url:
                # Build the display text from the parsed line numbers so it
                # stays in sync with the link target (line_hint may carry
                # extra free-form text). Escape since file_path is model output.
                loc_text = f"{file_path}:{line_start}"
                if line_end and line_end != line_start:
                    loc_text += f"-{line_end}"
                loc_line = f'<a href="{url}">{html.escape(loc_text)}</a>'
            else:
                title += f" (`{location}`)"

        lines.append("<details>")
        if loc_line:
            lines.append("<summary>")
            lines.append(f"{title}<br>")
            lines.append(loc_line)
            lines.append("</summary>")
        else:
            lines.append(f"<summary>{title}</summary>")
        lines.append("")
        if explanation:
            lines.append(f"**Risk**: {explanation}")
            lines.append("")
        if snippet:
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
            lines.append("")
        if suggestion:
            lines.append(f"**Fix**: {suggestion}")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines).rstrip()


def _format_bot_comments_for_prompt(comments: List[dict]) -> str:
    """Format bot comments for inclusion in the triage prompt."""
    lines = []
    for c in comments:
        path = c.get("path", "")
        line_num = c.get("line", "?")
        lines.append(f"### Comment ID: {c['id']} ({path}:{line_num})")
        lines.append(c.get("body", ""))
        lines.append("")
    return "\n".join(lines)


def _run_bot_comment_triage(
    bot_comments: List[dict],
    diff: str,
    skill_dir: Optional[Path],
    project_path: str = "",
    project_name: str = "",
) -> List[dict]:
    """Run Claude triage on bot inline comments.

    Returns a list of ``{comment_id, reply}`` dicts for actionable comments,
    compatible with ``_post_comment_replies()``.
    """
    if not bot_comments:
        return []

    try:
        formatted_comments = _format_bot_comments_for_prompt(bot_comments)
        max_diff = 12_000
        truncated_diff = diff[:max_diff] + "\n...(truncated)" if len(diff) > max_diff else diff

        if skill_dir is not None:
            prompt = load_skill_prompt(
                skill_dir, "bot-review-triage",
                project_path=project_path,
                diff=truncated_diff, bot_comments=formatted_comments,
            )
        else:
            prompt = load_prompt(
                "bot-review-triage",
                diff=truncated_diff, bot_comments=formatted_comments,
            )
    except Exception as e:
        print(f"[review_runner] failed to load bot-review-triage prompt: {e}", file=sys.stderr)
        return []

    try:
        raw_output, error = _run_claude_review(prompt, project_path, project_name=project_name)
        if not raw_output:
            print(f"[review_runner] bot comment triage failed: {error}", file=sys.stderr)
            return []
    except Exception as e:
        print(f"[review_runner] bot comment triage failed: {e}", file=sys.stderr)
        return []

    try:
        stripped = raw_output.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            stripped = "\n".join(lines[1:-1]) if len(lines) > 2 else stripped
        entries = json.loads(stripped)
        if not isinstance(entries, list):
            return []
    except (json.JSONDecodeError, TypeError):
        return []

    replies = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("classification") != "actionable":
            continue
        reply = entry.get("reply", "").strip()
        comment_id = entry.get("comment_id")
        if reply and comment_id:
            replies.append({"comment_id": comment_id, "reply": reply})
    return replies


def _extract_review_body(raw_output: str) -> Optional[str]:
    """Extract structured review from Claude's raw output.

    Tries to find markdown-structured review content. If the output looks
    like JSON, attempts to parse and format it as markdown. Returns None
    when no structure can be recovered — callers MUST NOT post raw model
    output to a PR (see the guardrail in ``run_review``).
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

    # No structured review could be recovered. Signal failure rather than
    # leaking raw narration / JSON to the PR.
    return None


def _is_parseable_json(text: str) -> bool:
    """Return True if ``text`` parses as any JSON value (object, array, scalar)."""
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _loads_object_or_none(candidate: str) -> Optional[dict]:
    """json.loads ``candidate``, returning the dict or None on failure.

    Extracted so callers can attempt parsing inside a loop without a
    per-iteration try/except (PERF203).
    """
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _match_balanced_object(text: str, start: int) -> Optional[str]:
    """Return the balanced ``{ ... }`` substring beginning at ``start``.

    Tracks string context so braces inside JSON string values — and any
    markdown code fences embedded in those strings — do not affect nesting
    depth. Returns None if the braces never balance.
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
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
                return text[start:i + 1]
    return None


def _extract_json_text(text: str) -> Optional[str]:
    """Extract a JSON object string from text that may contain surrounding prose.

    Tries, in order:
    1. Direct parse of the full text (pure JSON).
    2. Strip markdown code fences wrapping the entire text (```json ... ```).
    3. Scan every ``{`` in the text, brace-match a balanced object at each
       (respecting string context), and return the largest substring that
       decodes to a JSON object.

    Strategy 3 is deliberately robust to two failure modes that previously
    caused raw model output to be posted to a PR: preamble prose containing
    brace-like tokens (e.g. GitHub Actions ``${{ ... }}`` expressions, whose
    leading ``{`` would otherwise hijack a first-brace-only matcher) and
    markdown code fences embedded inside JSON string values (which defeat
    fence-based regexes). The largest balanced object wins because the review
    object always wraps its nested file-comment objects.
    """
    stripped = text.strip()

    # Strategy 1: pure JSON
    if _is_parseable_json(stripped):
        return stripped

    # Strategy 2: text wrapped entirely in code fences
    fence_stripped = stripped
    if fence_stripped.startswith("```json"):
        fence_stripped = fence_stripped[len("```json"):]
    elif fence_stripped.startswith("```"):
        fence_stripped = fence_stripped[len("```"):]
    if fence_stripped.endswith("```"):
        fence_stripped = fence_stripped[:-3]
    fence_stripped = fence_stripped.strip()
    if fence_stripped != stripped and _is_parseable_json(fence_stripped):
        return fence_stripped

    # Strategy 3: scan every '{' and keep the largest balanced object that
    # decodes to a JSON object.
    best: Optional[str] = None
    pos = stripped.find("{")
    while pos != -1:
        candidate = _match_balanced_object(stripped, pos)
        if (
            candidate is not None
            and _loads_object_or_none(candidate) is not None
            and (best is None or len(candidate) > len(best))
        ):
            best = candidate
        pos = stripped.find("{", pos + 1)
    return best


def _normalize_review_data(data: object) -> object:
    """Backfill sentinel-defaultable fields the model commonly omits.

    The review schema declares every field required with an explicit sentinel
    value (empty array / empty string / False) rather than marking any field
    optional. But the model often produces a semantically complete, terse
    review that simply omits a field whose sentinel is unambiguous — most
    commonly ``review_summary.checklist`` (for trivial PRs) and a
    ``file_comments[].code_snippet``. Hard-rejecting the whole review for one
    such omission discards a useful review and posts the "could not be
    formatted" placeholder instead (observed on esphome/device-builder#1178).

    This fills in those sentinel defaults in place so a terse review survives
    validation. It deliberately does NOT fabricate semantically meaningful
    fields (``summary``, ``comment``, ``title``, ``severity``, line numbers,
    checklist ``item``/``passed``) — if those are missing the review is
    genuinely incomplete and should still fail validation.
    """
    if not isinstance(data, dict):
        return data

    # Top-level sentinel: an absent file_comments array means "no inline
    # findings", which is a valid (LGTM) review.
    if "file_comments" not in data:
        data["file_comments"] = []

    fc = data.get("file_comments")
    if isinstance(fc, list):
        for item in fc:
            if isinstance(item, dict) and "code_snippet" not in item:
                item["code_snippet"] = ""

    rs = data.get("review_summary")
    if isinstance(rs, dict):
        # checklist is explicitly allowed to be empty for trivial PRs.
        if "checklist" not in rs:
            rs["checklist"] = []
        checklist = rs.get("checklist")
        if isinstance(checklist, list):
            for entry in checklist:
                # Backfill the index-based field only when the entry carries no
                # cross-reference at all. A legacy string ``finding_ref`` is left
                # intact so the renderer's fallback path can still honor it.
                if (
                    isinstance(entry, dict)
                    and "finding_refs" not in entry
                    and "finding_ref" not in entry
                ):
                    entry["finding_refs"] = []
        # lgtm is derivable from finding severities when omitted: blocking iff
        # any critical/warning finding is present.
        if "lgtm" not in rs and isinstance(fc, list):
            rs["lgtm"] = not any(
                isinstance(c, dict) and c.get("severity") in ("critical", "warning")
                for c in fc
            )

    cr = data.get("comment_replies")
    if isinstance(cr, list):
        for item in cr:
            if isinstance(item, dict):
                action = item.get("action")
                if not isinstance(action, str) or action not in _VALID_REPLY_ACTIONS:
                    item["action"] = "acknowledged"

    return data


def _parse_and_validate_review(
    raw_output: str,
) -> Tuple[Optional[dict], Optional[list]]:
    """Parse and validate JSON review output, surfacing validation errors.

    Handles JSON wrapped in markdown code fences or surrounded by
    preamble/postamble text.

    Returns ``(data, errors)``:
      - ``(data, None)`` on success;
      - ``(None, errors)`` when the output parsed as JSON but failed schema
        validation — ``errors`` is the human-readable list, so a retry can
        tell the model exactly which rule it broke (e.g. a verdict that
        contradicts the finding severities), not just "invalid JSON";
      - ``(None, None)`` when the output could not be parsed as JSON at all.
    """
    json_text = _extract_json_text(raw_output)
    if json_text is None:
        return None, None

    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return None, None

    data = _normalize_review_data(data)

    is_valid, errors = validate_review(data)
    if not is_valid:
        print(
            f"[review_runner] JSON validation errors: {errors}",
            file=sys.stderr,
        )
        return None, errors
    return data, None


def _parse_review_json(raw_output: str) -> Optional[dict]:
    """Attempt to parse and validate JSON review output.

    Thin wrapper over :func:`_parse_and_validate_review` for callers that only
    need the parsed dict (returns ``None`` on any failure). Kept because many
    call sites and the eval harness expect a single ``Optional[dict]`` return.
    """
    data, _ = _parse_and_validate_review(raw_output)
    return data


def _build_review_retry_prompt(
    prompt: str, validation_errors: Optional[list],
) -> str:
    """Build the retry prompt after a failed first review response.

    Distinguishes the two failure modes so the fix guidance actually reaches
    the model:
      - When the output parsed as JSON but broke a review rule
        (``validation_errors`` non-empty — e.g. a verdict contradicting the
        finding severities), list the exact rule violations. Otherwise the
        model would be re-prompted about JSON validity and never learn the
        verdict rule, so a semantically-good review gets discarded.
      - When the output was not valid JSON at all, ask for valid JSON only.
    """
    if validation_errors:
        rules = "\n".join(f"- {error}" for error in validation_errors)
        return (
            prompt
            + "\n\nIMPORTANT: Your previous response was valid JSON but broke "
            "these review rules:\n"
            + rules
            + "\n\nFix every rule above and respond again with ONLY a valid "
            "JSON object matching the schema described above. No markdown, "
            "no commentary."
        )
    return (
        prompt
        + "\n\nIMPORTANT: Your previous response was not valid JSON. "
        "You MUST respond with ONLY a valid JSON object matching the "
        "schema described above. No markdown, no text, just JSON."
    )


def _safe_code_fence(content: str) -> str:
    """Return a backtick fence long enough to not conflict with content."""
    max_run = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return "`" * max(3, max_run + 1)


def _fix_nested_fences(text: str) -> str:
    """Re-fence code blocks whose content contains backtick runs that break them."""
    lines = text.split("\n")
    result: list = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(`{3,})(.*)", lines[i])
        if m:
            fence_len = len(m.group(1))
            lang = m.group(2)
            content_lines: list = []
            i += 1
            closed = False
            while i < len(lines):
                if re.match(r"^`{" + str(fence_len) + r",}\s*$", lines[i]):
                    closed = True
                    break
                content_lines.append(lines[i])
                i += 1
            if closed:
                content = "\n".join(content_lines)
                fence = _safe_code_fence(content)
                result.append(f"{fence}{lang}")
                result.extend(content_lines)
                result.append(fence)
                i += 1
            else:
                result.append(f"{m.group(1)}{lang}")
                result.extend(content_lines)
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


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

# Posted to the PR when the model's output cannot be parsed into the structured
# review format. A short placeholder is posted instead of raw narration / JSON.
_UNPARSEABLE_REVIEW_NOTICE = (
    "⚠️ The automated review could not be formatted into the standard "
    "structure. Re-run `/review` to retry."
)


def _github_blob_url(
    owner: str, repo: str, sha: str, path: str,
    line_start: int, line_end: Optional[int] = None,
) -> str:
    """Build a GitHub blob permalink with a line anchor.

    Returns ``""`` when any component required to build a precise, durable
    link is missing (owner/repo/sha/path/positive line). Pinning to the head
    SHA keeps the anchor accurate even after the PR gets new commits. The
    range anchor uses GitHub's ``#L<start>-L<end>`` form (end is also
    ``L``-prefixed), distinct from the plain-text ``L514-537`` display style.
    """
    if not (owner and repo and sha and path and line_start and line_start > 0):
        return ""
    anchor = f"#L{line_start}"
    if line_end and line_end != line_start:
        anchor += f"-L{line_end}"
    # path may contain spaces / unicode — percent-encode but keep separators.
    safe_path = quote(path, safe="/")
    return f"https://github.com/{owner}/{repo}/blob/{sha}/{safe_path}{anchor}"


def _parse_line_hint(line_hint: str) -> Tuple[int, int]:
    """Parse an error-hunter ``line_hint`` string into (start, end).

    ``line_hint`` is free-form model output (e.g. ``"42"`` or ``"38-45"``).
    Returns ``(0, 0)`` when no leading integer can be extracted, so callers
    fall back to plain-text rendering.
    """
    if not line_hint:
        return 0, 0
    m = re.search(r"(\d+)(?:\s*-\s*(\d+))?", str(line_hint))
    if not m:
        return 0, 0
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return start, end


_LEGACY_FINDING_REF_RE = re.compile(
    r"(critical|warning|suggestion)\s*#\s*(\d+)", re.IGNORECASE
)


def _resolve_finding_labels(
    checklist_item: dict, index_to_label: dict, valid_labels: set,
) -> list:
    """Resolve a checklist item's finding cross-references to rendered labels.

    Preferred form is ``finding_refs`` — a list of 0-based indices into
    ``file_comments``. Each index is mapped to the label the renderer assigned
    that finding (e.g. ``"warning #2"``). Indices with no rendered finding are
    silently dropped, so a checklist can never reference a finding number that
    does not exist.

    Falls back to a legacy ``finding_ref`` string (e.g. ``"warning #1"``) when
    ``finding_refs`` is absent, keeping only tokens that match a rendered
    finding. Returns labels in ASCII ``#`` form (the caller escapes ``#`` for
    display). Order is preserved and duplicates removed.
    """
    refs = checklist_item.get("finding_refs")
    if isinstance(refs, list):
        labels: list = []
        seen: set = set()
        for ref in refs:
            if isinstance(ref, bool):
                continue
            if isinstance(ref, float) and ref == int(ref):
                ref = int(ref)
            if not isinstance(ref, int):
                continue
            label = index_to_label.get(ref)
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
        return labels

    legacy = checklist_item.get("finding_ref")
    if isinstance(legacy, str) and legacy:
        labels = []
        seen = set()
        for sev, num in _LEGACY_FINDING_REF_RE.findall(legacy):
            label = f"{sev.lower()} #{int(num)}"
            if label in valid_labels and label not in seen:
                seen.add(label)
                labels.append(label)
        return labels

    return []


def _format_triage_accounting(review_data: dict) -> str:
    """Compact, collapsed accounting of triage filtering (spec 010, US4, FR-024).

    Summarizes what triage did — findings frozen (suppressed on unchanged code),
    demoted to recommendation (pre-existing), or downgraded as deferred — so the
    filtering is auditable without competing with the primary findings (it is a
    single collapsed line). Returns "" when nothing was filtered, so a review with
    no filtering is byte-identical to before.
    """
    if not isinstance(review_data, dict):
        return ""
    freeze = review_data.get("_freeze_summary") or {}
    pre = review_data.get("_pre_existing_summary") or {}
    deferred = review_data.get("_deferred_summary") or {}
    parts: list = []
    if int(freeze.get("suppressed") or 0):
        parts.append(
            f"{int(freeze['suppressed'])} pre-existing finding(s) on unchanged "
            "code suppressed (freeze)")
    if int(pre.get("demoted") or 0):
        parts.append(
            f"{int(pre['demoted'])} pre-existing finding(s) demoted to recommendation")
    if int(deferred.get("deferred") or 0):
        parts.append(
            f"{int(deferred['deferred'])} finding(s) deferred at a reviewer's request")
    if not parts:
        return ""
    return (
        "<details><summary>ℹ️ Triage summary</summary>\n\n"
        + "; ".join(parts)
        + ".\n</details>"
    )


def _format_review_as_markdown(
    review_data: dict, title: str = "", bot_username: str = "",
    owner: str = "", repo: str = "", head_sha: str = "",
) -> str:
    """Convert validated review JSON into the markdown format for GitHub.

    Produces the standard ## PR Review format: the summary as the lead
    paragraph under the header, an optional plan alignment section (when
    present), then severity sections and the checklist. The summary is emitted
    only once — at the top — to avoid duplicating it in a trailing section.
    """
    comments = review_data["file_comments"]
    summary_data = review_data["review_summary"]

    lines: list = []

    # Header
    header = f"## PR Review — {title}" if title else "## PR Review"
    lines.append(header)
    lines.append("")
    summary_text = summary_data["summary"]
    # The summary is plain prose. The merge signal is carried by the
    # severity-graded verdict alert (_build_verdict_body), not by wrapping the
    # whole paragraph in a callout — a full-paragraph IMPORTANT block here
    # over-emphasizes it (parsimony rule, comment-formatting.md).
    lines.append(summary_text)
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
            lines.extend(f"- {req}" for req in met)
            lines.append("")
        if missing:
            lines.append(f"❌ **Missing** ({len(missing)})")
            lines.append("")
            lines.extend(f"- {req}" for req in missing)
            lines.append("")
        if out_of_scope:
            lines.append(f"📋 **Out of scope** ({len(out_of_scope)})")
            lines.append("")
            lines.extend(f"- {item}" for item in out_of_scope)
            lines.append("")
        lines.append("---")
        lines.append("")

    # Resolved since last review — prior findings programmatically verified to
    # no longer exist at the reviewed HEAD. Collapsible and short; suppressed
    # re-raises live here instead of being re-reported as new findings.
    resolved_prior = review_data.get("_resolved_prior") or []
    if resolved_prior:
        lines.append(f"### ✅ Resolved since last review ({len(resolved_prior)})")
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Previously-flagged issues verified fixed</summary>")
        lines.append("")
        for rf in resolved_prior:
            if not isinstance(rf, dict):
                continue
            rf_file = rf.get("file") or ""
            rf_line = rf.get("line_start") or 0
            loc = f"{rf_file}:{rf_line}" if rf_file and rf_line > 0 else rf_file
            rf_title = (rf.get("title") or "").strip() or "Finding"
            prefix = f"`{html.escape(loc)}` " if loc else ""
            lines.append(f"- {prefix}{html.escape(rf_title)}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Group comments by severity
    by_severity: dict = {"critical": [], "warning": [], "suggestion": []}
    for idx, c in enumerate(comments):
        sev = c.get("severity", "suggestion")
        by_severity.setdefault(sev, []).append((idx, c))

    # Map each file_comments index to the label the renderer assigns it
    # (e.g. "warning #3"). IDs are global across severity sections, 1-based,
    # and follow rendered order so every finding in one review can be named
    # unambiguously. Checklist cross-references use those same IDs.
    # Indices for severities that aren't rendered (anything outside the three
    # known levels) are intentionally omitted, so references to them get dropped.
    index_to_label: dict = {}
    finding_id_by_index: dict = {}
    next_finding_id = 1
    for sev in ("critical", "warning", "suggestion"):
        for idx, _item in by_severity.get(sev, []):
            finding_id_by_index[idx] = next_finding_id
            index_to_label[idx] = f"{sev} #{next_finding_id}"
            next_finding_id += 1

    # Emit severity sections (skip empty ones)
    for sev in ("critical", "warning", "suggestion"):
        items = by_severity.get(sev, [])
        if not items:
            continue
        emoji = _SEVERITY_EMOJI[sev]
        heading = _SEVERITY_HEADING[sev]
        lines.append(f"### {emoji} {heading}")
        lines.append("")
        for idx, item in items:
            title_line = f"<b>{finding_id_by_index[idx]}. {item['title']}</b>"
            line_start = item.get("line_start") or 0
            line_end = item.get("line_end") or 0
            lines.append("<details>")
            lines.append("<summary>")
            if line_start > 0:
                # Location on its own line inside the summary, as a clickable
                # head-SHA permalink when one can be built, else plain code.
                loc_text = f"{item['file']}:{line_start}"
                if line_end and line_end != line_start:
                    loc_text += f"-{line_end}"
                url = _github_blob_url(
                    owner, repo, head_sha, item["file"], line_start, line_end,
                )
                # Escape: loc_text is built from model-provided file paths.
                safe_loc = html.escape(loc_text)
                loc_line = f'<a href="{url}">{safe_loc}</a>' if url else f"<code>{safe_loc}</code>"
                lines.append(f"{title_line}<br>")
                lines.append(loc_line)
            else:
                lines.append(title_line)
            lines.append("</summary>")
            lines.append("")
            lines.append(_fix_nested_fences(item["comment"]))
            if item.get("code_snippet"):
                fence = _safe_code_fence(item["code_snippet"])
                lines.append("")
                lines.append(fence)
                lines.append(item["code_snippet"])
                lines.append(fence)
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # Checklist
    checklist = summary_data.get("checklist", [])
    if checklist:
        lines.append("---")
        lines.append("")
        lines.append("### Checklist")
        lines.append("")
        valid_labels = set(index_to_label.values())
        for ci in checklist:
            mark = "x" if ci["passed"] else " "
            labels = _resolve_finding_labels(ci, index_to_label, valid_labels)
            if labels:
                # Replace ASCII # with fullwidth ＃ (U+FF03) to prevent GitHub
                # from auto-linking cross-references to repository issues/PRs.
                safe_ref = ", ".join(labels).replace("#", "\uFF03")
                ref = f" \u2014 {safe_ref}"
            else:
                ref = ""
            lines.append(f"- [{mark}] {ci['item']}{ref}")
        lines.append("")

    # NOTE: The summary paragraph is intentionally emitted only once, as the
    # lead paragraph directly under the header (see above). A second labelled
    # "### Summary" section used to repeat ``summary_data["summary"]`` verbatim,
    # which rendered the identical text twice in posted reviews.

    # Severity filter hint — only show when there are findings at multiple
    # severity levels so the hint is actually useful.
    severity_count = sum(1 for s in ("critical", "warning", "suggestion") if by_severity.get(s))
    if severity_count > 1:
        lines.append("")
        lines.append("---")
        if bot_username:
            mention = f"@{bot_username}"
            lines.append(
                f"_To rebase and address feedback, mention me:_ "
                f"`{mention} rebase critical` _(fixes 🔴 only)_, "
                f"`{mention} rebase important` _(fixes 🔴 + 🟡)_, "
                f"_or_ `{mention} rebase --fix` _for all._ "
                f"_(A bare_ `{mention} rebase` _only rebases onto the base branch.)_"
            )
        else:
            lines.append(
                "_To rebase and address feedback, use:_ "
                "`/rebase <url> critical` _(fixes 🔴 only)_, "
                "`/rebase <url> important` _(fixes 🔴 + 🟡)_, "
                "_or_ `/rebase --fix <url>` _for all._ "
                "_(A bare_ `/rebase <url>` _only rebases onto the base branch.)_"
            )

    triage_note = _format_triage_accounting(review_data)
    if triage_note:
        lines.append("")
        lines.append(triage_note)

    return "\n".join(lines)


def _build_review_footer(
    provider_name: str = "", model: str = "", head_sha: str = "",
    duration_seconds: float = 0,
) -> str:
    """Build the review footer with branding, provider, model, HEAD SHA, and duration."""
    from app.pr_footer import build_koan_footer, format_duration
    footer = build_koan_footer(
        action="Automated review by",
        provider_name=provider_name,
        model=model,
    )
    if head_sha:
        footer += f" `HEAD={head_sha[:7]}`"
    if duration_seconds > 0:
        footer += f" `{format_duration(duration_seconds)}`"
    return footer


def _build_stale_head_alert(reviewed_sha: str, live_sha: str) -> str:
    """Return a GitHub IMPORTANT alert when the branch moved during review.

    Compares the SHA the review was performed against (``reviewed_sha`` — the
    same value shown as ``HEAD=<short>`` in the footer) to the PR branch's live
    HEAD (``live_sha``). When they differ, commits were pushed (or force-pushed)
    after the review captured its diff, so the findings may be stale.

    Returns a leading-blank-line alert block suitable for appending to the end
    of the review content, or "" when either SHA is missing or they match (in
    which case the posted comment is byte-identical to today's output).
    """
    if not reviewed_sha or not live_sha or reviewed_sha == live_sha:
        return ""
    return "\n\n" + build_alert(
        "IMPORTANT",
        "**The branch moved during review.** This review was performed "
        f"against `HEAD={reviewed_sha[:7]}`, but the PR branch now points at "
        f"`{live_sha[:7]}`. Commits pushed after the review started are not "
        "reflected below — re-run `/review` to cover them.",
    )


def _extract_comment_ref(raw: str) -> Optional[dict]:
    """Parse {id, html_url} from a GitHub comment API JSON response.

    Returns None when the payload is missing, unparseable, or lacks an id, so
    a genuine posting success is never downgraded to a failure by a parse
    hiccup or a stdout format we didn't expect.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or obj.get("id") is None:
        return None
    return {"id": obj["id"], "html_url": obj.get("html_url", "")}


def _post_review_comment(
    owner: str, repo: str, pr_number: str, review_text: str,
    existing_comment: Optional[dict] = None,
    commit_shas: Optional[List[str]] = None,
    provider_name: str = "",
    model: str = "",
    duration_seconds: float = 0,
    live_head_sha: str = "",
    coverage_note: str = "",
) -> Tuple[bool, str, Optional[dict]]:
    """Post (or update) the review as a comment on the PR.

    Prepends ``SUMMARY_TAG`` so future runs can locate the comment via
    ``find_bot_comment``.  When ``existing_comment`` is provided the
    comment is updated via PATCH instead of creating a new one.

    When ``commit_shas`` is provided, embeds them in the body so the
    incremental-review check can skip already-reviewed commits.  When
    absent, preserves any COMMIT_IDS block from ``existing_comment`` so
    a re-review without SHA info doesn't clobber prior state.

    When ``live_head_sha`` is provided and differs from the reviewed tip
    (``commit_shas[-1]``), a stale-HEAD IMPORTANT alert is appended at the
    end of the review content (the branch moved during review). Empty
    ``live_head_sha`` (the default) leaves the body byte-identical.

    When ``coverage_note`` is non-empty, it is prepended to the review body
    *before* the GitHub-length truncation so the partial-review warning is
    never the part that gets cut.

    Returns (True, "", ref) on success, (False, error_detail, None) on
    failure, where ``ref`` is the posted comment's ``{id, html_url}`` (or
    ``None`` when the GitHub response could not be parsed).
    """
    # Surface partial-coverage warning at the very top, before truncation.
    if coverage_note:
        review_text = f"{coverage_note.rstrip()}\n\n{review_text}"

    # Truncate if too long for GitHub (max ~65536 chars)
    max_len = 60000
    if len(review_text) > max_len:
        review_text = review_text[:max_len] + "\n\n_(Review truncated)_"

    head_sha = commit_shas[-1] if commit_shas else ""
    footer = _build_review_footer(
        provider_name, model, head_sha=head_sha,
        duration_seconds=duration_seconds,
    )

    # Stale-HEAD alert: appended after truncation so it is never dropped.
    stale_alert = _build_stale_head_alert(head_sha, live_head_sha)

    # If body already starts with a ## heading, don't add another
    if review_text.startswith("## "):
        body = f"{SUMMARY_TAG}\n{review_text}{stale_alert}\n\n---\n{footer}"
    else:
        body = f"{SUMMARY_TAG}\n## Code Review\n\n{review_text}{stale_alert}\n\n---\n{footer}"

    # Embed commit SHAs in a single hidden HTML comment (fully invisible).
    if commit_shas:
        body = replace_commit_block(body, commit_shas)
    elif existing_comment:
        prior = extract_commit_shas(existing_comment.get("body", ""))
        if prior:
            body = replace_commit_block(body, prior)

    sanitized = sanitize_github_comment(body)
    if existing_comment:
        comment_id = existing_comment["id"]
        try:
            raw = run_gh(
                "api",
                f"repos/{owner}/{repo}/issues/comments/{comment_id}",
                "-X", "PATCH",
                "-f", f"body={sanitized}",
            )
            return True, "", _extract_comment_ref(raw)
        except Exception as e:
            # PATCH can fail with 403 when the existing comment belongs to a
            # different bot account (review bot was switched). Fall back to
            # posting a fresh comment so the review still lands.
            print(
                f"[review_runner] PATCH of comment {comment_id} failed "
                f"({e}); posting a new comment instead",
                file=sys.stderr,
            )

    try:
        # POST via the issues-comments API (not `gh pr comment`) so a
        # structured {id, html_url} JSON comes back for the review result.
        raw = run_gh(
            "api",
            f"repos/{owner}/{repo}/issues/{pr_number}/comments",
            "-X", "POST",
            "-f", f"body={sanitized}",
        )
        return True, "", _extract_comment_ref(raw)
    except Exception as e:
        print(f"[review_runner] failed to post comment: {e}", file=sys.stderr)
        return False, str(e), None


def _post_no_changes_notice(
    owner: str, repo: str, pr_number: str, head_sha: str = "",
) -> bool:
    """Post a blue ``> [!NOTE]`` telling the requester the review was skipped.

    Fired when the incremental-review check finds no new commits since the
    last review. Best-effort: a failed post only logs and returns False so
    the skip still succeeds.
    """
    tip = f" (`{head_sha[:7]}`)" if head_sha else ""
    body = build_alert(
        "NOTE",
        f"No code changes since the last review{tip} — skipping. "
        "Push new commits or re-request a review to run again.",
    )
    try:
        run_gh(
            "api",
            f"repos/{owner}/{repo}/issues/{pr_number}/comments",
            "-X", "POST",
            "-f", f"body={sanitize_github_comment(body)}",
        )
        return True
    except Exception as e:
        print(
            f"[review_runner] failed to post no-changes notice: {e}",
            file=sys.stderr,
        )
        return False


def _append_error_section_to_review(
    owner: str,
    repo: str,
    pr_number: str,
    *,
    review_body: str,
    error_section: str,
    bot_username: str = "",
    commit_shas: Optional[List[str]] = None,
    provider_name: str = "",
    model: str = "",
    duration_seconds: float = 0,
    coverage_note: str = "",
) -> bool:
    """Append the silent-failure-hunter section to the posted review comment.

    The core review is posted *before* the enrichment passes run (so a slow or
    failing enrichment pass can never cost the review). This re-locates that
    comment via ``SUMMARY_TAG`` and rewrites it in place with the extra section
    appended. Best-effort: on any failure the core review still stands and only
    the extra section is lost.

    ``prefer_newest=True`` re-locates the *most recent* marked comment: with
    ``review_history.preserve_previous`` the superseded prior review is left
    intact and still carries ``SUMMARY_TAG``, so the (default) first-match
    lookup would append onto the old comment. The freshly-posted review always
    has the highest comment id, so the newest match is the correct target.

    ``coverage_note``, when passed, is forwarded to ``_post_review_comment``
    so it is re-prepended here too. This rebuild replaces the whole comment
    body (``combined``), so without it the ``⚠️ Partial review`` warning that
    the initial post prepended would be silently dropped by this edit.

    Returns True when the comment was updated.
    """
    located = find_bot_comment(
        owner, repo, pr_number, SUMMARY_TAG, bot_username=bot_username,
        prefer_newest=True,
    )
    if not located:
        print(
            "[review_runner] could not re-locate review comment to append "
            "silent-failure-hunter section; leaving core review as-is",
            file=sys.stderr,
        )
        return False
    combined = review_body + "\n\n---\n\n" + error_section
    updated, err, _ = _post_review_comment(
        owner, repo, pr_number, combined, located,
        commit_shas=commit_shas,
        provider_name=provider_name,
        model=model,
        duration_seconds=duration_seconds,
        coverage_note=coverage_note,
    )
    if not updated:
        print(
            f"[review_runner] failed to append silent-failure-hunter "
            f"section: {err}",
            file=sys.stderr,
        )
    return updated


def _collapse_old_review(
    owner: str, repo: str, comment: dict,
) -> None:
    """Replace an old review comment body with a short pointer to the new one.

    Called before posting a fresh review on re-review so the PR timeline
    stays tidy. Failures are logged but never block the new review from
    being posted.
    """
    comment_id = comment.get("id")
    if not comment_id:
        return
    collapsed = "~~Previous review~~ — superseded by a newer review below.\n"
    try:
        run_gh(
            "api",
            f"repos/{owner}/{repo}/issues/comments/{comment_id}",
            "-X", "PATCH",
            "-f", f"body={collapsed}",
        )
    except Exception as e:
        print(
            f"[review_runner] failed to collapse old review comment "
            f"{comment_id}: {e}",
            file=sys.stderr,
        )


def _post_comment_replies(
    owner: str,
    repo: str,
    pr_number: str,
    replies: list,
    repliable_comments: list,
) -> list:
    """Post individual replies to PR comments.

    For review_comment types, uses the pull request review comment reply API.
    For issue_comment types, posts a new issue comment quoting the original.

    Returns list of {comment_id, action} dicts for successfully posted replies.
    """
    if not replies:
        return []

    full_repo = f"{owner}/{repo}"
    comment_map = {c["id"]: c for c in repliable_comments}
    posted = []

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

        # Surface clarification asks as IMPORTANT callouts so maintainers
        # scanning many bot replies can spot ones that need human input.
        # GitHub has no native QUESTION type; IMPORTANT is the closest fit.
        if reply_item.get("action") == "needs_clarification":
            reply_text = build_alert("IMPORTANT", reply_text)

        try:
            if original["type"] == "review_comment":
                safe_reply = sanitize_github_comment(reply_text)
                run_gh(
                    "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
                    "-X", "POST",
                    "-f", f"body={safe_reply}",
                    "-F", f"in_reply_to={comment_id}",
                )
            else:
                user = original.get("user", "someone")
                quote_line = original["body"].split("\n")[0]
                if len(quote_line) > 100:
                    quote_line = quote_line[:100] + "..."
                body = sanitize_github_comment(f"> @{user}: {quote_line}\n\n{reply_text}")
                run_gh(
                    "pr", "comment", pr_number,
                    "--repo", full_repo,
                    "--body", body,
                )
            posted.append({
                "comment_id": comment_id,
                "action": reply_item.get("action", "acknowledged"),
            })
        except Exception as e:
            print(
                f"[review_runner] failed to post reply to comment {comment_id}: {e}",
                file=sys.stderr,
            )

    return posted


def _format_inline_finding_body(item: dict) -> str:
    """Render a single finding as a flat (non-collapsible) inline comment body.

    Mirrors the collapsible summary entry: severity marker + level label +
    title header, then the full comment, then the code snippet (if any).
    """
    sev = item.get("severity", "suggestion")
    if sev not in _SEVERITY_EMOJI:
        sev = "suggestion"
    emoji = _SEVERITY_EMOJI[sev]
    heading = _SEVERITY_HEADING[sev]
    title = item.get("title", "").strip() or "Finding"

    lines = [f"{emoji} {heading}: {title}", ""]
    comment = item.get("comment", "")
    if comment:
        lines.append(_fix_nested_fences(comment))
    snippet = item.get("code_snippet")
    if snippet:
        fence = _safe_code_fence(snippet)
        lines.append("")
        lines.append(fence)
        lines.append(snippet)
        lines.append(fence)
    return "\n".join(lines).strip()


def _fetch_existing_inline_anchors(owner: str, repo: str, pr_number: str) -> set:
    """Return {(path, line, first_body_line)} of existing PR inline comments.

    Used to make re-runs idempotent: a finding whose anchor + first body line
    already exists is skipped instead of posting a duplicate. Best-effort —
    returns an empty set on any failure (treats every finding as new).
    """
    try:
        raw = run_gh(
            "api", f"repos/{owner}/{repo}/pulls/{pr_number}/comments",
            "--paginate",
        )
        data = json.loads(raw) if raw else []
    except Exception as e:
        log("review", f"Could not fetch existing inline comments on PR #{pr_number}: {e}")
        return set()

    anchors = set()
    for c in data if isinstance(data, list) else []:
        path = c.get("path")
        line = c.get("line") or c.get("original_line")
        body = (c.get("body") or "").strip()
        first_line = body.split("\n", 1)[0] if body else ""
        if path and line:
            anchors.add((path, int(line), first_line))
    return anchors


def _post_inline_finding_comments(
    owner: str,
    repo: str,
    pr_number: str,
    comments: list,
    head_sha: str,
    max_comments: int,
) -> tuple:
    """Post each resolvable finding as a new inline PR review comment.

    Additive to the main summary comment. Best-effort: a finding whose line
    is not part of the diff (GitHub 422) is skipped without aborting the rest.
    Re-run idempotent: findings already anchored on the PR are skipped.
    Returns (posted, attempted) where attempted counts the new, resolvable
    findings we tried to POST (skipped/duplicate findings are not counted).
    """
    if not comments or not head_sha or max_comments <= 0:
        return (0, 0)

    full_repo = f"{owner}/{repo}"
    existing = _fetch_existing_inline_anchors(owner, repo, pr_number)
    posted = 0
    attempted = 0
    for item in comments:
        if posted >= max_comments:
            break
        line_start = item.get("line_start") or 0
        if line_start <= 0 or not item.get("file"):
            continue
        line = item.get("line_end") or line_start
        body = sanitize_github_comment(_format_inline_finding_body(item))
        first_line = body.split("\n", 1)[0] if body else ""
        if (item["file"], int(line), first_line) in existing:
            continue
        attempted += 1
        args = [
            "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
            "-X", "POST",
            "-f", f"body={body}",
            "-f", f"commit_id={head_sha}",
            "-f", f"path={item['file']}",
            "-F", f"line={line}",
            "-f", "side=RIGHT",
        ]
        # Anchor multi-line findings to their full range instead of collapsing
        # to a single line at line_end.
        if line_start < line:
            args += ["-F", f"start_line={line_start}", "-f", "start_side=RIGHT"]
        try:
            run_gh(*args)
            posted += 1
        except Exception as e:
            log(
                "review",
                f"Failed to post inline comment on {item.get('file')}:{line} "
                f"on PR #{pr_number}: {e}",
            )
    return (posted, attempted)


def _maybe_post_inline_comments(
    owner: str, repo: str, pr_number: str,
    review_data: Optional[dict], head_sha: str,
) -> tuple:
    """Config-gated inline posting of structured findings (additive).

    Returns (posted, attempted) — see _post_inline_finding_comments.
    """
    cfg = get_review_inline_comments_config()
    if not cfg["enabled"]:
        return (0, 0)
    if not isinstance(review_data, dict):
        return (0, 0)
    findings = review_data.get("file_comments") or []
    if not findings:
        return (0, 0)
    return _post_inline_finding_comments(
        owner, repo, pr_number, findings, head_sha, cfg["max_comments"],
    )


def _patch_comment_body(
    owner: str, repo: str, comment_id: int, body: str,
) -> bool:
    """PATCH a GitHub issue comment body. Returns True on success."""
    try:
        run_gh(
            "api",
            f"repos/{owner}/{repo}/issues/comments/{comment_id}",
            "-X", "PATCH",
            "-f", f"body={body}",
        )
        return True
    except Exception as e:
        print(f"[review_runner] failed to patch comment {comment_id}: {e}", file=sys.stderr)
        return False


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
        from app.pr_footer import strip_legacy_footers
        plan_body = strip_legacy_footers(plan_body)
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


def _fetch_pr_commit_shas(owner: str, repo: str, pr_number: str) -> List[str]:
    """Return the list of full commit SHAs for a PR (oldest first).

    Returns an empty list on any error so callers can treat absence as
    "no prior state" rather than crashing.
    """
    try:
        raw = run_gh(
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/commits",
            "--paginate",
            "--jq", r".[].sha",
        )
        if not raw.strip():
            return []
        return [line.strip() for line in raw.strip().splitlines() if line.strip()]
    except RuntimeError:
        return []


def _fetch_pr_head_oid(owner: str, repo: str, pr_number: str) -> str:
    """Return the PR branch's current HEAD commit OID (full SHA), or "" on error.

    Unlike ``_fetch_pr_commit_shas`` (which pages the commits list and can
    truncate at GitHub's 250-commit cap), ``headRefOid`` always reflects the
    true branch tip — including after a force-push. Best-effort: any failure
    yields "" so callers treat it as "unknown" and skip the staleness check.

    Catches ``Exception`` deliberately: this call sits *after* the (expensive)
    provider analysis and just *before* posting, so a transient ``gh`` failure
    (``run_gh`` re-raises ``OSError`` / ``subprocess.TimeoutExpired`` after
    exhausting retries — neither a ``RuntimeError``) must never propagate and
    discard an otherwise-complete review.
    """
    try:
        return run_gh(
            "pr", "view", pr_number,
            "--repo", f"{owner}/{repo}",
            "--json", "headRefOid",
            "--jq", ".headRefOid",
        ).strip()
    except Exception as e:
        log("review", f"Could not read live HEAD for PR #{pr_number}: {e}")
        return ""


# GitHub's compare endpoint returns at most this many entries in ``files`` and
# does NOT paginate them (unlike commits). A response at the cap is
# indistinguishable from a truncated one, so we treat it as undetermined.
# https://docs.github.com/en/rest/commits/commits#compare-two-commits
_COMPARE_FILES_CAP = 300


def _compare_changed_files(
    owner: str, repo: str, base: str, head: str,
) -> Optional[set]:
    """Return the set of files changed between ``base`` and ``head`` (or None).

    Used by the spec-010 re-review freeze to determine which files the commits
    touched since the prior review. Best-effort and fail-open: any error, a
    missing base/head, an *undetermined* changeset (empty/blank response), or a
    *truncated* file list yields ``None`` so the caller *skips* the freeze rather
    than freezing against an unknown or under-populated changeset (FR-003
    fail-open, D3). Freezing against too few files would silently suppress
    first-time findings on genuinely-changed code — the inverse of the intent.
    """
    if not base or not head or base == head:
        return None
    try:
        raw = run_gh(
            "api",
            f"repos/{owner}/{repo}/compare/{base}...{head}",
            "--jq", r"[.files[].filename]",
        )
        if not raw.strip():
            # A genuine zero-file diff serializes to "[]" (truthy). A blank
            # response is an ambiguous/error condition — return None (skip the
            # freeze), never set() (which would read as "nothing changed" and
            # drop every first-time finding).
            return None
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return None
        if len(parsed) >= _COMPARE_FILES_CAP:
            # File list is at GitHub's non-paginated cap: assume truncated and
            # skip the freeze rather than freeze against a partial changeset.
            log("review",
                f"changed-files compare at cap ({len(parsed)} files, "
                f"{base}...{head}); skipping freeze")
            return None
        return {str(f) for f in parsed if f}
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        log("review", f"changed-files compare failed ({base}...{head}): {exc}")
        return None


def _merge_base_sha(owner: str, repo: str, base_ref: str, head_sha: str) -> str:
    """Return the merge-base commit SHA of ``base_ref`` and ``head_sha`` (or "").

    Part of the spec-010 reuse key (FR-001, D2): base-branch movement changes the
    merge base even when the PR head is unchanged, which must defeat reuse.
    Best-effort — any failure yields "" so reuse simply does not fire (safe).
    """
    if not base_ref or not head_sha:
        return ""
    try:
        raw = run_gh(
            "api",
            f"repos/{owner}/{repo}/compare/{base_ref}...{head_sha}",
            "--jq", ".merge_base_commit.sha",
        )
        return raw.strip()
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        log("review", f"merge-base lookup failed ({base_ref}...{head_sha}): {exc}")
        return ""


def _fetch_pr_state_and_labels(
    owner: str, repo: str, pr_number: str,
) -> tuple[str, list[str]]:
    """Return (state, label_names). Empty state / [] on error."""
    try:
        raw = run_gh(
            "pr", "view", pr_number,
            "--repo", f"{owner}/{repo}",
            "--json", "state,labels",
            "--jq", "{state: .state, labels: [.labels[].name]}",
        ).strip()
        data = json.loads(raw) if raw else {}
        state = str(data.get("state") or "").strip().upper()
        labels = data.get("labels") or []
        if not isinstance(labels, list):
            labels = []
        return state, [str(x) for x in labels if x is not None]
    except (RuntimeError, json.JSONDecodeError, TypeError) as e:
        log("review", f"Could not check PR state/labels for #{pr_number}: {e}")
        return "", []


def _fetch_pr_state(owner: str, repo: str, pr_number: str) -> str:
    """Back-compat wrapper — state only."""
    state, _ = _fetch_pr_state_and_labels(owner, repo, pr_number)
    return state


def _is_review_requested(owner: str, repo: str, pr_number: str, bot_username: str) -> bool:
    """Check if the bot has a pending review request on this PR.

    When a user clicks "Refresh" on the Reviewers panel, GitHub re-adds
    the bot to the requested_reviewers list.  Detecting this lets us
    bypass the incremental-review SHA check and honour the explicit
    re-request.
    """
    if not bot_username:
        return False
    try:
        raw = run_gh(
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
            "--jq", "[.users[].login, .teams[].slug] | .[]",
        )
        reviewers = [r.strip().lower() for r in raw.strip().splitlines() if r.strip()]
        return bot_username.lower() in reviewers
    except RuntimeError as e:
        log("review", f"Failed to check requested reviewers on PR #{pr_number}: {e}")
        return False


def _build_verdict_body(
    approve: bool,
    review_data: Optional[dict],
    body_enabled: bool = True,
    include_blockers: bool = True,
) -> str:
    """Build body text for a review verdict, graded by severity.

    The body is wrapped in a native GitHub alert whose color grades the
    outcome at a glance (see specs/components/comment-formatting.md):

    - ``> [!TIP]`` (green) — approved / merge-ready.
    - ``> [!WARNING]`` (yellow) — blocked, but the only blockers are
      ``warning``-level.
    - ``> [!CAUTION]`` (red) — blocked with at least one ``critical`` finding.

    When *body_enabled* is False, returns ``""`` so the verdict is submitted
    with an empty body (the APPROVE / REQUEST_CHANGES state still shows in
    the Reviewers panel).

    When *include_blockers* is True and the verdict is REQUEST_CHANGES,
    appends a concise bullet list of critical + warning finding titles
    extracted from the structured review data.

    Defensive backstop only: the ``approve`` flag is normally kept consistent
    with the categorized finding severities upstream (schema validation rejects
    a contradictory verdict before posting, and post-reflection reconciliation
    derives it from the final findings). This builder runs *after* the review
    comment is posted, so if the invariant is ever broken it degrades — logs
    the mismatch and returns ``""`` — rather than raising and aborting the run
    past an irreversible side effect.
    """
    comments = (
        review_data.get("file_comments") or []
        if isinstance(review_data, dict)
        else []
    )
    blockers = [
        c for c in comments
        if isinstance(c, dict) and c.get("severity") in ("critical", "warning")
    ]
    if not approve and not blockers:
        log("review", "Verdict invariant broken: REQUEST_CHANGES verdict has no "
            "critical/warning finding; submitting verdict with an empty body "
            "instead of raising after the review comment was posted.")
        return ""
    if approve and blockers:
        log("review", "Verdict invariant broken: APPROVE verdict contradicts "
            "critical/warning findings; submitting verdict with an empty body "
            "instead of raising after the review comment was posted.")
        return ""

    if not body_enabled:
        return ""

    if approve:
        return build_alert("TIP", "No blocking issues found — ready to merge.")

    has_critical = any(c.get("severity") == "critical" for c in blockers)
    if has_critical:
        kind, headline = "CAUTION", "Critical issues found."
    else:
        kind, headline = "WARNING", "Important issues found."

    if not include_blockers:
        return build_alert(kind, headline)

    blocker_titles = [
        c["title"]
        for c in blockers
        if c.get("title")
    ]

    text = "\n".join([headline, "", *(f"- {title}" for title in blocker_titles)])
    return build_alert(kind, text)


def _resolve_verdict_config(project_name: Optional[str] = None) -> dict:
    """Merge global review_verdict config with project-level overrides."""
    cfg = get_review_verdict_config()
    if project_name:
        try:
            import os
            from app.projects_config import load_projects_config, get_project_review_verdict
            koan_root = os.environ.get("KOAN_ROOT", "")
            if koan_root:
                projects_cfg = load_projects_config(koan_root)
                if projects_cfg:
                    overrides = get_project_review_verdict(projects_cfg, project_name)
                    cfg.update(overrides)
        except Exception as exc:
            log("review", f"Failed to load project review_verdict overrides: {exc}")
            cfg["approved"] = False
    return cfg


def _resolve_history_config(project_name: Optional[str] = None) -> dict:
    """Merge global review_history config with project-level overrides.

    On a project-config load error, returns ``cfg`` unchanged — i.e. the
    already-loaded global value is preserved, not forced to False. This is
    intentional: the global config.yaml loaded fine, so only the per-project
    override failed to apply. (This diverges from _resolve_verdict_config,
    which force-resets on error because an approved verdict is unsafe if
    uncertain.)
    """
    cfg = get_review_history_config()
    if project_name:
        try:
            import os
            from app.projects_config import (
                load_projects_config, get_project_review_history,
            )
            koan_root = os.environ.get("KOAN_ROOT", "")
            if koan_root:
                projects_cfg = load_projects_config(koan_root)
                if projects_cfg:
                    overrides = get_project_review_history(projects_cfg, project_name)
                    cfg.update(overrides)
        except Exception as exc:
            log("review", f"Failed to load project review_history overrides: {exc}")
    return cfg


def _is_self_review_error(error: Exception) -> bool:
    """True when a verdict POST failed because the bot authored the PR.

    GitHub returns HTTP 422 with a message like "Can not approve your own
    pull request" (and the equivalent for request-changes). Matching the
    422 status plus "own pull request" avoids treating unrelated 422s
    (e.g. invalid commit_id, no commits between base and head) as
    self-reviews.
    """
    msg = str(error).lower()
    return "422" in msg and "own pull request" in msg


def _submit_review_verdict(
    owner: str, repo: str, pr_number: str,
    approve: bool, head_sha: str,
    body: Optional[str] = None,
) -> bool:
    """Submit a formal PR review verdict (APPROVE or REQUEST_CHANGES).

    Uses the GitHub Pull Request Reviews API so the bot's decision
    is reflected in the Reviewers panel (green check / red X).

    The ``commit_id`` field anchors the review to a specific commit so
    GitHub knows what code state was reviewed.

    Returns True on success, False on error (non-fatal — the comment
    review was already posted).
    """
    event = "APPROVE" if approve else "REQUEST_CHANGES"
    review_body = body if body is not None else (
        "No blocking issues found." if approve
        else "Blocking issues found — see the review comment above."
    )
    reviews_path = f"repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    try:
        run_gh(
            "api", reviews_path,
            "-X", "POST",
            "-f", f"event={event}",
            "-f", f"body={review_body}",
            "-f", f"commit_id={head_sha}",
        )
        log("review", f"Submitted {event} verdict on PR #{pr_number}")
        return True
    except RuntimeError as e:
        # GitHub forbids APPROVE / REQUEST_CHANGES on a PR you authored
        # (HTTP 422). When the bot reviews its own PR, fall back to a COMMENT
        # review so the verdict body still lands in the Reviewers panel
        # instead of being lost to a misleading "failed to submit" error.
        if _is_self_review_error(e):
            try:
                run_gh(
                    "api", reviews_path,
                    "-X", "POST",
                    "-f", "event=COMMENT",
                    "-f", f"body={review_body}",
                    "-f", f"commit_id={head_sha}",
                )
                log("review", f"Posted {event} verdict as COMMENT on own PR #{pr_number}")
                return True
            except RuntimeError as e2:
                log("review", f"Failed to submit {event} verdict on PR #{pr_number}: {e2}")
                return False
        log("review", f"Failed to submit {event} verdict on PR #{pr_number}: {e}")
        return False


def _apply_review_diff_filters(
    context: dict, *, label: str = "",
) -> Tuple[dict, list]:
    """Filter a PR diff for review and report what was dropped.

    Applies the configured ``review_ignore`` glob/regex filters, then
    content-aware triage of trivial file changes, returning the (possibly
    reduced) context and the list of triaged files. *label* prefixes the
    diagnostic output so callers can distinguish e.g. private-gate runs.
    """
    from app.config import get_review_ignore_config, get_review_triage_config
    from app.diff_triage import triage_diff_files
    from app.utils import filter_diff_by_ignore

    review_ignore = get_review_ignore_config()
    glob_pats = review_ignore.get("glob", [])
    regex_pats = review_ignore.get("regex", [])
    if glob_pats or regex_pats:
        filtered_diff, skipped = filter_diff_by_ignore(
            context.get("diff", ""), glob_pats, regex_pats,
        )
        if skipped:
            print(
                f"[review_runner] {label}ignoring {len(skipped)} "
                f"file(s): {skipped}",
                file=sys.stderr,
            )
        context = {**context, "diff": filtered_diff}

    triage_config = get_review_triage_config()
    triaged_diff, triaged_files = triage_diff_files(
        context.get("diff", ""), triage_config,
    )
    if triaged_files:
        triage_summary = ", ".join(
            f"{t.path} ({t.reason})" for t in triaged_files
        )
        log(
            "review",
            f"{label}triaged {len(triaged_files)} trivial file(s): "
            f"{triage_summary}",
        )
        context = {**context, "diff": triaged_diff}

    return context, triaged_files


def _run_review_analysis(
    prompt: str,
    project_path: str,
    diff: str,
    skill_dir: Optional[Path] = None,
    project_name: Optional[str] = None,
) -> Tuple[Optional[dict], str, Optional[str]]:
    """Run the provider review and parse it into structured review data.

    Invokes the provider once, then retries once if the first response fails
    to parse or breaks a review rule — the retry guidance is tailored to the
    failure mode (rule violations are listed when the output was valid JSON,
    otherwise a JSON-only instruction), so the model actually learns what to
    fix. Then runs the reflection pass over any findings.

    Returns ``(review_data, raw_output, error)``:
      - ``review_data``: parsed + reflected review dict, or ``None`` when the
        provider produced output that could not be parsed/validated.
      - ``raw_output``: the first provider response ("" when the provider
        produced nothing); callers may use it for a regex fallback.
      - ``error``: short provider error string, set only when the provider
        produced no output at all.
    """
    pname = project_name or ""
    raw_output, error = _run_claude_review(prompt, project_path, project_name=pname)
    if not raw_output:
        return None, "", error

    review_data, validation_errors = _parse_and_validate_review(raw_output)
    if review_data is None:
        retry_prompt = _build_review_retry_prompt(prompt, validation_errors)
        retry_output, _ = _run_claude_review(retry_prompt, project_path, project_name=pname)
        if retry_output:
            review_data, _ = _parse_and_validate_review(retry_output)

    if review_data is not None and review_data.get("file_comments"):
        from app.cli_provider import resolve_role_provider
        from app.config import get_model_config, get_review_reflect_config
        # Resolve the reflect model against the review_mode PROVIDER's section,
        # since the reflect pass runs on that same binary (see _run_claude_review).
        review_provider = resolve_role_provider("review_mode", pname)
        models = get_model_config(
            pname,
            role_providers={
                "reflect": review_provider.name,
                "lightweight": review_provider.name,
            },
        )
        reflect_cfg = get_review_reflect_config()
        reflect_model = models.get("reflect") or models.get("lightweight")
        reflect_threshold = reflect_cfg.get("threshold", 5)
        calibration_hints = _load_calibration_hints(project_name)
        original_findings = review_data["file_comments"]
        reflected_findings, retained_indices = _reflect_findings(
            original_findings,
            diff,
            project_path,
            reflect_model,
            reflect_threshold,
            skill_dir=skill_dir,
            calibration_hints=calibration_hints,
            project_name=pname,
        )
        _reconcile_review_after_reflection(
            review_data, reflected_findings, retained_indices,
        )

    return review_data, raw_output, error


def run_private_review(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
    plan_url: Optional[str] = None,
    project_name: Optional[str] = None,
    comments: bool = False,
    ultra: bool = False,
    force: bool = False,
) -> Tuple[bool, str, Optional[dict], dict]:
    """Run the review analysis pipeline without writing to GitHub.

    This is the backend-only counterpart to :func:`run_review`. It reuses the
    same PR context loading, diff ignore/triage, prompt, JSON parsing, retry,
    and reflection behavior, then returns structured review data to callers.
    It deliberately skips posting comments, replying to threads, submitting
    verdicts, closing PRs, bot-comment triage, and incremental SHA skipping.
    """
    if ultra:
        architecture = True

    if notify_fn is None:
        def notify_fn(_msg):
            return None

    try:
        owner, repo = resolve_pr_location(owner, repo, pr_number, project_path)
    except RuntimeError as e:
        return False, str(e), None, {}

    if not force:
        pr_state = _fetch_pr_state(owner, repo, pr_number)
        if pr_state in ("MERGED", "CLOSED"):
            return (
                True,
                f"PR #{pr_number} is {pr_state.lower()} — skipping private review.",
                None,
                {},
            )

    full_repo = f"{owner}/{repo}"
    notify_fn(f"Privately reviewing PR #{pr_number} ({full_repo})...")

    try:
        context = fetch_pr_context(
            owner, repo, pr_number, project_path,
            max_diff_chars=get_review_max_diff_chars(),
        )
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}", None, {}

    context, triaged_files = _apply_review_diff_filters(
        context, label="Private review ",
    )

    if not context.get("diff"):
        if context.get("diff_error"):
            return False, f"PR #{pr_number} diff unavailable — cannot review.", None, context
        return True, f"PR #{pr_number} has no diff — nothing to review.", None, context

    plan_body = _resolve_plan_body(plan_url, context.get("body", ""))

    prompt, _coverage_note = build_review_prompt(
        context,
        skill_dir=skill_dir,
        architecture=architecture,
        comments=comments,
        repliable_comments=[],
        plan_body=plan_body or None,
        project_path=project_path,
        triaged_files=triaged_files,
        project_name=project_name or "",
    )

    notify_fn(f"Analyzing code changes on `{context['branch']}` privately...")
    review_data, raw_output, error = _run_review_analysis(
        prompt, project_path, context.get("diff", ""), skill_dir=skill_dir,
        project_name=project_name,
    )
    if not raw_output:
        detail = f" ({error})" if error else ""
        return False, f"Provider review failed for PR #{pr_number}{detail}.", None, context

    if review_data is None:
        return False, f"Private review output for PR #{pr_number} was unparseable.", None, context

    # Accuracy gate: reconcile against the reviewed HEAD (same as run_review).
    # run_private_review has no commit list; read the live head oid directly.
    _priv_sha = _fetch_pr_head_oid(owner, repo, pr_number)
    if _priv_sha:
        _apply_review_accuracy_gate(
            review_data, owner=owner, repo=repo, sha=_priv_sha,
            project_path=project_path, project_name=project_name or "",
            pr_number=pr_number,
        )

    return True, f"Private review completed for PR #{pr_number}.", review_data, context


def _fire_post_review(
    *,
    instance_dir: str,
    project_name: str,
    project_path: str,
    owner: str,
    repo: str,
    pr_number: str,
    pr_url: str,
    review_summary: dict,
    review_data: dict,
    verdict_submitted: bool,
    closed: bool,
    ultra: bool,
) -> None:
    """Fire the post_review lifecycle hook. Fire-and-forget.

    Called once from run_review() after the review comment is posted.
    Any failure is logged and swallowed so hook errors never affect the
    review's own return value.

    Review skills run as a subprocess (``python -m app.review_runner``), so
    the daemon's hook registry is not inherited. Initialize here (idempotent)
    before firing so instance-wide and skill-bound handlers actually run.
    """
    try:
        from app.hooks import fire_hook, init_hooks
        # Subprocess entry: registry starts None; init is per-process.
        init_hooks(instance_dir)
        fire_hook(
            "post_review",
            instance_dir=instance_dir,
            project_name=project_name or "",
            project_path=project_path,
            owner=owner,
            repo=repo,
            pr_number=str(pr_number),
            pr_url=pr_url,
            review_summary=dict(review_summary or {}),
            review_data=review_data if isinstance(review_data, dict) else {},
            lgtm=(review_summary or {}).get("lgtm"),
            verdict_submitted=verdict_submitted,
            closed=closed,
            ultra=ultra,
        )
    except Exception as e:  # pragma: no cover - defensive
        print(f"[hooks] post_review hook error: {e}", file=sys.stderr)


def run_review(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    architecture: bool = False,
    plan_url: Optional[str] = None,
    project_name: Optional[str] = None,
    errors: bool = False,
    comments: bool = False,
    ultra: bool = False,
    force: bool = False,
    bot_comments: bool = False,
    pinned_worktree: bool = True,
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
        project_name: Optional project name for injecting project-specific
            learnings into the review prompt.
        errors: If True, run an additional silent-failure-hunter pass to detect
            swallowed exceptions and silent error paths. Auto-triggered when
            the diff contains error-handling patterns.
        comments: If True, use comment-quality review prompt.
        ultra: If True, run the most thorough review possible — combines the
            architecture-focused main pass with the silent-failure-hunter
            (errors) pass. Equivalent to passing architecture=True and
            errors=True; provided as a single semantic switch for the
            /ultrareview skill.
        force: If True, review even if the PR is closed/merged, and
            re-review even when no new commits were pushed (bypasses the
            incremental SHA skip and the prior-review reuse short-circuit).
        bot_comments: If True, run an additional pass to triage inline
            comments from code-review bots and post replies to actionable
            findings.

    Returns:
        (success, summary, review_data) tuple. review_data is the validated
        JSON review dict, or None if JSON parsing failed (fallback was used).
    """
    if ultra:
        architecture = True
        errors = True

    if notify_fn is None:
        from app.messaging_level import progress_notify
        notify_fn = progress_notify(log_category="review")

    # ── Step 0: Resolve actual PR location (cross-owner support) ──────
    try:
        owner, repo = resolve_pr_location(owner, repo, pr_number, project_path)
    except RuntimeError as e:
        return False, str(e), None

    # ── Step 0a: Check PR state + pause label (single gh call) ──────────
    # Skip closed/merged and pause-label PRs unless --force. Labels and state
    # share one gh call so the pause gate adds zero extra round-trips.
    if not force:
        from app.config import get_review_pause_label
        pr_state, pr_labels = _fetch_pr_state_and_labels(owner, repo, pr_number)

        if pr_state in ("MERGED", "CLOSED"):
            msg = (
                f"PR #{pr_number} is {pr_state.lower()} — skipping review. "
                "Use --force to review anyway."
            )
            log("review", msg)
            return True, msg, None

        pause_label = get_review_pause_label()
        if pause_label and pause_label in pr_labels:
            msg = (
                f"⏸ Review skipped\n"
                f'Reason: Pull request contains label "{pause_label}"\n'
                "Remove the label to resume auto-review, or use --force to review anyway."
            )
            log("review", msg)
            notify_fn(msg)
            return True, msg, None

    # ── Step 0b: pin a disposable checkout of the PR head ───────────────
    #
    # Missions are dispatched after git_prep.prepare_project_branch, which
    # checks out the BASE branch and hard-resets it to <remote>/<base>. Without
    # this, `cwd` is the base tip and the reviewed code is not on disk at all --
    # while the prompts tell the model to verify against "the checked-out code".
    #
    # Placed after the closed/merged and pause-label gates so a skipped PR never
    # pays for a checkout.
    if not pinned_worktree:
        # Opt-out for a caller that has ALREADY pinned a checkout of this PR head
        # and passes it as ``project_path``. Pinning again would double-fetch and,
        # worse, could resolve a different SHA than the one the caller recorded.
        return _run_review_body(
            owner, repo, pr_number, project_path, notify_fn, skill_dir,
            architecture, plan_url, project_name, errors, comments, ultra,
            force, bot_comments,
        )

    from app.review_worktree import pinned_review_worktree

    # ``_run_review_body`` runs INSIDE the worktree context, so it is exposed to
    # the same except clause as the pin -- unless we distinguish them. A bug in
    # the review body itself (e.g. a new uncaught RuntimeError during analysis)
    # must propagate as a stack trace pointing at the real defect, never be
    # swallowed and reported as a checkout failure; masking it makes the
    # operator retry instead of fixing. ``entered`` separates the two: it flips
    # only once ``pinned_review_worktree.__enter__`` has succeeded, so an
    # exception raised before that is a pin failure, and anything after (the
    # body, or teardown of a successful pin) propagates unchanged.
    entered = False
    try:
        with pinned_review_worktree(
            owner, repo, pr_number, project_path,
        ) as (review_path, _pinned_head):
            entered = True
            return _run_review_body(
                owner, repo, pr_number, review_path, notify_fn, skill_dir,
                architecture, plan_url, project_name, errors, comments, ultra,
                force, bot_comments,
            )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if entered:
            # The pin succeeded; this exception came from the review body or the
            # teardown of a worktree we successfully entered. Do NOT mask it as
            # a checkout failure -- see the comment above.
            raise
        # The pin itself failed. Deliberately NO fallback to project_path:
        # reviewing the wrong tree and reporting the findings as fact is the
        # defect this exists to remove; a silent fallback reproduces it behind a
        # warning nobody reads. A failed review is visible and retriable, a
        # wrong review is neither.
        msg = f"Could not prepare a clean checkout of PR #{pr_number}: {exc}"
        log("error", msg)
        notify_fn(f"❌ {msg}")
        return False, msg, None


def _run_review_body(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn,
    skill_dir: Optional[Path],
    architecture: bool,
    plan_url: Optional[str],
    project_name: Optional[str],
    errors: bool,
    comments: bool,
    ultra: bool,
    force: bool,
    bot_comments: bool,
) -> Tuple[bool, str, Optional[dict]]:
    """Fetch context, run the review passes and post. See :func:`run_review`.

    Split out so ``run_review`` can hold the pinned worktree open around the
    whole thing; ``project_path`` here is the WORKTREE, not the operator's
    checkout.
    """
    from app.config import get_review_concurrency_config
    concurrency_cfg = get_review_concurrency_config()
    github_workers = concurrency_cfg["github_workers"]
    concurrency_enabled = concurrency_cfg["enabled"]

    full_repo = f"{owner}/{repo}"

    # Resolve bot username to exclude own comments from repliable list
    bot_username = _resolve_bot_username()

    # Step 1: Fetch PR context and repliable comments in parallel
    notify_fn(f"Reviewing PR #{pr_number} ({full_repo})...")
    if concurrency_enabled and github_workers > 1:
        with ThreadPoolExecutor(max_workers=min(2, github_workers)) as pool:
            f_context = pool.submit(
                fetch_pr_context, owner, repo, pr_number, project_path,
                max_diff_chars=get_review_max_diff_chars(),
            )
            f_comments = pool.submit(
                fetch_repliable_comments, owner, repo, pr_number, True, bot_username,
            )
            try:
                context = f_context.result()
            except Exception as e:
                return False, f"Failed to fetch PR context: {e}", None
            repliable_comments = f_comments.result()
    else:
        try:
            context = fetch_pr_context(
                owner, repo, pr_number, project_path,
                max_diff_chars=get_review_max_diff_chars(),
            )
        except Exception as e:
            return False, f"Failed to fetch PR context: {e}", None
        repliable_comments = fetch_repliable_comments(
            owner, repo, pr_number, parallel=False, bot_username=bot_username,
        )

    # Step 1a: Apply review_ignore filters + content triage to the diff.
    context, _triaged_files = _apply_review_diff_filters(context)

    if not context.get("diff"):
        if context.get("diff_error"):
            return (
                False,
                f"PR #{pr_number} diff unavailable — cannot review.",
                None,
            )
        return True, f"PR #{pr_number} has no diff — nothing to review.", None

    # Step 1b: Detect and fetch plan body for alignment checking
    plan_body = _resolve_plan_body(plan_url, context.get("body", ""))

    # Step 1c: Look up any existing bot summary comment (Phase 3).
    # Filter by the current bot's account: a summary left by a *different*
    # bot (e.g. after switching review bots) can't be PATCHed by us — GitHub
    # returns 403 — so we treat only our own comment as the upsert target.
    existing_comment = find_bot_comment(
        owner, repo, pr_number, SUMMARY_TAG, bot_username=bot_username,
    )

    # Step 1d: Fetch current PR commit SHAs (Phase 5 — incremental review)
    current_shas = _fetch_pr_commit_shas(owner, repo, pr_number)

    # Step 1e: Extract previously reviewed SHAs from existing comment (Phase 5)
    prior_shas: List[str] = []
    if existing_comment:
        prior_shas = extract_commit_shas(existing_comment.get("body", ""))

    # Step 1f: Check if the bot has a pending review request (re-request
    # via the "Refresh" button on GitHub's Reviewers panel).  When a
    # re-request is detected, bypass the incremental SHA check so the
    # user's explicit action is honoured even without new commits.
    review_was_requested = _is_review_requested(
        owner, repo, pr_number, bot_username,
    )

    # If all current commits were already reviewed AND this is not an
    # explicit re-request (Reviewers-panel re-request or --force), skip.
    if (
        current_shas
        and prior_shas
        and set(current_shas) == set(prior_shas)
        and not review_was_requested
        and not force
    ):
        bot_triage_cfg = get_review_bot_triage_config()
        bot_triage_enabled = bot_comments or bot_triage_cfg["enabled"]
        if bot_triage_enabled:
            extra_bot_usernames = bot_triage_cfg["bot_usernames"]
            full_repo = f"{owner}/{repo}"
            bot_inline = _fetch_bot_inline_comments(
                full_repo, pr_number, bot_username, extra_bot_usernames,
            )
            if bot_inline:
                notify_fn(f"Triaging {len(bot_inline)} bot comment(s) on PR #{pr_number}...")
                triage_replies = _run_bot_comment_triage(
                    bot_inline, context.get("diff", ""), skill_dir,
                    project_path=project_path,
                    project_name=project_name or "",
                )
                if triage_replies:
                    bot_reply_results = _post_comment_replies(
                        owner, repo, pr_number, triage_replies, bot_inline,
                    )
                    if bot_reply_results:
                        print(
                            f"[review_runner] posted {len(bot_reply_results)} bot triage reply(ies)",
                            file=sys.stderr,
                        )
        # Tell the requester why nothing happened instead of skipping silently.
        _post_no_changes_notice(
            owner, repo, pr_number,
            head_sha=(current_shas[-1] if current_shas else ""),
        )
        return (
            True,
            f"PR #{pr_number} has no new commits since last review — skipping.",
            None,
        )

    # Step 1g: spec-010 reuse short-circuit (US1, FR-001). On the re-analyze path
    # (new commits or an explicit re-request), if the PR head AND base
    # (merge-base) SHA are unchanged since the prior review and the request is
    # equivalent, reproduce the prior review instead of re-deriving it —
    # eliminating run-to-run drift on unchanged code. The signature is also
    # persisted (below) so a future review can make this decision. Fail-open: any
    # unknown SHA or missing prior record falls through to a fresh review (FR-007).
    from app.config import get_review_consistency_config as _get_cc_cfg
    from app.review_reuse import request_signature, should_reuse
    _head_sha = current_shas[-1] if current_shas else ""
    _focus_flags = [
        name for name, on in (
            ("architecture", architecture), ("errors", errors),
            ("comments", comments), ("plan", bool(plan_url)), ("ultra", ultra),
        ) if on
    ]
    # discovery_enabled is part of the request signature (FR-016) so a single-pass
    # review is never reused as a comprehensive one (or vice versa).
    from app.config import get_review_discovery_config as _get_disc_cfg
    _discovery_enabled = _get_disc_cfg(project_name or "")["enabled"]
    _request_signature = request_signature(_focus_flags, _discovery_enabled)
    _consistency_cfg = _get_cc_cfg(project_name or "")
    _base_sha = ""
    if _consistency_cfg["reuse_enabled"] and _head_sha and not force:
        _base_sha = _merge_base_sha(owner, repo, context.get("base") or "", _head_sha)
        if _base_sha:
            _prior_record = _read_prior_review_record(
                str(KOAN_ROOT / "instance"), owner, repo, pr_number,
            )
            if should_reuse(_prior_record, _head_sha, _base_sha, _request_signature):
                msg = (
                    f"PR #{pr_number}: head & base unchanged since the last "
                    "review and the request is equivalent — the prior review "
                    "stands (reproduced, no re-analysis)."
                )
                log("review", msg)
                notify_fn(msg)
                return True, msg, None

    # Track review wall-clock time for footer attribution
    _review_start = time.monotonic()

    # Step 2: Build review prompt. Surface the bot's last structured review (if
    # any) as authoritative prior context so a re-review builds on it.
    prior_review_text = (
        extract_prior_review_body(existing_comment.get("body", ""))
        if existing_comment else None
    )
    prompt, coverage_note = build_review_prompt(
        context, skill_dir=skill_dir, architecture=architecture,
        comments=comments, repliable_comments=repliable_comments,
        plan_body=plan_body or None, project_path=project_path,
        triaged_files=_triaged_files, project_name=project_name or "",
        prior_review=prior_review_text,
    )

    # Resolve provider/model for footer attribution against the review_mode
    # provider (the one the review actually runs on), so the footer reflects the
    # binary/model used rather than the global provider.
    review_provider_name, review_model = _review_attribution(project_name or "")

    # Step 3: Run provider review (read-only)
    notify_fn(f"Analyzing code changes on `{context['branch']}`...")
    review_data, raw_output, error = _run_review_analysis(
        prompt, project_path, context.get("diff", ""), skill_dir=skill_dir,
        project_name=project_name,
    )
    if not raw_output:
        detail = f" ({error})" if error else ""
        return False, f"Provider review failed for PR #{pr_number}{detail}.", None

    # Step 4b: Accuracy gate — reconcile the parsed review against the reviewed
    # HEAD before rendering (kills pre-fix quotes and re-raised, already-fixed
    # findings). Runs before _format_review_as_markdown so both the summary and
    # inline comments render the reconciled findings.
    if review_data is not None and current_shas:
        _apply_review_accuracy_gate(
            review_data, owner=owner, repo=repo, sha=current_shas[-1],
            project_path=project_path, project_name=project_name or "",
            pr_number=pr_number,
        )

    # Step 5: Convert to markdown for posting
    if review_data is not None:
        review_body = _format_review_as_markdown(
            review_data, title=context.get("title", ""),
            bot_username=bot_username,
            owner=owner, repo=repo,
            head_sha=(current_shas[-1] if current_shas else ""),
        )
    else:
        # Fallback: use regex extraction for non-JSON responses
        print(
            "[review_runner] JSON parsing failed, falling back to regex extraction",
            file=sys.stderr,
        )
        review_body = _extract_review_body(raw_output)
        if review_body is None:
            # Guardrail: never post raw model output (narration / JSON) to a PR.
            # Post a short placeholder and alert a human to re-run.
            print(
                "[review_runner] review output unparseable; "
                "posting placeholder notice",
                file=sys.stderr,
            )
            notify_fn(
                f"⚠️ Review for PR #{pr_number}: model output couldn't be "
                "parsed into the structured format; posted a placeholder. "
                "Re-run /review to retry."
            )
            review_body = _UNPARSEABLE_REVIEW_NOTICE

    # Step 6: Post replies to user comments
    reply_results = []
    if review_data and review_data.get("comment_replies") and repliable_comments:
        reply_results = _post_comment_replies(
            owner, repo, pr_number,
            review_data["comment_replies"],
            repliable_comments,
        )
        if reply_results:
            print(
                f"[review_runner] posted {len(reply_results)} reply(ies) to user comments",
                file=sys.stderr,
            )

    full_repo = f"{owner}/{repo}"

    # Step 7 (posted BEFORE the optional enrichment passes): post (or update)
    # the core review comment first, so a slow or failing enrichment pass (bot
    # triage, silent-failure-hunter) can never cost the review that's already
    # in hand. The silent-failure-hunter section, when produced, is appended to
    # this comment via a follow-up edit below (Step 6a).
    #
    # Phase 3 — idempotent upsert. Commit SHAs are embedded in the body upfront
    # to avoid extra API calls. Re-review with new commits: post a FRESH
    # comment instead of PATCHing — GitHub does not notify on edits, so an
    # in-place update is invisible to the reviewer.
    post_target = existing_comment
    new_commits = prior_shas and current_shas and set(current_shas) != set(prior_shas)
    if existing_comment and (new_commits or review_was_requested):
        # Preserve the prior review comment when configured (review_history.
        # preserve_previous). Default (False) collapses the old comment to a
        # short "superseded" pointer so the timeline stays tidy. Either way a
        # fresh comment is posted below, since GitHub does not notify on edits.
        if _resolve_history_config(project_name)["preserve_previous"]:
            log("review", "Preserving previous review comment "
                "(review_history.preserve_previous=true)")
        else:
            _collapse_old_review(owner, repo, existing_comment)
        post_target = None

    # Re-read the branch's live HEAD just before posting so we can flag a
    # review whose diff was captured before the author pushed new commits.
    # Best-effort: "" on any error ⇒ no alert (never blocks the post).
    live_head_sha = _fetch_pr_head_oid(owner, repo, pr_number) if current_shas else ""

    notify_fn(f"Posting review on PR #{pr_number}...")
    _review_duration = time.monotonic() - _review_start
    posted, post_error, comment_ref = _post_review_comment(
        owner, repo, pr_number, review_body, post_target,
        commit_shas=current_shas or None,
        provider_name=review_provider_name,
        model=review_model,
        duration_seconds=_review_duration,
        live_head_sha=live_head_sha,
        coverage_note=coverage_note,
    )

    # Steps 6b + 6a run AFTER the core post and are strictly best-effort: the
    # whole enrichment block is wrapped so that once the review has landed, no
    # enrichment failure (stall, provider error, or an unexpected exception)
    # can flip the run's outcome to failure. run_error_hunter is pre-set so the
    # summary reference below is defined even if the block aborts early.
    run_error_hunter = errors or _should_run_error_hunter(context.get("diff", ""))
    try:
        # Step 6b: Bot comment triage — posts its own independent reply
        # comments and never touched review_body, so running it after the post
        # means a stall here can't delay or lose the review.
        bot_triage_cfg = get_review_bot_triage_config()
        bot_triage_enabled = bot_comments or bot_triage_cfg["enabled"]
        extra_bot_usernames = bot_triage_cfg["bot_usernames"]

        if bot_triage_enabled:
            bot_inline = _fetch_bot_inline_comments(
                full_repo, pr_number, bot_username, extra_bot_usernames,
            )
            if bot_inline:
                notify_fn(f"Triaging {len(bot_inline)} bot comment(s) on PR #{pr_number}...")
                triage_replies = _run_bot_comment_triage(
                    bot_inline, context.get("diff", ""), skill_dir,
                    project_path=project_path,
                    project_name=project_name or "",
                )
                if triage_replies:
                    bot_reply_results = _post_comment_replies(
                        owner, repo, pr_number, triage_replies, bot_inline,
                    )
                    if bot_reply_results:
                        print(
                            f"[review_runner] posted {len(bot_reply_results)} bot triage reply(ies)",
                            file=sys.stderr,
                        )

        # Step 6a: Silent-failure-hunter pass (explicit flag or auto-detected).
        # Its section is appended to the already-posted review comment via an
        # edit. If the core post failed, or the comment can't be re-located, the
        # section is dropped (the core review still stands).
        if run_error_hunter:
            notify_fn(f"Running silent-failure-hunter on PR #{pr_number}...")
            error_section = _run_error_hunter(
                context.get("diff", ""), project_path, skill_dir,
                owner=owner, repo=repo,
                head_sha=(current_shas[-1] if current_shas else ""),
                project_name=project_name or "",
                start_id=(
                    len(review_data.get("file_comments", [])) + 1
                    if isinstance(review_data, dict)
                    else 1
                ),
            )
            if error_section and posted:
                _append_error_section_to_review(
                    owner, repo, pr_number,
                    review_body=review_body,
                    error_section=error_section,
                    bot_username=bot_username,
                    commit_shas=current_shas or None,
                    provider_name=review_provider_name,
                    model=review_model,
                    duration_seconds=_review_duration,
                    coverage_note=coverage_note,
                )
            elif error_section and not posted:
                print(
                    "[review_runner] silent-failure-hunter findings dropped: "
                    "core review comment was not posted",
                    file=sys.stderr,
                )
            else:
                print(
                    "[review_runner] silent-failure-hunter: no findings",
                    file=sys.stderr,
                )
    except Exception as exc:
        # Never let a post-hoc enrichment failure undo an already-posted review.
        # The catch is broad on purpose (a stall or provider error must not flip
        # the run), but surface it through notify_fn too — not stderr alone — so
        # a persistently-broken enrichment pass (e.g. a real defect introduced by
        # a later refactor) is visible rather than silently swallowed on every PR.
        print(
            f"[review_runner] enrichment pass failed after posting review "
            f"on PR #{pr_number}: {exc}",
            file=sys.stderr,
        )
        notify_fn(
            f"Enrichment pass failed after posting review on PR #{pr_number} "
            f"(review still landed): {exc}"
        )

    # Step 7c: Optionally post each finding as an inline PR comment (opt-in).
    # Additive to the summary comment above and independently failable, so an
    # inline-posting error never affects the already-posted summary.
    if posted:
        inline_posted, inline_attempted = _maybe_post_inline_comments(
            owner, repo, pr_number, review_data,
            current_shas[-1] if current_shas else "",
        )
        if inline_posted:
            notify_fn(f"Posted {inline_posted} inline comment(s) on PR #{pr_number}.")
        elif inline_attempted:
            notify_fn(
                f"Inline posting failed: 0 of {inline_attempted} comment(s) "
                f"posted on PR #{pr_number}."
            )

    # Step 7a: Persist structured findings for post-merge outcome tracking
    # Only written after review was successfully posted so we track
    # findings that were actually delivered to the author.
    if posted and review_data is not None:
        _sidecar_head = current_shas[-1] if current_shas else ""
        _sidecar_base = context.get("base", "main")
        if _sidecar_head:
            _write_review_findings_sidecar(
                str(KOAN_ROOT / "instance"),
                owner, repo, pr_number,
                review_data.get("file_comments", []),
                base_ref=_sidecar_base,
                head_sha=_sidecar_head,
                base_sha=_base_sha,
                request_signature=_request_signature,
                project_name=project_name or "",
                review_summary=review_data.get("review_summary") or {},
                review_comment=comment_ref,
            )
        else:
            print(
                "[review_runner] skipping outcome sidecar: no head SHA captured",
                file=sys.stderr,
            )

    # Step 7b: Submit formal review verdict (APPROVE / REQUEST_CHANGES)
    # so the bot's decision shows in GitHub's Reviewers panel.  Only
    # submitted when we have structured data (lgtm field) and the
    # comment was posted successfully.  The commit_id anchors the
    # verdict to the reviewed code state.
    verdict_submitted = False
    review_summary = {}
    if posted and isinstance(review_data, dict):
        review_summary = review_data.get("review_summary") or {}
        lgtm = review_summary.get("lgtm")
        if isinstance(lgtm, bool) and current_shas:
            verdict_cfg = _resolve_verdict_config(project_name)
            if verdict_cfg["approved"]:
                verdict_body = _build_verdict_body(
                    approve=lgtm,
                    review_data=review_data,
                    body_enabled=verdict_cfg["body_enabled"],
                    include_blockers=verdict_cfg["include_blockers"],
                )
                verdict_submitted = _submit_review_verdict(
                    owner, repo, pr_number,
                    approve=lgtm,
                    head_sha=current_shas[-1],
                    body=verdict_body,
                )
            else:
                log("review", f"Verdict submission disabled — skipping on PR #{pr_number}")

    # Step 8: Close the PR if the review decided closure is warranted
    closed = False
    close_reason = ""
    if isinstance(review_data, dict):
        close_decision = review_data.get("close_pr") or {}
        if close_decision.get("close") is True:
            if posted:
                close_reason = (close_decision.get("reason") or "").strip()
                closed = _close_pr_from_review(
                    owner, repo, pr_number, close_reason, notify_fn=notify_fn,
                )
            else:
                print(
                    f"[review_runner] close_pr.close=True observed but review "
                    f"post failed; skipping close on PR #{pr_number}",
                    file=sys.stderr,
                )

    if posted:
        label = "Ultra review" if ultra else "Review"
        summary = f"{label} posted on PR #{pr_number} ({full_repo})."
        if verdict_submitted:
            verdict_label = "APPROVE" if review_summary.get("lgtm") else "REQUEST_CHANGES"
            summary += f" Verdict: {verdict_label}."
        if run_error_hunter:
            summary += " Silent-failure-hunter pass included."
        if reply_results:
            summary += f" Replied to {len(reply_results)} comment(s)."
        if closed:
            summary += f" PR closed: {close_reason or 'no reason provided'}."
        from app.messaging_level import notify_outcome
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
        verb = "Ultra reviewed" if ultra else "Reviewed"
        notify_outcome(f"✅ {verb} {pr_url}", notify_fn)
        _fire_post_review(
            instance_dir=str(KOAN_ROOT / "instance"),
            project_name=project_name or "",
            project_path=project_path,
            owner=owner,
            repo=repo,
            pr_number=str(pr_number),
            pr_url=pr_url,
            review_summary=review_summary,
            review_data=review_data if isinstance(review_data, dict) else {},
            verdict_submitted=verdict_submitted,
            closed=closed,
            ultra=ultra,
        )
        return True, summary, review_data
    else:
        detail = f" Error: {post_error}" if post_error else ""
        from app.messaging_level import notify_outcome
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
        notify_outcome(f"❌ Review failed {pr_url}{detail}", notify_fn)
        return False, f"Review generated but failed to post comment on PR #{pr_number}.{detail}", review_data


def _close_pr_from_review(
    owner: str,
    repo: str,
    pr_number: str,
    reason: str,
    notify_fn=None,
) -> bool:
    """Close a PR after the review decided closure is warranted.

    Runs ``gh pr close --comment ...`` so the explanatory comment and the
    close action are atomic: if close fails (403, rate limit, etc.) no
    misleading "PR Closed" comment is left dangling on an open PR.

    Returns True on success, False on any failure (caller continues either way).
    """
    full_repo = f"{owner}/{repo}"
    reason_text = reason or "Closure recommended by the latest review."
    comment_body = (
        "## PR Closed by Reviewer Recommendation\n\n"
        f"{reason_text}\n\n"
        "See the review above for the full rationale. Reopen the PR with a "
        "comment if this determination is incorrect.\n\n"
        "---\n_Automated by Kōan_"
    )
    try:
        run_gh(
            "pr", "close", pr_number,
            "--repo", full_repo,
            "--comment", sanitize_github_comment(comment_body),
        )
    except Exception as e:
        print(f"[review_runner] PR close failed: {e}", file=sys.stderr)
        return False

    if notify_fn:
        msg = f"PR #{pr_number} ({full_repo}) closed by reviewer recommendation."
        if reason:
            msg += f" Reason: {reason}"
        notify_fn(msg)
    return True


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
    parser.add_argument(
        "--project-name",
        help="Project name for injecting project-specific learnings into the review prompt.",
    )
    parser.add_argument(
        "--errors", action="store_true",
        help="Run an additional silent-failure-hunter pass to detect swallowed "
             "exceptions and silent error paths.",
    )
    parser.add_argument(
        "--comments", action="store_true",
        help="Use comment-quality review (accuracy, completeness, stale TODOs)",
    )
    parser.add_argument(
        "--ultra", action="store_true",
        help="Ultra review: combine the architecture-focused pass with the "
             "silent-failure-hunter pass for the most thorough review.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Review even if the PR is closed or merged, and re-review even "
             "when no new commits were pushed since the last review.",
    )
    parser.add_argument(
        "--bot-comments", action="store_true",
        help="Run an additional pass to triage inline comments from code-review bots "
             "(CodeRabbit, Copilot Review, Sourcery).",
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
        project_name=cli_args.project_name,
        errors=cli_args.errors,
        comments=cli_args.comments,
        ultra=cli_args.ultra,
        force=cli_args.force,
        bot_comments=cli_args.bot_comments,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
