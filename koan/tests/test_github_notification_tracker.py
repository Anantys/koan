"""Tests for github_notification_tracker — persistent comment dedup."""

import json
import time

import pytest

from app.github_notification_tracker import (
    _MAX_ENTRIES,
    _TTL_SECONDS,
    _tracker_path,
    is_comment_tracked,
    track_comment,
)


@pytest.fixture()
def instance_dir(tmp_path):
    return str(tmp_path)


def test_track_and_check(instance_dir):
    assert not is_comment_tracked(instance_dir, "123")
    track_comment(instance_dir, "123")
    assert is_comment_tracked(instance_dir, "123")


def test_empty_comment_id(instance_dir):
    track_comment(instance_dir, "")
    assert not is_comment_tracked(instance_dir, "")


def test_survives_reload(instance_dir):
    """Simulates process restart — data persists on disk."""
    track_comment(instance_dir, "abc")
    # Read directly from file to confirm persistence
    data = json.loads(_tracker_path(instance_dir).read_text())
    assert "abc" in data


def test_ttl_expiry(instance_dir):
    """Expired entries are pruned on load."""
    path = _tracker_path(instance_dir)
    old_ts = time.time() - _TTL_SECONDS - 1
    path.write_text(json.dumps({"old": old_ts, "fresh": time.time()}))

    assert not is_comment_tracked(instance_dir, "old")
    assert is_comment_tracked(instance_dir, "fresh")


def test_max_entries_cap(instance_dir):
    """Oldest entries are evicted when cap is exceeded."""
    now = time.time()
    data = {str(i): now - (_MAX_ENTRIES - i) for i in range(_MAX_ENTRIES)}
    _tracker_path(instance_dir).write_text(json.dumps(data))

    # Adding one more should evict the oldest
    track_comment(instance_dir, "new_entry")
    result = json.loads(_tracker_path(instance_dir).read_text())
    assert len(result) == _MAX_ENTRIES
    assert "new_entry" in result
    # Entry "0" had the oldest timestamp, should be evicted
    assert "0" not in result


def test_corrupt_file_handled(instance_dir):
    """Corrupt JSON is treated as empty tracker."""
    _tracker_path(instance_dir).write_text("not json{{{")
    assert not is_comment_tracked(instance_dir, "123")
    # Can still write
    track_comment(instance_dir, "123")
    assert is_comment_tracked(instance_dir, "123")


def test_multiple_comments(instance_dir):
    track_comment(instance_dir, "a")
    track_comment(instance_dir, "b")
    track_comment(instance_dir, "c")
    assert is_comment_tracked(instance_dir, "a")
    assert is_comment_tracked(instance_dir, "b")
    assert is_comment_tracked(instance_dir, "c")
    assert not is_comment_tracked(instance_dir, "d")
