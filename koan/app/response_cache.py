"""In-memory TTL cache for avoiding redundant Claude API calls.

Thread-safe cache with TTL expiry and max-entries eviction.
Each process (run.py, awake.py) gets its own instance since they're
separate processes. No file I/O — pure in-memory.

Follows the _commits_cache pattern from session_tracker.py but is
generic enough for multiple callsites.
"""

import threading
import time
from typing import Dict, Optional, Tuple


# Default settings
DEFAULT_TTL = 1800  # 30 minutes
DEFAULT_MAX_ENTRIES = 500


class TTLCache:
    """Thread-safe in-memory cache with TTL expiry and LRU eviction.

    Usage:
        cache = TTLCache(max_entries=500)
        cache.put("key", "value", ttl=900)
        result = cache.get("key")  # returns "value" or None if expired
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # key -> (value, expiry_monotonic)
        self._store: Dict[str, Tuple[str, float]] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[str]:
        """Return cached value or None if expired/missing."""
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                value, expiry = entry
                if time.monotonic() < expiry:
                    self._hits += 1
                    return value
                # Expired — remove it
                del self._store[key]
            self._misses += 1
            return None

    def put(self, key: str, value: str, ttl: int = DEFAULT_TTL) -> None:
        """Store a value with TTL in seconds."""
        expiry = time.monotonic() + ttl
        with self._lock:
            self._store[key] = (value, expiry)
            if len(self._store) > self._max_entries:
                self._evict()

    def clear(self) -> None:
        """Flush all entries and reset stats."""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        """Return hit/miss counts for observability."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
            }

    def _evict(self) -> None:
        """Evict expired entries first, then oldest by expiry (LRU-like).

        Must be called with self._lock held.
        """
        now = time.monotonic()

        # First pass: remove expired entries
        expired = [k for k, (_, exp) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]

        # If still over limit, remove entries with earliest expiry
        if len(self._store) > self._max_entries:
            by_expiry = sorted(self._store.items(), key=lambda item: item[1][1])
            to_remove = len(self._store) - self._max_entries
            for k, _ in by_expiry[:to_remove]:
                del self._store[k]


# Module-level singleton for format_message caching
_format_cache = TTLCache()


def get_format_cache() -> TTLCache:
    """Return the module-level format message cache singleton."""
    return _format_cache
