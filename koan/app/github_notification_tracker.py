"""Persistent tracker for processed GitHub notification comments.

Survives process restarts — prevents duplicate mission queueing when
GitHub reaction API fails (SSO, rate limits, network errors).

File location: ``instance/.koan-github-processed.json``
Format: ``{"<comment_id>": <epoch_timestamp>, ...}``
"""

import fcntl
import json
import time
from pathlib import Path


_TRACKER_FILE = ".koan-github-processed.json"
_LOCK_FILE = ".koan-github-processed.lock"
_TTL_SECONDS = 7 * 86400  # 7 days
_MAX_ENTRIES = 5000


def _tracker_path(instance_dir: str) -> Path:
    return Path(instance_dir) / _TRACKER_FILE


def _lock_path(instance_dir: str) -> Path:
    return Path(instance_dir) / _LOCK_FILE


def _load(instance_dir: str) -> dict:
    """Load tracker data, pruning expired entries."""
    path = _tracker_path(instance_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}
    except (json.JSONDecodeError, OSError):
        return {}
    # Prune expired
    now = time.time()
    return {k: v for k, v in data.items() if now - v < _TTL_SECONDS}


def _save(instance_dir: str, data: dict) -> None:
    from app.utils import atomic_write

    path = _tracker_path(instance_dir)
    atomic_write(path, json.dumps(data) + "\n")


def is_comment_tracked(instance_dir: str, comment_id: str) -> bool:
    """Check if a comment ID has been persistently recorded."""
    if not comment_id:
        return False
    data = _load(instance_dir)
    return comment_id in data


def track_comment(instance_dir: str, comment_id: str) -> None:
    """Record a comment ID as processed (with file lock for thread safety)."""
    if not comment_id:
        return
    lock = _lock_path(instance_dir)
    try:
        with open(lock, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = _load(instance_dir)
                data[comment_id] = time.time()
                # Cap entries — evict oldest beyond limit
                if len(data) > _MAX_ENTRIES:
                    sorted_items = sorted(data.items(), key=lambda x: x[1])
                    data = dict(sorted_items[-_MAX_ENTRIES:])
                _save(instance_dir, data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except OSError:
        pass  # Best-effort — don't break notification processing
