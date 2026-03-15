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

    return None


def lookup_project(project_name: str, projects: list) -> Optional[str]:
    """Find project path by name.

    Args:
        project_name: Name to look up.
        projects: List of (name, path) tuples.

    Returns:
        Project path if found, None otherwise.
    """
    for name, path in projects:
        if name == project_name:
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
# Thread-safe via _github_config_lock.
_GITHUB_CONFIG_UNSET = object()  # sentinel: "no cached value yet"
_github_config_cache = _GITHUB_CONFIG_UNSET
_github_config_cache_mtime: float = 0
_github_config_lock = threading.Lock()

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

    # Check mtime-based cache: return cached result if config file hasn't changed
    current_mtime = _get_config_mtime(koan_root)
    with _github_config_lock:
        if _github_config_cache is not _GITHUB_CONFIG_UNSET and current_mtime == _github_config_cache_mtime:
            return _github_config_cache

    from app.github_config import get_github_commands_enabled, get_github_max_age_hours, get_github_nickname

    if not get_github_commands_enabled(config):
        if not _github_config_logged:
            _github_log("Commands disabled (github.commands_enabled not set in config.yaml)", "debug")
            _github_config_logged = True
        with _github_config_lock:
            _github_config_cache_mtime = current_mtime
            _github_config_cache = None
        return None

    nickname = get_github_nickname(config)
    if not nickname:
        if not _github_config_logged:
            _github_log("Commands enabled but github.nickname is not set — skipping", "warning")
            _github_config_logged = True
        with _github_config_lock:
            _github_config_cache_mtime = current_mtime
            _github_config_cache = None
        return None

    bot_username = os.environ.get("GITHUB_USER", nickname)
    max_age = get_github_max_age_hours(config)

    if not _github_config_logged:
        _github_log(f"Monitoring @{nickname} mentions (bot_user={bot_username}, max_age={max_age}h)")
        _github_config_logged = True

    result = {
        "nickname": nickname,
        "bot_username": bot_username,
        "max_age": max_age,
    }
    with _github_config_lock:
        _github_config_cache = result
        _github_config_cache_mtime = current_mtime
    return result


# Module-level cache for the GitHub notification skill registry.
# _build_skill_registry() is called every ~30s cycle; caching avoids
# rebuilding from filesystem each time.
_gh_cached_registry = None
_gh_cached_extra_dirs: Optional[tuple] = None


def _build_skill_registry(instance_dir: str):
    """Build combined skill registry from core and instance skills.

    Uses a module-level cache to avoid rebuilding from filesystem on
    every GitHub notification polling cycle (~30s).

    Returns:
        Populated SkillRegistry
    """
    global _gh_cached_registry, _gh_cached_extra_dirs
    from app.skills import build_registry

    instance_skills = Path(instance_dir) / "skills"
    extra = tuple(p for p in [instance_skills] if p.is_dir())

    if _gh_cached_registry is None or extra != _gh_cached_extra_dirs:
        _gh_cached_registry = build_registry(list(extra))
        _gh_cached_extra_dirs = extra

    return _gh_cached_registry


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


def _get_effective_check_interval() -> int:
    """Compute check interval with exponential backoff on consecutive empty results."""
    if _consecutive_empty_checks <= 0:
        return _GITHUB_CHECK_INTERVAL
    return min(
        _GITHUB_CHECK_INTERVAL * (2 ** _consecutive_empty_checks),
        _GITHUB_MAX_CHECK_INTERVAL,
    )


def reset_github_backoff() -> None:
    """Reset backoff state. Useful for tests and when external events suggest activity."""
    global _last_github_check, _last_github_check_iso, _consecutive_empty_checks, _github_config_logged, _github_interval_loaded
    global _github_config_cache, _github_config_cache_mtime
    _last_github_check = 0
    _last_github_check_iso = ""
    _consecutive_empty_checks = 0
    _github_config_logged = False
    _github_interval_loaded = False
    with _github_config_lock:
        _github_config_cache = _GITHUB_CONFIG_UNSET
        _github_config_cache_mtime = 0


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
    if not _github_interval_loaded:
        try:
            from app.utils import load_config
            from app.github_config import get_github_check_interval, get_github_max_check_interval
            cfg = load_config()
            _GITHUB_CHECK_INTERVAL = get_github_check_interval(cfg)
            _GITHUB_MAX_CHECK_INTERVAL = get_github_max_check_interval(cfg)
            _github_interval_loaded = True
        except (ImportError, OSError, ValueError) as e:
            log.debug("Could not load github check interval from config: %s", e)

    now = time.time()
    effective_interval = _get_effective_check_interval()
    if now - _last_github_check < effective_interval:
        return 0

    _last_github_check = now

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
        from app.github_notifications import fetch_unread_notifications, mark_notification_read
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
        _last_github_check_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if notifications:
            _github_log(f"Fetched {len(notifications)} actionable notification(s)")
        else:
            log.debug("GitHub: no actionable notifications found")

        missions_created = 0
        for notif in notifications:
            _log_notification(notif)
            success, error = process_single_notification(
                notif, registry, config, projects_config,
                github_config.get("bot_username", ""),
                github_config.get("max_age", 24),
            )

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

        # Drain non-actionable notifications (ci_activity, review_requested,
        # etc.) to prevent accumulation that blocks future @mention detection.
        # When old notifications pile up on a thread, new @mentions may update
        # the existing notification instead of creating a fresh "mention" one.
        drained = _drain_notifications(result.drain)
        if drained > 0:
            log.debug("GitHub: drained %d non-actionable notification(s)", drained)

        # Update backoff state
        if missions_created > 0 or notifications:
            _consecutive_empty_checks = 0
        else:
            _consecutive_empty_checks += 1
            if _consecutive_empty_checks > 1:
                log.debug(
                    "GitHub: no notifications (%d consecutive), next check in %ds",
                    _consecutive_empty_checks,
                    _get_effective_check_interval(),
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

    Non-actionable notifications (ci_activity, review_requested, state_change,
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
            mark_notification_read(thread_id)
            drained += 1
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
        msg = (
            f"📬 GitHub @mention → mission queued\n"
            f"{repo_name} ({subject_type}): {subject_title}"
        )
        if thread_url:
            msg += f"\n{thread_url}"
        send_telegram(msg)
    except (ImportError, OSError) as e:
        log.debug("Failed to send notification message: %s", e)


def _post_error_for_notification(notif: dict, error: str) -> None:
    """Post error reply to a notification if possible."""
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
        if comment:
            comment_id = str(comment.get("id", ""))
            comment_api_url = comment.get("url", "")
            if comment_id:
                post_error_reply(owner, repo, issue_num, comment_id, error,
                                 comment_api_url=comment_api_url)
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"[loop_manager] Error posting reply to GitHub: {e}", file=sys.stderr)


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
