"""GitHub notification fetching and parsing.

Core module for the notification-driven commands feature. Handles:
- Fetching unread notifications filtered to @mentions
- Parsing @mention commands from comment bodies
- Converting API URLs to web URLs
- Reaction-based deduplication (any bot reaction = processed)
- Permission checks for authorized users
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from app.bounded_set import BoundedSet
from app.github import SSOAuthRequired, api

log = logging.getLogger(__name__)

# Count of SSO failures observed during the current processing cycle.
# Reset at the start of each cycle by the caller (loop_manager).
_sso_failure_count: int = 0

# Consecutive fetch failures in fetch_unread_notifications.
# After _FETCH_FAILURE_THRESHOLD consecutive failures, log at warning level
# and notify via outbox so the user knows GitHub polling is broken.
_consecutive_fetch_failures: int = 0
_FETCH_FAILURE_THRESHOLD = 3
# Track whether we already sent an outbox alert for the current failure streak,
# so we don't spam the user on every subsequent failure.
_fetch_failure_alerted: bool = False

# Consecutive SSO failures across cycles.  Only reset when a full cycle
# completes with zero SSO failures, indicating the token works again.
_consecutive_sso_failures: int = 0

# Threshold at which an outbox alert is sent.
SSO_ESCALATION_THRESHOLD: int = 5

# Track whether the outbox escalation has already fired for the current
# failure streak so we don't spam on every subsequent cycle.
_sso_escalation_sent: bool = False


def reset_sso_failure_count() -> None:
    """Reset the per-cycle SSO failure counter.

    Called at the start of each notification cycle.  Does NOT reset the
    cross-cycle consecutive counter — that is handled by
    ``update_consecutive_sso_failures()``.
    """
    global _sso_failure_count
    _sso_failure_count = 0


def reset_consecutive_sso_state() -> None:
    """Reset all consecutive SSO failure state.  For tests only."""
    global _consecutive_sso_failures, _sso_escalation_sent
    _consecutive_sso_failures = 0
    _sso_escalation_sent = False


def get_sso_failure_count() -> int:
    """Return the number of SSO failures observed in the current cycle."""
    return _sso_failure_count


def reset_fetch_failure_count() -> None:
    """Reset the consecutive fetch failure counter."""
    global _consecutive_fetch_failures, _fetch_failure_alerted
    _consecutive_fetch_failures = 0
    _fetch_failure_alerted = False


def get_fetch_failure_count() -> int:
    """Return the number of consecutive fetch failures."""
    return _consecutive_fetch_failures


def _record_fetch_failure(reason: str) -> None:
    """Record a fetch failure, escalate logging and notify after threshold."""
    global _consecutive_fetch_failures, _fetch_failure_alerted
    _consecutive_fetch_failures += 1

    if _consecutive_fetch_failures < _FETCH_FAILURE_THRESHOLD:
        log.debug("GitHub API: failed to fetch notifications: %s", reason)
        return

    # Threshold reached — escalate to warning
    log.warning(
        "GitHub API: %d consecutive fetch failures (latest: %s). "
        "Notification polling may be broken.",
        _consecutive_fetch_failures,
        reason,
    )

    # Send a one-time outbox alert so the user gets a Telegram notification
    if not _fetch_failure_alerted:
        _fetch_failure_alerted = True
        _send_fetch_failure_alert(_consecutive_fetch_failures, reason)


def _send_fetch_failure_alert(count: int, reason: str) -> None:
    """Write a fetch-failure alert to outbox.md."""
    try:
        koan_root = os.environ.get("KOAN_ROOT", "")
        if not koan_root:
            return
        outbox_path = Path(koan_root) / "instance" / "outbox.md"
        if not outbox_path.parent.is_dir():
            return
        from app.utils import append_to_outbox
        msg = (
            f"⚠️ GitHub notification polling has failed {count} times in a row "
            f"({reason}). @mentions may be missed until connectivity is restored.\n"
        )
        append_to_outbox(outbox_path, msg)
    except Exception as exc:
        log.debug("Failed to write fetch-failure alert to outbox: %s", exc)


def _clear_fetch_failures() -> None:
    """Reset failure counter on a successful fetch."""
    global _consecutive_fetch_failures, _fetch_failure_alerted
    if _consecutive_fetch_failures > 0:
        if _fetch_failure_alerted:
            log.info(
                "GitHub API: notification fetch recovered after %d failures",
                _consecutive_fetch_failures,
            )
        _consecutive_fetch_failures = 0
        _fetch_failure_alerted = False


def get_consecutive_sso_failures() -> int:
    """Return the number of consecutive SSO failures across cycles."""
    return _consecutive_sso_failures


def update_consecutive_sso_failures() -> None:
    """Update the cross-cycle consecutive failure counter.

    Call this AFTER a notification cycle completes.  If the cycle had
    SSO failures, they are added to the running total.  If the cycle
    was clean, the running total resets to zero.
    """
    global _consecutive_sso_failures, _sso_escalation_sent
    if _sso_failure_count > 0:
        _consecutive_sso_failures += _sso_failure_count
    else:
        _consecutive_sso_failures = 0
        _sso_escalation_sent = False


def check_sso_escalation() -> bool:
    """Check if SSO failures should be escalated to outbox.

    Returns True if an outbox alert was written, False otherwise.
    The alert fires once per failure streak (reset when failures stop).
    """
    global _sso_escalation_sent
    if _sso_escalation_sent:
        return False
    if _consecutive_sso_failures < SSO_ESCALATION_THRESHOLD:
        return False

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return False

    outbox_path = Path(koan_root) / "instance" / "outbox.md"
    try:
        from app.utils import append_to_outbox
        append_to_outbox(
            outbox_path,
            f"⚠️ GitHub SSO auth has failed {_consecutive_sso_failures} times "
            "consecutively — token needs re-authorization.\n"
            "Run: `gh auth refresh -h github.com -s read:org`\n",
        )
        _sso_escalation_sent = True
        log.warning(
            "SSO escalation: %d consecutive failures, alert written to outbox",
            _consecutive_sso_failures,
        )
        return True
    except Exception as e:
        log.debug("Failed to write SSO escalation to outbox: %s", e)
        return False


def _record_sso_failure(context: str) -> None:
    """Record an SSO failure and log a warning (once per cycle)."""
    global _sso_failure_count
    _sso_failure_count += 1
    if _sso_failure_count == 1:
        log.warning(
            "GitHub SSO auth failure detected (%s). "
            "Token needs re-authorization: gh auth refresh -h github.com -s read:org",
            context,
        )

# In-memory set of processed comment IDs (resets on restart).
# Bounded: FIFO eviction when limit is reached (oldest entries removed first).
_MAX_PROCESSED_COMMENTS = 10000
_processed_comments: BoundedSet = BoundedSet(maxlen=_MAX_PROCESSED_COMMENTS)

# Regex for extracting @mention commands, skipping code blocks
_CODE_BLOCK_RE = re.compile(r'```.*?```|`[^`]+`', re.DOTALL)


# Reasons that may contain @mention commands in the latest comment.
# "mention" is the primary signal.  "author" and "comment" notifications
# can hide @mentions when a bot-authored thread already has an unread
# notification — GitHub updates the existing notification instead of
# creating a new "mention" one.
# "review_requested" — review requests can include @mentions in the
# associated comment; the notification reason stays review_requested.
# "team_mention" — @team mentions that the bot is part of.
# "subscribed" — when the bot watches a repo, @mentions on threads with
# existing unread notifications may keep the subscribed reason instead
# of updating to mention (GitHub API race condition / caching).
# "assign" — the bot was assigned to an issue; triggers /implement mission.
_ACTIONABLE_REASONS = {
    "mention", "author", "comment",
    "review_requested", "team_mention", "subscribed",
    "assign",
}


class FetchResult:
    """Result from fetch_unread_notifications.

    Attributes:
        actionable: Notifications that might contain @mention commands
            (reasons: mention, author, comment).
        drain: Non-actionable notifications from known repos that should
            be marked as read to prevent accumulation.
    """
    __slots__ = ("actionable", "drain")

    def __init__(self, actionable: List[dict], drain: List[dict]):
        self.actionable = actionable
        self.drain = drain


def fetch_unread_notifications(known_repos: Optional[Set[str]] = None,
                               since: Optional[str] = None) -> FetchResult:
    """Fetch GitHub notifications, categorized for processing.

    Returns actionable notifications (may contain @mention commands) and
    drain-only notifications (noise that should be marked as read to
    prevent accumulation that blocks future @mention detection).

    When ``since`` is provided, fetches ALL notifications (read + unread)
    updated after that timestamp.  This catches @mentions that were
    auto-read by the GitHub web UI before the bot could poll them —
    a common race condition when the user posts an @mention while
    viewing the PR page.

    Args:
        known_repos: Optional set of "owner/repo" strings to filter against.
            If None, all notifications from any repo are included.
        since: Optional ISO 8601 timestamp.  When set, uses ``all=true``
            to include already-read notifications updated after this time.

    Returns:
        FetchResult with actionable and drain lists.
    """
    try:
        # Build endpoint with query params directly in the URL.
        # Using -f flags would cause gh to send a POST (with JSON body)
        # instead of GET, resulting in 404 from the notifications endpoint.
        endpoint = "notifications"
        if since:
            endpoint = f"notifications?since={since}&all=true"
        raw = api(endpoint, extra_args=["--paginate"], timeout=30)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        _record_fetch_failure(str(e))
        return FetchResult([], [])

    if not raw:
        _record_fetch_failure("empty response")
        return FetchResult([], [])

    try:
        notifications = json.loads(raw)
    except json.JSONDecodeError:
        _record_fetch_failure("invalid JSON")
        return FetchResult([], [])

    if not isinstance(notifications, list):
        _record_fetch_failure(f"unexpected type: {type(notifications).__name__}")
        return FetchResult([], [])

    # Successful parse — clear any failure streak
    _clear_fetch_failures()

    log.debug(
        "GitHub API: %d total notifications%s",
        len(notifications),
        " (including read)" if since else "",
    )

    skipped_reasons: Dict[str, int] = {}
    skipped_repos: List[str] = []
    actionable = []
    drain = []
    # Direct @mention reasons always pass the repo filter — the bot was
    # explicitly called, so we must process regardless of projects.yaml.
    _DIRECT_MENTION_REASONS = {"mention", "team_mention"}
    for notif in notifications:
        reason = notif.get("reason", "?")
        repo_name = notif.get("repository", {}).get("full_name", "?")

        # Filter by known repos if provided — normalize for comparison.
        # Direct @mentions bypass this filter: the bot was explicitly invoked.
        if known_repos and reason not in _DIRECT_MENTION_REASONS:
            repo_lower = repo_name.lower()
            if repo_lower not in known_repos:
                skipped_repos.append(repo_name)
                continue

        if reason in _ACTIONABLE_REASONS:
            actionable.append(notif)
        else:
            drain.append(notif)
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

    if skipped_reasons:
        log.debug(
            "GitHub: %d drain-only notifications: %s",
            sum(skipped_reasons.values()),
            ", ".join(f"{r}={c}" for r, c in sorted(skipped_reasons.items())),
        )
    if skipped_repos:
        log.debug(
            "GitHub: skipped %d notifications from unknown repos: %s",
            len(skipped_repos), ", ".join(skipped_repos),
        )

    log.debug(
        "GitHub: %d actionable + %d drain notification(s) from known repos",
        len(actionable), len(drain),
    )
    return FetchResult(actionable, drain)


def parse_mention_command(comment_body: str, nickname: str) -> Optional[Tuple[str, str]]:
    """Extract command and args from a @mention in a comment body.

    Ignores mentions inside code blocks (``` or `).
    Only processes the first @mention found.

    Args:
        comment_body: The full comment text.
        nickname: The bot's GitHub username (without @).

    Returns:
        Tuple of (command, context) or None if no valid mention found.
        Command is lowercase. Context is the remaining text after command.
    """
    if not comment_body or not nickname:
        return None

    # Remove code blocks to avoid matching mentions in code
    clean_body = _CODE_BLOCK_RE.sub('', comment_body)

    # Match @nickname followed by a command word (optional leading / is stripped)
    pattern = rf'@{re.escape(nickname)}\s+/?(\w+)(.*?)(?:\n|$)'
    match = re.search(pattern, clean_body, re.IGNORECASE)
    if not match:
        return None

    command = match.group(1).strip().lower()
    context = match.group(2).strip()

    if not command:
        return None

    return command, context


def api_url_to_web_url(api_url: str) -> str:
    """Convert a GitHub API URL to a web URL.

    Examples:
        https://api.github.com/repos/owner/repo/pulls/123
        → https://github.com/owner/repo/pull/123

        https://api.github.com/repos/owner/repo/issues/42
        → https://github.com/owner/repo/issues/42
    """
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    # API uses "pulls" (plural), web uses "pull" (singular)
    url = re.sub(r'/pulls/(\d+)', r'/pull/\1', url)
    return url


def get_comment_from_notification(notification: dict) -> Optional[dict]:
    """Fetch the latest comment that triggered the notification.

    Note: subject.latest_comment_url points to the most recent comment on
    the thread, not necessarily the one that triggered the notification.
    When the bot itself posts a comment after the @mention, this URL shifts.
    Use find_mention_in_thread() as a fallback when this returns a self-authored comment.

    Args:
        notification: A notification dict from the GitHub API.

    Returns:
        The comment dict, or None if it can't be fetched.
    """
    comment_url = notification.get("subject", {}).get("latest_comment_url", "")
    if not comment_url:
        return None

    # Convert full URL to API endpoint (strict prefix check to prevent SSRF)
    api_prefix = "https://api.github.com/"
    if not comment_url.startswith(api_prefix):
        return None
    endpoint = comment_url[len(api_prefix):]
    if not endpoint:
        return None

    try:
        raw = api(endpoint, timeout=30)
        return json.loads(raw) if raw else None
    except SSOAuthRequired:
        _record_sso_failure(f"get_comment endpoint={endpoint[:80]}")
        return None
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        log.warning("GitHub API: failed to fetch comment %s: %s", endpoint[:80], e)
        return None


def _search_comments_for_mention(
    comments: list,
    bot_username: str,
    owner: str,
    repo: str,
) -> Optional[dict]:
    """Search a list of comments for an unprocessed @mention of the bot.

    Shared helper for find_mention_in_thread — avoids duplicating the
    filter/dedup logic across issue comments and PR review comments.

    Returns:
        The first unprocessed comment containing an @mention, or None.
    """
    bot_lower = f"@{bot_username}".lower()

    for comment in comments:
        # Skip bot's own comments
        if comment.get("user", {}).get("login") == bot_username:
            continue

        # Check if this comment mentions the bot
        body = comment.get("body", "")
        if bot_lower not in body.lower():
            continue

        # Check if already processed (has bot reaction)
        comment_id = str(comment.get("id", ""))
        comment_api_url = comment.get("url", "")
        if check_already_processed(
            comment_id, bot_username, owner, repo,
            comment_api_url=comment_api_url,
        ):
            continue

        log.debug(
            "GitHub: found unprocessed @mention in comment %s by @%s",
            comment_id,
            comment.get("user", {}).get("login", "?"),
        )
        return comment

    return None


def find_mention_in_thread(
    notification: dict,
    bot_username: str,
) -> Optional[dict]:
    """Search a PR/issue thread for an unprocessed @mention comment.

    Fallback for when latest_comment_url points to a bot comment (self-mention
    race condition). Fetches recent comments on the thread and finds the first
    unprocessed @mention of the bot.

    Searches both issue comments and PR review comments (inline code comments),
    since @mentions can appear in either location.

    Args:
        notification: A notification dict from the GitHub API.
        bot_username: The bot's GitHub username.

    Returns:
        The comment dict containing the @mention, or None if not found.
    """
    subject_url = notification.get("subject", {}).get("url", "")
    if not subject_url:
        return None

    # Extract owner/repo/number and type from subject URL
    # e.g. https://api.github.com/repos/cpanel/Test-MockFile/pulls/208
    match = re.match(
        r'https://api\.github\.com/repos/([^/]+)/([^/]+)/(pulls|issues)/(\d+)',
        subject_url,
    )
    if not match:
        return None

    owner, repo, subject_type, number = match.groups()

    # Search comment endpoints for an @mention.  Issue comments always,
    # review comments only for PRs.
    endpoints = [
        (f"repos/{owner}/{repo}/issues/{number}/comments"
         "?per_page=100&sort=created&direction=desc",
         f"find_mention issue_comments {owner}/{repo}#{number}"),
    ]
    if subject_type == "pulls":
        endpoints.append(
            (f"repos/{owner}/{repo}/pulls/{number}/comments"
             "?per_page=100&sort=created&direction=desc",
             f"find_mention review_comments {owner}/{repo}#{number}"),
        )

    for endpoint, sso_label in endpoints:
        try:
            raw = api(endpoint, timeout=30)
            comments = json.loads(raw) if raw else []
        except SSOAuthRequired:
            _record_sso_failure(sso_label)
            continue
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            log.warning("GitHub API: failed to fetch %s: %s", endpoint[:80], e)
            continue

        if not isinstance(comments, list):
            continue

        if len(comments) >= 100:
            log.warning(
                "Truncated comment list for %s/%s#%s (%d items) — "
                "mention may be missed",
                owner, repo, number, len(comments),
            )

        result = _search_comments_for_mention(comments, bot_username, owner, repo)
        if result:
            return result

    return None


def mark_notification_read(thread_id: str) -> bool:
    """Mark a notification thread as read.

    Args:
        thread_id: The notification thread ID.

    Returns:
        True if successful, False otherwise.
    """
    try:
        api(f"notifications/threads/{thread_id}", method="PATCH", timeout=30)
        return True
    except RuntimeError:
        return False


def _reactions_endpoint(
    comment_api_url: str = "",
    owner: str = "",
    repo: str = "",
    comment_id: str = "",
) -> str:
    """Build the reactions API endpoint for a comment.

    Uses comment_api_url when available (handles all comment types:
    issue comments, PR review comments, commit comments).
    Falls back to the issues/comments endpoint for backward compatibility.

    Args:
        comment_api_url: The comment's canonical API URL (from comment["url"]).
        owner: Repository owner (fallback).
        repo: Repository name (fallback).
        comment_id: Comment ID (fallback).

    Returns:
        The reactions API endpoint path.
    """
    if comment_api_url:
        api_prefix = "https://api.github.com/"
        if comment_api_url.startswith(api_prefix):
            return comment_api_url[len(api_prefix):] + "/reactions"
    return f"repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"


def check_already_processed(comment_id: str, bot_username: str,
                             owner: str, repo: str,
                             comment_api_url: str = "") -> bool:
    """Check if a comment has already been processed (has bot reaction).

    Checks for any reaction from the bot — both 👍 (command acknowledgment)
    and 👀 (AI reply acknowledgment). This prevents duplicate processing
    when mark_notification_read fails.

    Also checks in-memory set for current session deduplication.

    Args:
        comment_id: The comment ID.
        bot_username: The bot's GitHub username.
        owner: Repository owner.
        repo: Repository name.
        comment_api_url: The comment's canonical API URL. When provided,
            derives the correct reactions endpoint (handles PR review
            comments, commit comments, etc.). Falls back to
            issues/comments endpoint.

    Returns:
        True if already processed.
    """
    # Check in-memory first
    if comment_id in _processed_comments:
        return True

    # Check persistent file tracker (survives restarts)
    koan_root = os.environ.get("KOAN_ROOT", "")
    if koan_root:
        from app.github_notification_tracker import is_comment_tracked
        instance_dir = os.path.join(koan_root, "instance")
        if is_comment_tracked(instance_dir, comment_id):
            _processed_comments.add(comment_id)
            return True

    # Check GitHub reactions — any reaction from the bot means processed
    endpoint = _reactions_endpoint(comment_api_url, owner, repo, comment_id)
    try:
        raw = api(endpoint, timeout=30)
        reactions = json.loads(raw) if raw else []
        if isinstance(reactions, list):
            for reaction in reactions:
                if reaction.get("user", {}).get("login") == bot_username:
                    _processed_comments.add(comment_id)
                    return True
    except SSOAuthRequired:
        _record_sso_failure(f"check_already_processed comment={comment_id}")
    except (RuntimeError, json.JSONDecodeError):
        pass

    return False


def add_reaction(owner: str, repo: str, comment_id: str,
                 emoji: str = "+1", comment_api_url: str = "") -> bool:
    """Add a reaction to a comment.

    Args:
        owner: Repository owner.
        repo: Repository name.
        comment_id: The comment ID.
        emoji: Reaction content (default: "+1" for 👍).
        comment_api_url: The comment's canonical API URL. When provided,
            derives the correct reactions endpoint (handles PR review
            comments, commit comments, etc.).

    Returns:
        True if successful.
    """
    endpoint = _reactions_endpoint(comment_api_url, owner, repo, comment_id)
    try:
        api(
            endpoint,
            method="POST",
            extra_args=["-f", f"content={emoji}"],
            timeout=30,
        )
        _processed_comments.add(comment_id)
        return True
    except RuntimeError:
        return False


def check_user_permission(owner: str, repo: str, username: str,
                           allowed_users: List[str]) -> bool:
    """Check if a user is authorized to trigger bot commands.

    Args:
        owner: Repository owner.
        repo: Repository name.
        username: The GitHub username to check.
        allowed_users: List of allowed usernames, or ["*"] for all.

    Returns:
        True if authorized.
    """
    # Explicit allowlist: trust the admin's decision, no API call needed
    if "*" not in allowed_users:
        return username in allowed_users

    # Wildcard: verify at least write access via GitHub API
    try:
        raw = api(f"repos/{owner}/{repo}/collaborators/{username}/permission", timeout=30)
        data = json.loads(raw) if raw else {}
        permission = data.get("permission", "none")
        return permission in ("admin", "write")
    except SSOAuthRequired:
        _record_sso_failure(f"check_user_permission {owner}/{repo}")
        return False
    except (RuntimeError, json.JSONDecodeError):
        return False


def is_notification_stale(notification: dict, max_age_hours: int = 24) -> bool:
    """Check if a notification is too old to process.

    Args:
        notification: A notification dict.
        max_age_hours: Maximum age in hours (default: 24).

    Returns:
        True if the notification is stale.
    """
    updated_at = notification.get("updated_at", "")
    if not updated_at:
        return True

    try:
        notif_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - notif_time).total_seconds() / 3600
        return age_hours > max_age_hours
    except (ValueError, TypeError):
        return True


def is_self_mention(comment: dict, bot_username: str) -> bool:
    """Check if the comment was posted by the bot itself.

    Args:
        comment: A comment dict from the GitHub API.
        bot_username: The bot's GitHub username.

    Returns:
        True if the comment author is the bot.
    """
    author = comment.get("user", {}).get("login", "")
    return author == bot_username


def extract_comment_metadata(comment_url: str) -> Optional[Tuple[str, str, str]]:
    """Extract owner, repo, and comment ID from a comment URL.

    Handles web URLs and API URLs for all GitHub comment types:
        https://github.com/owner/repo/issues/123#issuecomment-456
        https://api.github.com/repos/owner/repo/issues/comments/456
        https://api.github.com/repos/owner/repo/pulls/comments/456

    Returns:
        Tuple of (owner, repo, comment_id) or None.
    """
    # Try API URL format (handles issues/comments and pulls/comments)
    match = re.match(
        r'https?://api\.github\.com/repos/([^/]+)/([^/]+)/(?:issues|pulls)/comments/(\d+)',
        comment_url,
    )
    if match:
        return match.group(1), match.group(2), match.group(3)

    # Try web URL format
    match = re.match(
        r'https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/\d+#issuecomment-(\d+)',
        comment_url,
    )
    if match:
        return match.group(1), match.group(2), match.group(3)

    return None
