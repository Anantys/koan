"""Bounded set with FIFO eviction.

A set-like container with a maximum size. When the set is full,
the oldest entries are evicted first (FIFO order).

Used for in-memory deduplication (e.g. processed comment IDs,
error reply keys) where unbounded growth would leak memory and
clearing all entries at once would lose dedup state.

Thread-safe: all operations are protected by a lock since instances
are used as global state accessed by worker threads (awake.py)
and the main loop concurrently.
"""

import threading


class BoundedSet:
    """Set with a maximum size and FIFO eviction.

    Backed by a dict (Python 3.7+ preserves insertion order)
    for O(1) membership test and FIFO eviction of oldest entries.

    Thread-safe via internal lock.

    Args:
        maxlen: Maximum number of entries. When full, adding a new
            entry evicts the oldest one.
    """

    __slots__ = ("_data", "_maxlen", "_lock")

    def __init__(self, maxlen: int = 10000):
        if maxlen < 1:
            raise ValueError(f"maxlen must be >= 1, got {maxlen}")
        self._data: dict = {}
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def add(self, item) -> None:
        """Add an item. Evicts the oldest entry if at capacity."""
        with self._lock:
            if item in self._data:
                return
            if len(self._data) >= self._maxlen:
                # Evict oldest (first key in insertion order)
                oldest = next(iter(self._data))
                del self._data[oldest]
            self._data[item] = None

    def __contains__(self, item) -> bool:
        with self._lock:
            return item in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._data.clear()

    def __repr__(self) -> str:
        with self._lock:
            return f"BoundedSet(maxlen={self._maxlen}, size={len(self._data)})"
