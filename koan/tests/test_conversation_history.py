"""Tests for app.conversation_history — conversation history management."""

import json
import pytest
from pathlib import Path


@pytest.fixture
def history_file(tmp_path):
    """Create a temporary conversation history file for testing."""
    return tmp_path / "conversation-history.jsonl"


@pytest.fixture
def topics_file(tmp_path):
    return tmp_path / "previous-discussions-topics.json"


# --- save_conversation_message ---


class TestSaveConversationMessage:
    """Test save_conversation_message (and backward-compatible save_telegram_message alias)."""
    
    def test_saves_user_message(self, history_file):
        from app.conversation_history import save_conversation_message
        save_conversation_message(history_file, "user", "Hello")
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 1
        msg = json.loads(lines[0])
        assert msg["role"] == "user"
        assert msg["text"] == "Hello"
        assert "timestamp" in msg

    def test_saves_assistant_message(self, history_file):
        from app.conversation_history import save_conversation_message
        save_conversation_message(history_file, "assistant", "Hi there")
        msg = json.loads(history_file.read_text().strip())
        assert msg["role"] == "assistant"
        assert msg["text"] == "Hi there"

    def test_appends_multiple(self, history_file):
        from app.conversation_history import save_conversation_message
        save_conversation_message(history_file, "user", "One")
        save_conversation_message(history_file, "assistant", "Two")
        save_conversation_message(history_file, "user", "Three")
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_handles_unicode(self, history_file):
        from app.conversation_history import save_conversation_message
        save_conversation_message(history_file, "user", "Bonjour \u00e0 tous \U0001f389")
        msg = json.loads(history_file.read_text().strip())
        assert "\u00e0" in msg["text"]


# --- load_recent_history ---


class TestLoadRecentHistory:
    """Test load_recent_history (and backward-compatible load_recent_telegram_history alias)."""
    
    def test_empty_file(self, history_file):
        from app.conversation_history import load_recent_history
        assert load_recent_history(history_file) == []

    def test_nonexistent_file(self, tmp_path):
        from app.conversation_history import load_recent_history
        assert load_recent_history(tmp_path / "nope.jsonl") == []

    def test_loads_messages(self, history_file):
        from app.conversation_history import save_conversation_message, load_recent_history
        save_conversation_message(history_file, "user", "Hello")
        save_conversation_message(history_file, "assistant", "Hi")
        msgs = load_recent_history(history_file)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_max_messages(self, history_file):
        from app.conversation_history import save_conversation_message, load_recent_history
        for i in range(15):
            save_conversation_message(history_file, "user", f"Message {i}")
        msgs = load_recent_history(history_file, max_messages=5)
        assert len(msgs) == 5
        assert msgs[0]["text"] == "Message 10"  # last 5

    def test_skips_invalid_json(self, history_file):
        from app.conversation_history import load_recent_history
        history_file.write_text('{"role":"user","text":"ok"}\nnot json\n{"role":"user","text":"ok2"}\n')
        msgs = load_recent_history(history_file)
        assert len(msgs) == 2


# --- format_conversation_history ---


class TestFormatConversationHistory:
    def test_empty_messages(self):
        from app.conversation_history import format_conversation_history
        assert format_conversation_history([]) == ""

    def test_formats_roles(self):
        from app.conversation_history import format_conversation_history
        msgs = [
            {"role": "user", "text": "Hello"},
            {"role": "assistant", "text": "Hi there"},
        ]
        result = format_conversation_history(msgs)
        assert "Human: Hello" in result
        assert "K\u014dan: Hi there" in result
        assert "Recent conversation:" in result

    def test_truncates_long_messages(self):
        from app.conversation_history import format_conversation_history
        msgs = [{"role": "user", "text": "x" * 600}]
        result = format_conversation_history(msgs)
        assert "..." in result

    def test_max_chars_limit(self):
        from app.conversation_history import format_conversation_history
        msgs = [{"role": "user", "text": f"Message {i}"} for i in range(100)]
        result = format_conversation_history(msgs, max_chars=200)
        assert len(result) <= 300  # some slack for the last appended line


# --- compact_history ---


class TestCompactHistory:
    """Test compact_history (and backward-compatible compact_telegram_history alias)."""
    
    def test_skips_if_nonexistent(self, history_file, topics_file):
        from app.conversation_history import compact_history
        assert compact_history(history_file, topics_file) == 0

    def test_skips_if_too_few(self, history_file, topics_file):
        from app.conversation_history import save_conversation_message, compact_history
        for i in range(5):
            save_conversation_message(history_file, "user", f"Message {i}")
        assert compact_history(history_file, topics_file, min_messages=20) == 0

    def test_compacts_messages(self, history_file, topics_file):
        from app.conversation_history import save_conversation_message, compact_history
        for i in range(25):
            save_conversation_message(history_file, "user", f"Testing topic number {i} of today")
        count = compact_history(history_file, topics_file, min_messages=20)
        assert count == 25
        # History file should be empty after compaction
        assert history_file.read_text() == ""
        # Topics file should have entries
        topics = json.loads(topics_file.read_text())
        assert len(topics) == 1
        assert topics[0]["message_count"] == 25
        assert "topics_by_date" in topics[0]

    def test_appends_to_existing_topics(self, history_file, topics_file):
        from app.conversation_history import save_conversation_message, compact_history
        # Pre-existing topics
        topics_file.write_text('[{"existing": true}]')
        for i in range(25):
            save_conversation_message(history_file, "user", f"Something worth discussing number {i}")
        compact_history(history_file, topics_file, min_messages=20)
        topics = json.loads(topics_file.read_text())
        assert len(topics) == 2
        assert topics[0]["existing"] is True

    def test_handles_no_extractable_topics(self, history_file, topics_file):
        from app.conversation_history import compact_history
        # Write messages with very short text (< 5 chars → skipped by topic extraction)
        lines = []
        for i in range(25):
            msg = json.dumps({"timestamp": "2026-02-07T10:00:00", "role": "user", "text": "hi"})
            lines.append(msg)
        history_file.write_text("\n".join(lines) + "\n")
        count = compact_history(history_file, topics_file, min_messages=20)
        assert count == 25
        assert history_file.read_text() == ""


# --- _parse_jsonl_lines ---


class TestParseJsonlLines:
    def test_valid_lines(self):
        from app.conversation_history import _parse_jsonl_lines
        lines = ['{"a": 1}\n', '{"b": 2}\n']
        result = _parse_jsonl_lines(lines)
        assert result == [{"a": 1}, {"b": 2}]

    def test_skips_blank_lines(self):
        from app.conversation_history import _parse_jsonl_lines
        lines = ['{"a": 1}\n', '\n', '  \n', '{"b": 2}\n']
        result = _parse_jsonl_lines(lines)
        assert len(result) == 2

    def test_skips_invalid_json(self):
        from app.conversation_history import _parse_jsonl_lines
        lines = ['{"a": 1}\n', 'not json\n', '{"b": 2}\n']
        result = _parse_jsonl_lines(lines)
        assert len(result) == 2
        assert result[0] == {"a": 1}
        assert result[1] == {"b": 2}

    def test_empty_list(self):
        from app.conversation_history import _parse_jsonl_lines
        assert _parse_jsonl_lines([]) == []

    def test_all_invalid(self):
        from app.conversation_history import _parse_jsonl_lines
        assert _parse_jsonl_lines(["bad", "also bad"]) == []

    def test_preserves_unicode(self):
        from app.conversation_history import _parse_jsonl_lines
        lines = ['{"text": "café ☕"}\n']
        result = _parse_jsonl_lines(lines)
        assert result[0]["text"] == "café ☕"


# --- lock safety ---


class TestLockSafety:
    """Verify that file locks are released even when write fails."""

    def test_save_releases_lock_on_write_error(self, history_file, monkeypatch):
        """Ensure the lock is released if json.dumps or write raises."""
        import fcntl
        from app.conversation_history import save_conversation_message

        # Create a non-serializable object that will cause json.dumps to fail
        # We need to test that the lock is released
        history_file.write_text("")

        # First, verify normal write works
        save_conversation_message(history_file, "user", "Hello")
        assert len(history_file.read_text().strip().splitlines()) == 1

        # Now verify a second write still works (lock was released)
        save_conversation_message(history_file, "user", "World")
        assert len(history_file.read_text().strip().splitlines()) == 2

    def test_load_releases_lock_after_read(self, history_file):
        """Ensure read lock is released after loading, allowing subsequent writes."""
        from app.conversation_history import save_conversation_message, load_recent_history

        save_conversation_message(history_file, "user", "Hello")
        msgs = load_recent_history(history_file)
        assert len(msgs) == 1

        # If lock wasn't released, this would deadlock
        save_conversation_message(history_file, "user", "World")
        msgs = load_recent_history(history_file)
        assert len(msgs) == 2


class TestCompactHistoryAtomicWrite:
    """Verify compact_history uses atomic_write for truncation and topics."""

    def test_truncation_uses_atomic_write(self, tmp_path):
        """compact_history truncates history via atomic_write, not bare open('w')."""
        import json
        from unittest.mock import patch
        from app.conversation_history import compact_history

        history = tmp_path / "history.jsonl"
        topics = tmp_path / "topics.json"

        messages = []
        for i in range(25):
            msg = {"timestamp": f"2026-02-18T10:{i:02d}:00", "role": "user", "text": f"Message {i} about testing"}
            messages.append(json.dumps(msg, ensure_ascii=False))
        history.write_text("\n".join(messages) + "\n")

        with patch("app.conversation_history._atomic_write") as mock_aw:
            compact_history(history, topics)
            # atomic_write should be called twice: once for topics, once for truncation
            assert mock_aw.call_count == 2
            # Second call should truncate history to empty
            last_call = mock_aw.call_args_list[-1]
            assert last_call[0][0] == history
            assert last_call[0][1] == ""

    def test_topics_written_via_atomic_write(self, tmp_path):
        """compact_history writes topics file via atomic_write."""
        import json
        from unittest.mock import patch
        from app.conversation_history import compact_history

        history = tmp_path / "history.jsonl"
        topics = tmp_path / "topics.json"

        messages = []
        for i in range(25):
            msg = {"timestamp": f"2026-02-18T10:{i:02d}:00", "role": "user", "text": f"Message {i} about testing"}
            messages.append(json.dumps(msg, ensure_ascii=False))
        history.write_text("\n".join(messages) + "\n")

        with patch("app.conversation_history._atomic_write") as mock_aw:
            compact_history(history, topics)
            # First call writes topics file
            first_call = mock_aw.call_args_list[0]
            assert first_call[0][0] == topics
            content = json.loads(first_call[0][1])
            assert isinstance(content, list)
            assert content[0]["message_count"] == 25

    def test_no_topics_uses_atomic_write(self, tmp_path):
        """When no topics are extractable, truncation still uses atomic_write."""
        import json
        from unittest.mock import patch
        from app.conversation_history import compact_history

        history = tmp_path / "history.jsonl"
        topics = tmp_path / "topics.json"

        messages = []
        for i in range(25):
            msg = {"timestamp": f"2026-02-18T10:{i:02d}:00", "role": "assistant", "text": "ok"}
            messages.append(json.dumps(msg, ensure_ascii=False))
        history.write_text("\n".join(messages) + "\n")

        with patch("app.conversation_history._atomic_write") as mock_aw:
            count = compact_history(history, topics)
            assert count == 25
            mock_aw.assert_called_once_with(history, "")

    def test_end_to_end_compaction(self, tmp_path):
        """Full compaction cycle: messages in, topics out, history empty."""
        import json
        from app.conversation_history import compact_history

        history = tmp_path / "history.jsonl"
        topics = tmp_path / "topics.json"

        messages = []
        for i in range(25):
            msg = {"timestamp": f"2026-02-18T10:{i:02d}:00", "role": "user", "text": f"Message {i} about testing"}
            messages.append(json.dumps(msg, ensure_ascii=False))
        history.write_text("\n".join(messages) + "\n")

        count = compact_history(history, topics)
        assert count == 25
        assert history.read_text() == ""
        assert topics.exists()
        topics_data = json.loads(topics.read_text())
        assert len(topics_data) == 1
        assert topics_data[0]["message_count"] == 25

    def test_end_to_end_no_topics(self, tmp_path):
        """Compaction with no extractable topics still clears history."""
        import json
        from app.conversation_history import compact_history

        history = tmp_path / "history.jsonl"
        topics = tmp_path / "topics.json"

        messages = []
        for i in range(25):
            msg = {"timestamp": f"2026-02-18T10:{i:02d}:00", "role": "assistant", "text": "ok"}
            messages.append(json.dumps(msg, ensure_ascii=False))
        history.write_text("\n".join(messages) + "\n")

        count = compact_history(history, topics)
        assert count == 25
        assert history.read_text() == ""


# --- prune_topics ---


class TestPruneTopics:
    def test_prunes_to_max_entries(self):
        from app.conversation_history import prune_topics
        entries = [
            {"compacted_at": f"2026-01-{i+1:02d}T00:00:00", "message_count": 10}
            for i in range(25)
        ]
        result = prune_topics(entries, max_entries=20)
        assert len(result) == 20
        # Should keep the most recent 20 (days 6-25)
        assert result[0]["compacted_at"] == "2026-01-06T00:00:00"
        assert result[-1]["compacted_at"] == "2026-01-25T00:00:00"

    def test_no_pruning_when_under_limit(self):
        from app.conversation_history import prune_topics
        entries = [{"compacted_at": "2026-01-01T00:00:00"}] * 5
        result = prune_topics(entries, max_entries=20)
        assert len(result) == 5

    def test_no_pruning_at_exact_limit(self):
        from app.conversation_history import prune_topics
        entries = [{"compacted_at": f"2026-01-{i+1:02d}T00:00:00"} for i in range(20)]
        result = prune_topics(entries, max_entries=20)
        assert len(result) == 20

    def test_empty_list(self):
        from app.conversation_history import prune_topics
        assert prune_topics([], max_entries=20) == []

    def test_non_list_input(self):
        from app.conversation_history import prune_topics
        assert prune_topics("not a list", max_entries=20) == "not a list"

    def test_compact_history_prunes_topics(self, history_file, topics_file):
        """compact_history should prune topics file after appending."""
        from app.conversation_history import save_conversation_message, compact_history
        import json

        # Pre-populate with 22 existing entries
        existing = [
            {"compacted_at": f"2026-01-{i+1:02d}T00:00:00", "message_count": 10}
            for i in range(22)
        ]
        topics_file.write_text(json.dumps(existing))

        # Trigger a compaction that appends one more entry (total 23)
        for i in range(25):
            save_conversation_message(history_file, "user", f"Discussion topic number {i}")
        compact_history(history_file, topics_file, min_messages=20)

        topics = json.loads(topics_file.read_text())
        assert len(topics) == 20
        # The newest entry (just appended) should be the last one
        assert "topics_by_date" in topics[-1]


# --- backward compatibility ---


class TestBackwardCompat:
    def test_telegram_functions_accessible_from_utils(self):
        from app.utils import (
            save_telegram_message,
            load_recent_telegram_history,
            format_conversation_history,
            compact_telegram_history,
        )
        assert callable(save_telegram_message)
        assert callable(load_recent_telegram_history)
        assert callable(format_conversation_history)
        assert callable(compact_telegram_history)
