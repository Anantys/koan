"""Thread subscription state tracker.

Manages a JSON file that tracks which GitHub threads Kōan is monitoring
for new comments. Used by the subscribe feature to avoid duplicate
mission queuing and to remember which comments have been replied to.

State file: instance/thread-subscriptions.json
Thread key format: "owner/repo#number" (e.g., "sukria/koan#777")

File structure:
    {
        "sukria/koan#777": {
            "last_replied_comment_id": 12345,
            "last_checked_at": "2026-03-13T10:00:00",
            "pending_mission": false
        }
    }
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.utils import atomic_write

log = logging.getLogger(__name__)

_STATE_FILENAME = "thread-subscriptions.json"
_DEFAULT_MAX_AGE_DAYS = 30


def _state_path(instance_dir: Path) -> Path:
    """Return the path to the thread subscriptions state file."""
    return instance_dir / _STATE_FILENAME


def load_subscriptions(instance_dir: Path) -> dict:
    """Load thread subscription state from JSON file.

    Returns empty dict on missing or malformed file.
    """
    path = _state_path(instance_dir)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            log.warning("thread-subscriptions.json is not a dict — resetting")
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load thread-subscriptions.json: %s", e)
        return {}


def save_subscriptions(instance_dir: Path, data: dict) -> None:
    """Atomically write thread subscription state to JSON file."""
    path = _state_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, sort_keys=True)
    atomic_write(path, content)


def mark_replied(
    instance_dir: Path,
    thread_key: str,
    comment_id: int,
) -> None:
    """Update state after a successful reply.

    Sets last_replied_comment_id and clears pending_mission flag.
    """
    data = load_subscriptions(instance_dir)
    entry = data.get(thread_key, {})
    entry["last_replied_comment_id"] = comment_id
    entry["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
    entry["pending_mission"] = False
    data[thread_key] = entry
    save_subscriptions(instance_dir, data)


def has_pending_mission(instance_dir: Path, thread_key: str) -> bool:
    """Check if a reply mission is already queued for this thread."""
    data = load_subscriptions(instance_dir)
    entry = data.get(thread_key, {})
    return bool(entry.get("pending_mission", False))


def set_pending_mission(
    instance_dir: Path,
    thread_key: str,
    pending: bool,
) -> None:
    """Toggle the pending_mission flag for a thread."""
    data = load_subscriptions(instance_dir)
    entry = data.get(thread_key, {})
    entry["pending_mission"] = pending
    entry["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
    data[thread_key] = entry
    save_subscriptions(instance_dir, data)


def get_last_replied_comment_id(
    instance_dir: Path,
    thread_key: str,
) -> Optional[int]:
    """Get the last comment ID that was replied to in a thread."""
    data = load_subscriptions(instance_dir)
    entry = data.get(thread_key, {})
    return entry.get("last_replied_comment_id")


def make_thread_key(owner: str, repo: str, number: str) -> str:
    """Build a thread key from owner, repo, and issue/PR number."""
    return f"{owner}/{repo}#{number}"


def cleanup_stale(
    instance_dir: Path,
    max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
) -> int:
    """Remove entries older than max_age_days.

    Returns the number of entries removed.
    """
    data = load_subscriptions(instance_dir)
    if not data:
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    stale_keys = []

    for key, entry in data.items():
        last_checked = entry.get("last_checked_at", "")
        if not last_checked:
            stale_keys.append(key)
            continue
        try:
            checked_at = datetime.fromisoformat(last_checked)
            if checked_at < cutoff:
                stale_keys.append(key)
        except ValueError:
            stale_keys.append(key)

    if stale_keys:
        for key in stale_keys:
            del data[key]
        save_subscriptions(instance_dir, data)

    return len(stale_keys)
