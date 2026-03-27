"""
Koan -- Loop management utilities for the agent loop.

Data-processing and decision-making logic used by the main loop:
1. Project config validation and lookup
2. Autonomous mode focus area resolution
3. Pending.md file creation
4. Interruptible sleep logic with wake-on-mission

CLI interface:
    python -m app.loop_manager resolve-focus --mode <mode>
    python -m app.loop_manager create-pending --instance ... --project-name ...
    python -m app.loop_manager validate-projects
    python -m app.loop_manager lookup-project --name <name>
    python -m app.loop_manager interruptible-sleep --interval <seconds> --koan-root ...
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.missions import count_pending
from app.utils import atomic_write


# --- Focus area resolution ---

# Maps autonomous mode to human-readable focus area description.
_FOCUS_AREAS = {
    "review": "Low-cost review: audit code, find issues, suggest improvements (READ-ONLY)",
    "implement": "Medium-cost implementation: prototype fixes, small improvements",
    "deep": "High-cost deep work: refactoring, architectural changes",
    "wait": "Budget exhausted — entering pause mode",
}


def resolve_focus_area(autonomous_mode: str, has_mission: bool = False) -> str:
    """Resolve the focus area description for a given autonomous mode.

    Args:
        autonomous_mode: Current mode (review/implement/deep/wait).
        has_mission: Whether a specific mission was assigned.

    Returns:
        Human-readable focus area string.
    """
    if has_mission:
        return "Execute assigned mission"
    return _FOCUS_AREAS.get(autonomous_mode, "General autonomous work")


# --- Project config validation and lookup ---


def validate_projects(
    projects: list, max_projects: int = 50
) -> Optional[str]:
    """Validate project configuration.

    Args:
        projects: List of (name, path) tuples.
        max_projects: Maximum allowed projects.

    Returns:
        Error message string if validation fails, None if valid.
    """
    if not projects:
        return "No projects configured. Create projects.yaml or set KOAN_PROJECTS env var."

    if len(projects) > max_projects:
        return f"Max {max_projects} projects allowed. You have {len(projects)}."

    for name, path in projects:
        if not os.path.isdir(path):
            return f"Project '{name}' path does not exist: {path}"

        # Verify the project path is a git repository
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=path,
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                return f"Project '{name}' is not a git repository: {path}"
        except (OSError, subprocess.TimeoutExpired):
            return f"Project '{name}' is not a git repository: {path}"

    return None


def lookup_project(project_name: str, projects: list) -> Optional[str]:
    """Find project path by name (case-insensitive).

    Args:
        project_name: Name to look up.
        projects: List of (name, path) tuples.

    Returns:
        Project path if found, None otherwise.
    """
    lower = project_name.lower()
    for name, path in projects:
        if name.lower() == lower:
            return path
    return None


def format_project_list(projects: list) -> str:
    """Format project names as a sorted bullet list.

    Args:
        projects: List of (name, path) tuples.

    Returns:
        Formatted string with bullet points, one per line.
    """
    return "\n".join(f"  \u2022 {name}" for name, _ in sorted(projects))


# --- Pending.md creation ---


def create_pending_file(
    instance_dir: str,
    project_name: str,
    run_num: int,
    max_runs: int,
    autonomous_mode: str,
    mission_title: str = "",
) -> str:
    """Create the pending.md progress journal file for a run.

    Args:
        instance_dir: Path to instance directory.
        project_name: Current project name.
        run_num: Current run number.
        max_runs: Maximum runs per session.
        autonomous_mode: Current autonomous mode.
        mission_title: Mission title (empty for autonomous runs).

    Returns:
        Path to the created pending.md file.
    """
    pending_path = Path(instance_dir) / "journal" / "pending.md"
    journal_dir = Path(instance_dir) / "journal" / datetime.now().strftime("%Y-%m-%d")
    journal_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if mission_title:
        header = f"# Mission: {mission_title}"
        mode = autonomous_mode if autonomous_mode else "mission"
    else:
        header = "# Autonomous run"
        mode = autonomous_mode

    content = f"""{header}
Project: {project_name}
Started: {now}
Run: {run_num}/{max_runs}
Mode: {mode}

---
"""
    atomic_write(pending_path, content)
    return str(pending_path)


# --- GitHub notification processing ---

# Throttle: minimum seconds between GitHub notification checks.
# This default is overridden at runtime by github.check_interval_seconds from config.yaml.
_GITHUB_CHECK_INTERVAL = 60
# Maximum backoff interval (3 minutes) when notifications are consistently empty.
# Overridden at runtime by github.max_check_interval_seconds from config.yaml.
_GITHUB_MAX_CHECK_INTERVAL = 180
_last_github_check: float = 0
# ISO 8601 timestamp of the last successful notification fetch.
# Passed as ``since`` to fetch_unread_notifications so that already-read
# notifications (auto-read by GitHub web UI) are still returned.
_last_github_check_iso: str = ""
_consecutive_empty_checks: int = 0
# Track whether we've logged the first config status (avoids repeating every check)
_github_config_logged: bool = False
# Track whether we've loaded the configured interval from config.yaml
_github_interval_loaded: bool = False
# Cached _load_github_config() result with mtime invalidation.
# Thread-safe via _github_state_lock.
_GITHUB_CONFIG_UNSET = object()  # sentinel: "no cached value yet"
_github_config_cache = _GITHUB_CONFIG_UNSET
_github_config_cache_mtime: float = 0

# --- Notification processing cache ---
# Avoid re-processing the same notification repeatedly across loop iterations.
# Key: (thread_id, updated_at) — naturally invalidates when the notification is
# updated (e.g. a new comment arrives). Value: epoch timestamp of when cached.
# Entries expire after _NOTIF_CACHE_TTL seconds.
_NOTIF_CACHE_TTL = 86400  # 24 hours
_NOTIF_CACHE_MAX = 2000
_notif_cache: dict = {}
_notif_cache_lock = threading.Lock()

# --- Failed error reply queue ---
# When posting an error reply to GitHub fails, store the params here for retry
# on the next notification cycle. Each entry is a dict with keys:
# owner, repo, issue_num, comment_id, error, comment_api_url.
_MAX_REPLY_RETRIES = 3
_MAX_PENDING_REPLIES = 50
_pending_error_replies: list = []
_pending_error_replies_lock = threading.Lock()

# Lock protecting all module-level mutable GitHub state above.
# Acquired for short state reads/writes only — never held during API calls.
_github_state_lock = threading.Lock()

log = logging.getLogger(__name__)


def _github_log(message: str, level: str = "info") -> None:
    """Print a console-visible log message for GitHub notifications.

    Uses print() to match run.py's logging pattern, ensuring visibility
    in 'make logs' output. Also logs via Python logging at matching level.
    """
    print(f"[github] {message}", flush=True)
    if level == "debug":
        log.debug(message)
    elif level == "warning":
        log.warning(message)
    else:
        log.info(message)


def _notif_cache_key(notif: dict) -> Optional[tuple]:
    """Build a cache key from a notification's thread ID and updated_at.

    Returns None if the notification has no truthy ``id`` — callers must
    skip caching to avoid all ID-less notifications colliding on the same
    cache slot.
    """
    notif_id = notif.get("id")
    if not notif_id:
        log.warning(
            "GitHub notification missing 'id', skipping cache: %s",
            notif.get("subject", {}).get("title", "<unknown>"),
        )
        return None
    return (str(notif_id), notif.get("updated_at", ""))


def _is_notif_cached(notif: dict) -> bool:
    """Check if a notification is in the processing cache and not expired."""
    key = _notif_cache_key(notif)
    if key is None:
        return False  # ID-less notifications are never considered cached
    with _notif_cache_lock:
        cached_at = _notif_cache.get(key)
        if cached_at is None:
            return False
        if time.time() - cached_at > _NOTIF_CACHE_TTL:
            del _notif_cache[key]
            return False
        return True


def _cache_notif(notif: dict) -> None:
    """Add a notification to the processing cache."""
    key = _notif_cache_key(notif)
    if key is None:
        return  # Warning already emitted by _notif_cache_key
    now = time.time()
    with _notif_cache_lock:
        _notif_cache[key] = now
        # Always sweep expired entries to prevent stale cache buildup.
        # Without this, expired entries only get evicted on cache-hit
        # (in _is_notif_cached) or when size exceeds _NOTIF_CACHE_MAX,
        # letting stale entries accumulate and block re-appearing notifications.
        expired = [k for k, v in _notif_cache.items() if now - v > _NOTIF_CACHE_TTL]
        for k in expired:
            del _notif_cache[k]
        # If still over limit, evict oldest
        if _notif_cache and len(_notif_cache) > _NOTIF_CACHE_MAX:
            oldest_key = min(_notif_cache, key=_notif_cache.get)
            del _notif_cache[oldest_key]


def _get_config_mtime(koan_root: str) -> float:
    """Get the mtime of config.yaml, or 0 if it doesn't exist."""
    config_path = Path(koan_root) / "instance" / "config.yaml"
    try:
        return config_path.stat().st_mtime
    except OSError:
        return 0


def _load_github_config(config: dict, koan_root: str, instance_dir: str) -> Optional[dict]:
    """Load and validate GitHub configuration.

    Caches the result and invalidates when config.yaml's mtime changes,
    avoiding repeated parsing on every notification cycle.

    Returns:
        Dict with config data or None if feature is disabled/invalid
    """
    global _github_config_logged, _github_config_cache, _github_config_cache_mtime

    current_mtime = _get_config_mtime(koan_root)

    with _github_state_lock:
        # Check mtime-based cache: return cached result if config file hasn't changed
        if _github_config_cache is not _GITHUB_CONFIG_UNSET and current_mtime == _github_config_cache_mtime:
            return _github_config_cache

    from app.github_config import get_github_commands_enabled, get_github_max_age_hours, get_github_nickname

    if not get_github_commands_enabled(config):
        with _github_state_lock:
            if not _github_config_logged:
                _github_log("Commands disabled (github.commands_enabled not set in config.yaml)", "debug")
                _github_config_logged = True
            _github_config_cache_mtime = current_mtime
            _github_config_cache = None
        return None

    nickname = get_github_nickname(config)
    if not nickname:
        with _github_state_lock:
            if not _github_config_logged:
                _github_log("Commands enabled but github.nickname is not set — skipping", "warning")
                _github_config_logged = True
            _github_config_cache_mtime = current_mtime
            _github_config_cache = None
        return None

    bot_username = os.environ.get("GITHUB_USER", nickname)
    max_age = get_github_max_age_hours(config)

    result = {
        "nickname": nickname,
        "bot_username": bot_username,
        "max_age": max_age,
    }
    with _github_state_lock:
        if not _github_config_logged:
            _github_log(f"Monitoring @{nickname} mentions (bot_user={bot_username}, max_age={max_age}h)")
            _github_config_logged = True
        _github_config_cache = result
        _github_config_cache_mtime = current_mtime
    return result


# Module-level cache for the GitHub notification skill registry.
# _build_skill_registry() is called every ~30s cycle; caching avoids
# rebuilding from filesystem each time.  Invalidated when skills
# directories change on disk (mtime check).
_gh_cached_registry = None
_gh_cached_extra_dirs: Optional[tuple] = None
_gh_cached_mtime: float = 0.0


def _skills_dir_mtime(instance_dir: str) -> float:
    """Get the max mtime of core and instance skills directories."""
    best = 0.0
    core_dir = Path(__file__).resolve().parent.parent / "skills" / "core"
    try:
        best = max(best, core_dir.stat().st_mtime)
    except OSError:
        pass
    instance_skills = Path(instance_dir) / "skills"
    if instance_skills.is_dir():
        try:
            best = max(best, instance_skills.stat().st_mtime)
        except OSError:
            pass
    return best


def _build_skill_registry(instance_dir: str):
    """Build combined skill registry from core and instance skills.

    Uses a module-level cache to avoid rebuilding from filesystem on
    every GitHub notification polling cycle (~30s).  Automatically
    invalidates when skills directories change on disk (new skill added).

    Returns:
        Populated SkillRegistry
    """
    global _gh_cached_registry, _gh_cached_extra_dirs, _gh_cached_mtime
    from app.skills import build_registry

    instance_skills = Path(instance_dir) / "skills"
    extra = tuple(p for p in [instance_skills] if p.is_dir())
    current_mtime = _skills_dir_mtime(instance_dir)

    with _github_state_lock:
        if (_gh_cached_registry is not None
                and extra == _gh_cached_extra_dirs
                and current_mtime <= _gh_cached_mtime):
            return _gh_cached_registry

    registry = build_registry(list(extra))

    with _github_state_lock:
        _gh_cached_registry = registry
        _gh_cached_extra_dirs = extra
        _gh_cached_mtime = current_mtime

    return registry


def _normalize_github_url(url: str) -> str:
    """Normalize a github_url to 'owner/repo' format.

    Handles both formats:
        "owner/repo" → "owner/repo"
        "https://github.com/owner/repo" → "owner/repo"
        "https://github.com/owner/repo.git" → "owner/repo"
    """
    # Strip full URL prefix
    match = re.match(r'https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$', url)
    if match:
        return match.group(1).lower()
    # Already in owner/repo format (or close)
    stripped = url.strip().rstrip("/").lower()
    # Remove trailing .git
    if stripped.endswith(".git"):
        stripped = stripped[:-4]
    return stripped


def _get_known_repos_from_projects(koan_root: str) -> Optional[set]:
    """Extract known repo names from all project sources.

    Includes repos from:
    1. projects.yaml (github_url + github_urls fields)
    2. Workspace projects (in-memory github URL cache from git remotes)

    Returns:
        Set of "owner/repo" strings or None for all repos.
        Normalizes github_url values to ensure consistent matching
        against GitHub API's full_name format.
    """
    from app.projects_config import load_projects_config

    known_repos = set()

    # 1. projects.yaml — primary source
    projects_config = load_projects_config(koan_root)
    if projects_config:
        for name, proj in projects_config.get("projects", {}).items():
            if not isinstance(proj, dict):
                continue
            gh_url = proj.get("github_url", "")
            if gh_url:
                known_repos.add(_normalize_github_url(gh_url))
            # Also include all remotes (github_urls) for fork workflows
            for url in proj.get("github_urls", []):
                if url:
                    known_repos.add(_normalize_github_url(url))

    # 2. Workspace projects — in-memory cache populated at startup
    try:
        from app.projects_merged import get_all_github_urls_cache, get_github_url_cache

        # Primary URLs (origin remote)
        for _name, url in get_github_url_cache().items():
            if url:
                known_repos.add(_normalize_github_url(url))

        # All remote URLs (origin + upstream + others)
        for _name, urls in get_all_github_urls_cache().items():
            for url in urls:
                if url:
                    known_repos.add(_normalize_github_url(url))
    except ImportError:
        pass

    if known_repos:
        log.debug("GitHub: known repos from all sources: %s", known_repos)

    return known_repos or None


def _get_effective_check_interval_locked() -> int:
    """Compute check interval with backoff. Caller must hold _github_state_lock."""
    if _consecutive_empty_checks <= 0:
        return _GITHUB_CHECK_INTERVAL
    return min(
        _GITHUB_CHECK_INTERVAL * (2 ** _consecutive_empty_checks),
        _GITHUB_MAX_CHECK_INTERVAL,
    )


def _get_effective_check_interval() -> int:
    """Compute check interval with exponential backoff on consecutive empty results."""
    with _github_state_lock:
        return _get_effective_check_interval_locked()


def _check_sso_failures() -> None:
    """After a notification cycle, update consecutive counter and escalate if needed."""
    from app.github_notifications import (
        get_sso_failure_count,
        update_consecutive_sso_failures,
        check_sso_escalation,
        get_consecutive_sso_failures,
    )

    count = get_sso_failure_count()
    update_consecutive_sso_failures()

    if count == 0:
        return

    consecutive = get_consecutive_sso_failures()
    _github_log(
        f"SSO auth failure: {count} call(s) this cycle, "
        f"{consecutive} consecutive — "
        "run: gh auth refresh -h github.com -s read:org",
        "warning",
    )

    # Escalate to outbox after threshold (fires once per streak)
    check_sso_escalation()


def reset_github_backoff() -> None:
    """Reset backoff state. Useful for tests and when external events suggest activity."""
    global _last_github_check, _last_github_check_iso, _consecutive_empty_checks, _github_config_logged, _github_interval_loaded
    global _github_config_cache, _github_config_cache_mtime
    with _github_state_lock:
        _last_github_check = 0
        _last_github_check_iso = ""
        _consecutive_empty_checks = 0
        _github_config_logged = False
        _github_interval_loaded = False
        _github_config_cache = _GITHUB_CONFIG_UNSET
        _github_config_cache_mtime = 0
    with _notif_cache_lock:
        _notif_cache.clear()
    with _pending_error_replies_lock:
        _pending_error_replies.clear()


def _retry_failed_replies() -> None:
    """Retry previously failed GitHub error replies.

    Drains the pending queue and attempts each reply once. Replies that
    fail again are re-queued (up to _MAX_REPLY_RETRIES total attempts).
    """
    with _pending_error_replies_lock:
        if not _pending_error_replies:
            return
        batch = list(_pending_error_replies)
        _pending_error_replies.clear()

    if not batch:
        return

    from app.github_command_handler import post_error_reply

    for entry in batch:
        try:
            post_error_reply(
                entry["owner"], entry["repo"], entry["issue_num"],
                entry["comment_id"], entry["error"],
                comment_api_url=entry.get("comment_api_url", ""),
            )
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as e:
            attempts = entry.get("attempts", 1) + 1
            if attempts <= _MAX_REPLY_RETRIES:
                entry["attempts"] = attempts
                with _pending_error_replies_lock:
                    if len(_pending_error_replies) < _MAX_PENDING_REPLIES:
                        _pending_error_replies.append(entry)
            else:
                _github_log(
                    f"Dropping error reply after {attempts - 1} attempts "
                    f"({entry['owner']}/{entry['repo']}#{entry['issue_num']}): {e}",
                    "warning",
                )


def process_github_notifications(
    koan_root: str,
    instance_dir: str,
) -> int:
    """Check GitHub notifications and create missions from @mentions.

    Respects throttling with exponential backoff: starts at the configured
    check_interval_seconds (default 60s), doubles on each empty result
    (up to 300s), resets on finding notifications.

    Args:
        koan_root: Path to koan root directory.
        instance_dir: Path to instance directory.

    Returns:
        Number of missions created.
    """
    global _last_github_check, _last_github_check_iso, _consecutive_empty_checks, _GITHUB_CHECK_INTERVAL, _GITHUB_MAX_CHECK_INTERVAL, _github_interval_loaded

    # Load configured intervals on first call (lazy, avoids import-time config reads)
    with _github_state_lock:
        need_interval_load = not _github_interval_loaded

    if need_interval_load:
        try:
            from app.utils import load_config
            from app.github_config import get_github_check_interval, get_github_max_check_interval
            cfg = load_config()
            with _github_state_lock:
                _GITHUB_CHECK_INTERVAL = get_github_check_interval(cfg)
                _GITHUB_MAX_CHECK_INTERVAL = get_github_max_check_interval(cfg)
                _github_interval_loaded = True
        except (ImportError, OSError, ValueError) as e:
            log.debug("Could not load github check interval from config: %s", e)

    now = time.time()
    # Atomic check-then-act: verify throttle and claim the timeslot under lock.
    with _github_state_lock:
        effective_interval = _get_effective_check_interval_locked()
        if now - _last_github_check < effective_interval:
            return 0
        _last_github_check = now

    # Retry any previously failed error replies before processing new ones.
    _retry_failed_replies()

    try:
        from app.utils import load_config
        from app.projects_config import load_projects_config

        config = load_config()
        github_config = _load_github_config(config, koan_root, instance_dir)
        if not github_config:
            return 0

        log.debug(
            "GitHub: checking notifications (nickname=%s, bot_user=%s, max_age=%dh)",
            github_config.get("nickname", "?"),
            github_config.get("bot_username", "?"),
            github_config.get("max_age", 24),
        )

        # Load components
        registry = _build_skill_registry(instance_dir)
        known_repos = _get_known_repos_from_projects(koan_root)
        projects_config = load_projects_config(koan_root)

        # Fetch and process notifications
        from app.github_notifications import fetch_unread_notifications, mark_notification_read, reset_sso_failure_count
        reset_sso_failure_count()
        from app.github_command_handler import (
            process_single_notification,
            post_error_reply,
            resolve_project_from_notification,
            extract_issue_number_from_notification,
        )

        # Pass ``since`` so we also get notifications that were auto-read
        # by the GitHub web UI before we could poll them (race condition
        # when user posts @mention while viewing the PR page).
        #
        # On the first check of a new session, _last_github_check_iso is
        # empty.  Without a ``since``, only unread notifications are fetched,
        # missing any @mention that GitHub auto-read (user was viewing the
        # page when they posted).  Seed from max_age_hours so the first poll
        # covers the same window as subsequent ones.
        from datetime import datetime, timedelta, timezone

        with _github_state_lock:
            since_value = _last_github_check_iso or None
        if since_value is None:
            max_age = github_config.get("max_age", 24)
            since_value = (
                datetime.now(timezone.utc) - timedelta(hours=max_age)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            _github_log(
                f"Cold start: fetching notifications since {since_value} "
                f"(max_age={max_age}h lookback)"
            )

        result = fetch_unread_notifications(known_repos, since=since_value)
        notifications = result.actionable

        # Record the check timestamp for the next ``since`` window.
        new_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with _github_state_lock:
            _last_github_check_iso = new_iso

        if notifications:
            _github_log(f"Fetched {len(notifications)} actionable notification(s)")
        else:
            log.debug("GitHub: no actionable notifications found")

        # Filter out notifications we've already processed and cached.
        # Cache key is (thread_id, updated_at) so new activity on a thread
        # naturally invalidates the cache entry.
        uncached = [n for n in notifications if not _is_notif_cached(n)]
        cached_count = len(notifications) - len(uncached)
        if cached_count > 0:
            log.debug(
                "GitHub: skipped %d cached notification(s), processing %d",
                cached_count, len(uncached),
            )

        missions_created = 0
        for notif in uncached:
            _log_notification(notif)
            success, error = process_single_notification(
                notif, registry, config, projects_config,
                github_config.get("bot_username", ""),
                github_config.get("max_age", 24),
            )

            # Cache immediately after processing: prevents re-processing on
            # next cycle. Must happen before the error reply attempt so that
            # a reply failure doesn't cause the whole notification to be
            # re-processed (which could create duplicate missions).
            _cache_notif(notif)

            if success:
                missions_created += 1
                repo = notif.get("repository", {}).get("full_name", "?")
                title = notif.get("subject", {}).get("title", "?")
                _github_log(f"Mission queued from @mention on {repo}: {title}")
                _notify_mission_from_mention(notif)
            elif error:
                repo = notif.get("repository", {}).get("full_name", "?")
                _github_log(f"Notification error for {repo}: {error[:100]}", "warning")
                _post_error_for_notification(notif, error)

        # Drain non-actionable notifications (ci_activity, state_change,
        # etc.) to prevent accumulation that blocks future @mention detection.
        # When old notifications pile up on a thread, new @mentions may update
        # the existing notification instead of creating a fresh "mention" one.
        drained = _drain_notifications(result.drain)
        if drained > 0:
            log.debug("GitHub: drained %d non-actionable notification(s)", drained)

        # Check for SSO failures and alert if needed
        _check_sso_failures()

        # Update backoff state
        with _github_state_lock:
            if missions_created > 0 or notifications:
                _consecutive_empty_checks = 0
            else:
                _consecutive_empty_checks += 1
                if _consecutive_empty_checks > 1:
                    log.debug(
                        "GitHub: no notifications (%d consecutive), next check in %ds",
                        _consecutive_empty_checks,
                        _get_effective_check_interval_locked(),
                    )

        return missions_created

    except (ImportError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("GitHub notification check failed: %s", e)
        return 0


# Maximum non-actionable notifications to drain per check cycle.
# Prevents API overload on first run after a long accumulation period.
_MAX_DRAIN_PER_CYCLE = 30


def _drain_notifications(notifications: list) -> int:
    """Mark non-actionable notifications as read to prevent accumulation.

    Non-actionable notifications (ci_activity, state_change,
    etc.) pile up on threads the bot owns. When they stay unread, new @mentions
    on those threads may update the existing notification instead of creating a
    fresh "mention"-reason notification, causing @mentions to be missed.

    Rate-limited to _MAX_DRAIN_PER_CYCLE per call to avoid API overload.

    Returns:
        Number of notifications drained.
    """
    from app.github_notifications import mark_notification_read

    drained = 0
    for notif in notifications[:_MAX_DRAIN_PER_CYCLE]:
        thread_id = str(notif.get("id", ""))
        if thread_id:
            try:
                mark_notification_read(thread_id)
                drained += 1
            except Exception:
                log.warning("GitHub: failed to mark notification %s as read", thread_id)
    return drained


def _log_notification(notif: dict) -> None:
    """Log a received notification with console visibility."""
    repo_name = notif.get("repository", {}).get("full_name", "?")
    subject_title = notif.get("subject", {}).get("title", "?")
    subject_type = notif.get("subject", {}).get("type", "?")
    updated_at = notif.get("updated_at", "?")
    _github_log(
        f"Processing: {repo_name} {subject_type} \"{subject_title}\" (updated {updated_at})",
        "debug",
    )


def _notify_mission_from_mention(notif: dict) -> None:
    """Send a message to the communication layer when a GitHub @mention creates a mission."""
    try:
        from app.notify import send_telegram
        from app.github_notifications import api_url_to_web_url

        repo_name = notif.get("repository", {}).get("full_name", "?")
        subject_title = notif.get("subject", {}).get("title", "?")
        subject_type = notif.get("subject", {}).get("type", "?").lower()
        subject_api_url = notif.get("subject", {}).get("url", "")
        thread_url = api_url_to_web_url(subject_api_url) if subject_api_url else ""

        # Use annotated command/author from process_single_notification
        command_name = notif.get("_koan_command", "")
        author = notif.get("_koan_author", "")

        # Build descriptive title: "📬 GitHub @user → /rebase mission queued"
        author_part = f"@{author}" if author else "@mention"
        command_part = f" /{command_name}" if command_name else ""
        msg = (
            f"📬 GitHub {author_part} →{command_part} mission queued\n"
            f"{repo_name} ({subject_type}): {subject_title}"
        )
        if thread_url:
            msg += f"\n{thread_url}"
        from app.notify import NotificationPriority
        send_telegram(msg, priority=NotificationPriority.ACTION)
    except (ImportError, OSError) as e:
        log.debug("Failed to send notification message: %s", e)


def _post_error_for_notification(notif: dict, error: str) -> None:
    """Post error reply to a notification if possible.

    On failure, queues the reply for retry on the next notification cycle
    rather than silently dropping it.
    """
    from app.github_command_handler import (
        post_error_reply,
        resolve_project_from_notification,
        extract_issue_number_from_notification,
    )
    from app.github_notifications import get_comment_from_notification

    project_info = resolve_project_from_notification(notif)
    issue_num = extract_issue_number_from_notification(notif)

    if not project_info or not issue_num:
        return

    _, owner, repo = project_info

    try:
        comment = get_comment_from_notification(notif)
        if not comment:
            return
        comment_id = str(comment.get("id", ""))
        comment_api_url = comment.get("url", "")
        if not comment_id:
            return
        post_error_reply(owner, repo, issue_num, comment_id, error,
                         comment_api_url=comment_api_url)
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        _github_log(f"Error posting reply to GitHub, queuing for retry: {e}", "warning")
        entry = {
            "owner": owner, "repo": repo, "issue_num": issue_num,
            "comment_id": comment_id, "error": error,
            "comment_api_url": comment_api_url, "attempts": 1,
        }
        with _pending_error_replies_lock:
            if len(_pending_error_replies) < _MAX_PENDING_REPLIES:
                _pending_error_replies.append(entry)


# --- Interruptible sleep ---


def _check_signal_file(koan_root: str, filename: str) -> bool:
    """Check if a signal file (.koan-stop, .koan-pause, etc.) exists."""
    return os.path.isfile(os.path.join(koan_root, filename))


def check_pending_missions(instance_dir: str) -> bool:
    """Check if there are pending missions in missions.md."""
    try:
        content = (Path(instance_dir) / "missions.md").read_text()
        return count_pending(content) > 0
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as e:
        print(f"[loop_manager] Error reading missions.md: {e}", file=sys.stderr)
        return False


def interruptible_sleep(
    interval: int,
    koan_root: str,
    instance_dir: str,
    check_interval: int = 10,
) -> str:
    """Sleep for a given interval, waking early on events.

    Checks for stop, pause, restart, shutdown files, pending missions,
    and GitHub notifications every check_interval seconds.

    Args:
        interval: Total sleep duration in seconds.
        koan_root: Path to koan root directory.
        instance_dir: Path to instance directory.
        check_interval: How often to check for wake events (seconds).

    Returns:
        Reason for waking: "timeout", "mission", "stop", "pause", "restart", "shutdown".
    """
    elapsed = 0
    while elapsed < interval:
        # Check signals BEFORE sleeping so events are detected immediately.
        if check_pending_missions(instance_dir):
            return "mission"
        if _check_signal_file(koan_root, ".koan-stop"):
            return "stop"
        if _check_signal_file(koan_root, ".koan-pause"):
            return "pause"
        if _check_signal_file(koan_root, ".koan-restart"):
            return "restart"
        if _check_signal_file(koan_root, ".koan-shutdown"):
            return "shutdown"

        # Write run-loop heartbeat during sleep to signal liveness
        from app.health_check import write_run_heartbeat
        write_run_heartbeat(koan_root)

        # Feature tip: surface an unseen skill to the user (throttled)
        from app.feature_tips import maybe_send_feature_tip
        maybe_send_feature_tip(instance_dir)

        # Run periodic heartbeat checks (throttled to once per 30 min)
        from app.heartbeat import run_stale_mission_check, run_disk_space_check
        run_stale_mission_check(instance_dir)
        run_disk_space_check(koan_root)

        # Check GitHub notifications (throttled to once per 60s).
        # Track wall time: API calls can be slow and should count toward elapsed.
        t0 = time.monotonic()
        if process_github_notifications(koan_root, instance_dir) > 0:
            return "mission"
        elapsed += time.monotonic() - t0

        # Sleep for the smaller of check_interval and remaining time
        # to avoid overshooting the requested interval.
        remaining = interval - elapsed
        if remaining <= 0:
            break
        sleep_time = min(check_interval, remaining)
        time.sleep(sleep_time)
        elapsed += sleep_time

    return "timeout"


# --- CLI interface ---


def _cli_resolve_focus(args: list) -> None:
    """CLI: python -m app.loop_manager resolve-focus --mode <mode> [--has-mission]"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--has-mission", action="store_true")
    parsed = parser.parse_args(args)

    print(resolve_focus_area(parsed.mode, parsed.has_mission))


def _cli_create_pending(args: list) -> None:
    """CLI: python -m app.loop_manager create-pending ..."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--run-num", type=int, required=True)
    parser.add_argument("--max-runs", type=int, required=True)
    parser.add_argument("--autonomous-mode", default="implement")
    parser.add_argument("--mission-title", default="")
    parsed = parser.parse_args(args)

    path = create_pending_file(
        instance_dir=parsed.instance,
        project_name=parsed.project_name,
        run_num=parsed.run_num,
        max_runs=parsed.max_runs,
        autonomous_mode=parsed.autonomous_mode,
        mission_title=parsed.mission_title,
    )
    print(path)


def _cli_validate_projects(args: list) -> None:
    """CLI: python -m app.loop_manager validate-projects"""
    from app.utils import get_known_projects

    projects = get_known_projects()
    if not projects:
        print("No projects configured.", file=sys.stderr)
        sys.exit(1)

    error = validate_projects(projects)
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)

    for name, path in projects:
        print(f"{name}:{path}")


def _cli_lookup_project(args: list) -> None:
    """CLI: python -m app.loop_manager lookup-project --name <name>"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parsed = parser.parse_args(args)

    from app.utils import get_known_projects

    projects = get_known_projects()
    path = lookup_project(parsed.name, projects)
    if path is None:
        print(f"Unknown project: {parsed.name}", file=sys.stderr)
        print(format_project_list(projects), file=sys.stderr)
        sys.exit(1)

    print(path)


def _cli_interruptible_sleep(args: list) -> None:
    """CLI: python -m app.loop_manager interruptible-sleep ..."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, required=True)
    parser.add_argument("--koan-root", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--check-interval", type=int, default=10)
    parsed = parser.parse_args(args)

    reason = interruptible_sleep(
        interval=parsed.interval,
        koan_root=parsed.koan_root,
        instance_dir=parsed.instance,
        check_interval=parsed.check_interval,
    )
    print(reason)


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(
            "Usage: loop_manager.py <resolve-focus|create-pending|validate-projects|"
            "lookup-project|interruptible-sleep> [args]",
            file=sys.stderr,
        )
        sys.exit(1)

    subcommand = sys.argv[1]
    remaining = sys.argv[2:]

    commands = {
        "resolve-focus": _cli_resolve_focus,
        "create-pending": _cli_create_pending,
        "validate-projects": _cli_validate_projects,
        "lookup-project": _cli_lookup_project,
        "interruptible-sleep": _cli_interruptible_sleep,
    }

    handler = commands.get(subcommand)
    if handler is None:
        print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
        sys.exit(1)

    handler(remaining)


if __name__ == "__main__":
    main()
