"""Tests for thread_subscriptions module."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def instance_dir(tmp_path):
    """Create a temporary instance directory."""
    d = tmp_path / "instance"
    d.mkdir()
    return d


class TestLoadSaveSubscriptions:
    def test_load_empty_when_missing(self, instance_dir):
        from app.thread_subscriptions import load_subscriptions

        assert load_subscriptions(instance_dir) == {}

    def test_save_and_load_roundtrip(self, instance_dir):
        from app.thread_subscriptions import load_subscriptions, save_subscriptions

        data = {
            "sukria/koan#777": {
                "last_replied_comment_id": 123,
                "last_checked_at": "2026-03-13T10:00:00",
                "pending_mission": False,
            }
        }
        save_subscriptions(instance_dir, data)
        loaded = load_subscriptions(instance_dir)
        assert loaded == data

    def test_load_handles_malformed_json(self, instance_dir):
        from app.thread_subscriptions import load_subscriptions

        path = instance_dir / "thread-subscriptions.json"
        path.write_text("not valid json {{{")
        assert load_subscriptions(instance_dir) == {}

    def test_load_handles_non_dict_json(self, instance_dir):
        from app.thread_subscriptions import load_subscriptions

        path = instance_dir / "thread-subscriptions.json"
        path.write_text('["not", "a", "dict"]')
        assert load_subscriptions(instance_dir) == {}

    def test_save_creates_parent_dirs(self, tmp_path):
        from app.thread_subscriptions import load_subscriptions, save_subscriptions

        nested = tmp_path / "deep" / "instance"
        save_subscriptions(nested, {"key": {"pending_mission": True}})
        assert load_subscriptions(nested) == {"key": {"pending_mission": True}}


class TestMarkReplied:
    def test_mark_replied_new_thread(self, instance_dir):
        from app.thread_subscriptions import load_subscriptions, mark_replied

        mark_replied(instance_dir, "sukria/koan#42", 999)
        data = load_subscriptions(instance_dir)
        entry = data["sukria/koan#42"]
        assert entry["last_replied_comment_id"] == 999
        assert entry["pending_mission"] is False
        assert "last_checked_at" in entry

    def test_mark_replied_clears_pending(self, instance_dir):
        from app.thread_subscriptions import (
            load_subscriptions,
            mark_replied,
            set_pending_mission,
        )

        set_pending_mission(instance_dir, "o/r#1", True)
        mark_replied(instance_dir, "o/r#1", 500)
        data = load_subscriptions(instance_dir)
        assert data["o/r#1"]["pending_mission"] is False
        assert data["o/r#1"]["last_replied_comment_id"] == 500


class TestPendingMission:
    def test_has_pending_false_when_missing(self, instance_dir):
        from app.thread_subscriptions import has_pending_mission

        assert has_pending_mission(instance_dir, "o/r#1") is False

    def test_set_and_check_pending(self, instance_dir):
        from app.thread_subscriptions import has_pending_mission, set_pending_mission

        set_pending_mission(instance_dir, "o/r#1", True)
        assert has_pending_mission(instance_dir, "o/r#1") is True

        set_pending_mission(instance_dir, "o/r#1", False)
        assert has_pending_mission(instance_dir, "o/r#1") is False


class TestGetLastRepliedCommentId:
    def test_returns_none_when_missing(self, instance_dir):
        from app.thread_subscriptions import get_last_replied_comment_id

        assert get_last_replied_comment_id(instance_dir, "o/r#1") is None

    def test_returns_id_after_mark(self, instance_dir):
        from app.thread_subscriptions import get_last_replied_comment_id, mark_replied

        mark_replied(instance_dir, "o/r#1", 42)
        assert get_last_replied_comment_id(instance_dir, "o/r#1") == 42


class TestMakeThreadKey:
    def test_format(self):
        from app.thread_subscriptions import make_thread_key

        assert make_thread_key("sukria", "koan", "777") == "sukria/koan#777"


class TestCleanupStale:
    def test_removes_old_entries(self, instance_dir):
        from app.thread_subscriptions import (
            cleanup_stale,
            load_subscriptions,
            save_subscriptions,
        )

        old_time = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
        recent_time = datetime.now().isoformat(timespec="seconds")
        data = {
            "old/repo#1": {"last_checked_at": old_time, "pending_mission": False},
            "new/repo#2": {"last_checked_at": recent_time, "pending_mission": False},
        }
        save_subscriptions(instance_dir, data)

        removed = cleanup_stale(instance_dir, max_age_days=30)
        assert removed == 1

        remaining = load_subscriptions(instance_dir)
        assert "old/repo#1" not in remaining
        assert "new/repo#2" in remaining

    def test_removes_entries_without_timestamp(self, instance_dir):
        from app.thread_subscriptions import (
            cleanup_stale,
            load_subscriptions,
            save_subscriptions,
        )

        data = {"no/time#1": {"pending_mission": False}}
        save_subscriptions(instance_dir, data)

        removed = cleanup_stale(instance_dir, max_age_days=30)
        assert removed == 1
        assert load_subscriptions(instance_dir) == {}

    def test_no_op_on_empty(self, instance_dir):
        from app.thread_subscriptions import cleanup_stale

        assert cleanup_stale(instance_dir) == 0
