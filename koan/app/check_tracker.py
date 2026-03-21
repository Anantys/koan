"""Track last-checked timestamps for /check skill.

Stores a simple JSON mapping of GitHub resource URLs to the `updated_at`
timestamp we last observed.  This lets /check skip resources that haven't
changed since the previous run — no GitHub noise, no wasted API calls.

Each URL entry may also carry an optional ``ci`` sub-key for CI recovery
state tracking (attempt count, last attempt timestamp, status).

File location: ``instance/.check-tracker.json``
"""

import fcntl
import json
from pathlib import Path


def _tracker_path(instance_dir):
    """Return path to the tracker file."""
    return Path(instance_dir) / ".check-tracker.json"


def _load(instance_dir):
    """Load the tracker data from disk.

    Returns:
        dict mapping URL strings to ``{"updated_at": str, "checked_at": str}``
        with an optional ``"ci"`` sub-key for CI recovery state.
    """
    path = _tracker_path(instance_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(instance_dir, data):
    """Persist tracker data to disk (atomic write)."""
    from app.utils import atomic_write

    path = _tracker_path(instance_dir)
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def get_last_checked(instance_dir, url):
    """Return the ``updated_at`` value we last recorded for *url*, or None."""
    data = _load(instance_dir)
    entry = data.get(url)
    if entry:
        return entry.get("updated_at")
    return None


def mark_checked(instance_dir, url, updated_at):
    """Record that we just checked *url* whose ``updated_at`` is *updated_at*.

    Args:
        instance_dir: Path to the instance directory.
        url: Canonical GitHub URL (PR or issue).
        updated_at: ISO-8601 timestamp from the GitHub API.
    """
    from datetime import datetime, timezone

    lock_path = Path(instance_dir) / ".check-tracker.lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _load(instance_dir)
            data[url] = {
                "updated_at": updated_at,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            _save(instance_dir, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def has_changed(instance_dir, url, current_updated_at):
    """Return True if the resource has been updated since we last checked.

    Also returns True if we've never checked this URL before.
    """
    last = get_last_checked(instance_dir, url)
    if last is None:
        return True
    return current_updated_at != last


# ---------------------------------------------------------------------------
# CI recovery state tracking
# ---------------------------------------------------------------------------

def get_ci_status(instance_dir, pr_url):
    """Return CI recovery state for a PR URL, or None if not tracked.

    Returns:
        dict with keys: status, attempt_count, last_attempt_at — or None.
    """
    data = _load(instance_dir)
    entry = data.get(pr_url)
    if entry:
        return entry.get("ci")
    return None


def set_ci_status(instance_dir, pr_url, status, attempt_count):
    """Persist CI recovery state for a PR.

    Args:
        instance_dir: Path to the instance directory.
        pr_url: Canonical GitHub PR URL.
        status: Recovery status string (e.g. "failed", "fix_dispatched").
        attempt_count: Number of fix attempts so far.
    """
    from datetime import datetime, timezone

    lock_path = Path(instance_dir) / ".check-tracker.lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _load(instance_dir)
            entry = data.setdefault(pr_url, {})
            entry["ci"] = {
                "status": status,
                "attempt_count": attempt_count,
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            }
            _save(instance_dir, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def get_ci_attempt_count(instance_dir, pr_url):
    """Return the number of CI fix attempts for a PR (0 if none recorded)."""
    ci = get_ci_status(instance_dir, pr_url)
    if ci is None:
        return 0
    return ci.get("attempt_count", 0)


def clear_ci_status(instance_dir, pr_url):
    """Remove CI recovery tracking for a PR (call on merge/close)."""
    lock_path = Path(instance_dir) / ".check-tracker.lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _load(instance_dir)
            entry = data.get(pr_url)
            if entry and "ci" in entry:
                del entry["ci"]
                _save(instance_dir, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
