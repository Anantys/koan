"""Tests for response_cache module — TTL cache with eviction and thread safety."""

import threading
import time
from unittest.mock import patch

import pytest

from app.response_cache import TTLCache, get_format_cache, _format_cache


@pytest.fixture(autouse=True)
def _clear_singleton_cache():
    """Reset module-level cache between tests."""
    _format_cache.clear()
    yield
    _format_cache.clear()


class TestTTLCache:
    def test_put_and_get(self):
        cache = TTLCache()
        cache.put("k1", "v1")
        assert cache.get("k1") == "v1"

    def test_get_missing_key_returns_none(self):
        cache = TTLCache()
        assert cache.get("nonexistent") is None

    def test_ttl_expiry(self):
        cache = TTLCache()
        with patch("app.response_cache.time.monotonic", side_effect=[100.0, 100.0, 200.0]):
            cache.put("k1", "v1", ttl=10)
            # First get: monotonic returns 100, not expired (100 < 110)
            # Wait — we need monotonic to return value < expiry on get
            # put sets expiry = 100 + 10 = 110
            # Second call (first get): 100 < 110 → hit
            assert cache.get("k1") == "v1"
            # Third call (second get): 200 >= 110 → expired
            assert cache.get("k1") is None

    def test_expired_entry_removed_on_get(self):
        cache = TTLCache()
        with patch("app.response_cache.time.monotonic", side_effect=[0.0, 0.0, 100.0]):
            cache.put("k1", "v1", ttl=5)
            assert cache.get("k1") == "v1"  # monotonic=0, expiry=5 → hit
            assert cache.get("k1") is None  # monotonic=100 → expired
        assert cache.stats()["size"] == 0

    def test_max_entries_eviction(self):
        cache = TTLCache(max_entries=3)
        cache.put("a", "1", ttl=100)
        cache.put("b", "2", ttl=200)
        cache.put("c", "3", ttl=300)
        # Adding 4th should evict the one with earliest expiry ("a")
        cache.put("d", "4", ttl=400)
        assert cache.get("a") is None
        assert cache.get("b") == "2"
        assert cache.get("d") == "4"
        assert cache.stats()["size"] == 3

    def test_eviction_prefers_expired_entries(self):
        cache = TTLCache(max_entries=2)
        # Manually insert an expired entry
        with patch("app.response_cache.time.monotonic", return_value=0.0):
            cache.put("old", "stale", ttl=1)
        with patch("app.response_cache.time.monotonic", return_value=100.0):
            cache.put("new1", "fresh1", ttl=100)
            cache.put("new2", "fresh2", ttl=200)
        # "old" should have been evicted (expired), both new ones kept
        assert cache.stats()["size"] == 2
        with patch("app.response_cache.time.monotonic", return_value=100.0):
            assert cache.get("new1") == "fresh1"
            assert cache.get("new2") == "fresh2"

    def test_clear_resets_everything(self):
        cache = TTLCache()
        cache.put("k", "v")
        cache.get("k")  # hit
        cache.get("miss")  # miss
        cache.clear()
        assert cache.get("k") is None  # miss after clear
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 1  # the get("k") above
        assert stats["size"] == 0

    def test_stats_tracking(self):
        cache = TTLCache()
        cache.put("k1", "v1")
        cache.get("k1")  # hit
        cache.get("k1")  # hit
        cache.get("missing")  # miss
        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["size"] == 1

    def test_overwrite_existing_key(self):
        cache = TTLCache()
        cache.put("k", "old")
        cache.put("k", "new")
        assert cache.get("k") == "new"
        assert cache.stats()["size"] == 1

    def test_thread_safety(self):
        """Concurrent reads and writes should not raise or corrupt data."""
        cache = TTLCache(max_entries=100)
        errors = []

        def writer(prefix, count):
            try:
                for i in range(count):
                    cache.put(f"{prefix}_{i}", f"val_{i}", ttl=60)
            except Exception as e:
                errors.append(e)

        def reader(prefix, count):
            try:
                for i in range(count):
                    cache.get(f"{prefix}_{i}")
            except Exception as e:
                errors.append(e)

        threads = []
        for t in range(4):
            threads.append(threading.Thread(target=writer, args=(f"w{t}", 50)))
            threads.append(threading.Thread(target=reader, args=(f"w{t}", 50)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        # Cache should have entries (exact count varies due to eviction)
        assert cache.stats()["size"] <= 100

    def test_empty_string_value(self):
        cache = TTLCache()
        cache.put("k", "")
        assert cache.get("k") == ""


class TestGetFormatCache:
    def test_returns_singleton(self):
        c1 = get_format_cache()
        c2 = get_format_cache()
        assert c1 is c2

    def test_singleton_is_module_level(self):
        assert get_format_cache() is _format_cache
