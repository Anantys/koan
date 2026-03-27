"""Automatic update checker for Kōan.

Periodically checks if upstream has new commits and triggers
a pull + restart when updates are available.

Configuration (config.yaml):
    auto_update:
        enabled: true           # default: false
        check_interval: 10      # check every N iterations (default: 10)
        notify: true            # notify on Telegram before updating (default: true)

The check is lightweight (git fetch + rev-list count) and only
triggers a full pull when new commits are actually available.
"""

import time
from pathlib import Path
from typing import Optional

from app.run_log import log
from app.update_manager import (
    _find_upstream_remote,
    _run_git,
)


# Module-level cache to avoid fetching too often
_last_check_time: Optional[float] = None
_MIN_CHECK_INTERVAL_SECONDS = 120  # never check more than once per 2 min


def _load_auto_update_config() -> dict:
    """Load auto_update config section with defaults."""
    try:
        from app.utils import load_config
        config = load_config()
    except Exception as e:
        log("update", f"Config load failed, using defaults: {e}")
        config = {}
    section = config.get("auto_update", {})
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "check_interval": int(section.get("check_interval", 10)),
        "notify": bool(section.get("notify", True)),
    }


def is_auto_update_enabled() -> bool:
    """Check if auto-update is enabled in config."""
    return _load_auto_update_config()["enabled"]


def get_check_interval() -> int:
    """Get the iteration interval for update checks."""
    return _load_auto_update_config()["check_interval"]


def check_for_updates(koan_root: str) -> Optional[int]:
    """Check if upstream has new commits without pulling.

    Returns the number of commits ahead, or None on error.
    Caches the result to avoid hammering git fetch.
    """
    global _last_check_time
    now = time.monotonic()
    if _last_check_time is not None and now - _last_check_time < _MIN_CHECK_INTERVAL_SECONDS:
        return 0
    _last_check_time = now

    koan_path = Path(koan_root)
    remote = _find_upstream_remote(koan_path)
    if remote is None:
        log("update", "No upstream remote found, skipping update check")
        return None

    # Fetch upstream (lightweight, only refs)
    result = _run_git(["fetch", remote, "--quiet"], koan_path)
    if result.returncode != 0:
        log("update", f"Fetch failed: {result.stderr.strip()}")
        return None

    # Compare local main vs remote main
    result = _run_git(
        ["rev-list", "--count", f"main..{remote}/main"],
        koan_path,
    )
    if result.returncode != 0:
        log("update", f"Rev-list failed: {result.stderr.strip()}")
        return None

    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def perform_auto_update(koan_root: str, instance: str) -> bool:
    """Check for updates and trigger pull + restart if available.

    Returns True if an update was triggered (caller should exit).
    Returns False if no update needed or update failed.
    """
    config = _load_auto_update_config()
    if not config["enabled"]:
        return False

    commits_ahead = check_for_updates(koan_root)
    if not commits_ahead:
        return False

    log("update", f"Upstream has {commits_ahead} new commit(s). Pulling...")

    # Notify before updating
    if config["notify"]:
        try:
            from app.notify import format_and_send
            format_and_send(
                f"🔄 Auto-update: {commits_ahead} new commit(s) detected. "
                f"Pulling and restarting...",
                instance_dir=instance,
            )
        except Exception as e:
            log("error", f"Auto-update notification failed: {e}")

    # Pull
    from app.update_manager import pull_upstream
    result = pull_upstream(Path(koan_root))

    if not result.success:
        log("error", f"Auto-update pull failed: {result.error}")
        if config["notify"]:
            try:
                from app.notify import format_and_send
                format_and_send(
                    f"❌ Auto-update failed: {result.error}",
                    instance_dir=instance,
                )
            except Exception as e:
                log("error", f"Failed to notify pull failure: {e}")
        return False

    log("update", result.summary())

    if not result.changed:
        return False

    # Trigger restart
    from app.restart_manager import request_restart
    from app.pause_manager import remove_pause
    remove_pause(koan_root)
    request_restart(koan_root)

    if config["notify"]:
        try:
            from app.notify import format_and_send
            msg = f"✅ {result.summary()}\nRestarting..."
            if result.stashed:
                msg += "\n⚠️ Dirty work was auto-stashed."
            format_and_send(msg, instance_dir=instance)
        except Exception as e:
            log("error", f"Failed to notify auto-update success: {e}")

    return True


def reset_check_cache():
    """Reset the check cache (for testing)."""
    global _last_check_time
    _last_check_time = None
