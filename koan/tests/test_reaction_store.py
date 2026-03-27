"""Tests for reaction_store module."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def reactions_file(tmp_path):
    return tmp_path / "reactions.jsonl"


@pytest.fixture
def history_file(tmp_path):
    path = tmp_path / "conversation-history.jsonl"
    entries = [
        {"timestamp": "2026-03-10T10:00:00", "role": "assistant", "text": "Hello there!", "message_id": 100, "message_type": "chat"},
        {"timestamp": "2026-03-10T10:01:00", "role": "assistant", "text": "Mission done: fixed the bug", "message_id": 101, "message_type": "conclusion"},
        {"timestamp": "2026-03-10T10:02:00", "role": "user", "text": "Thanks!"},
    ]
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


class TestSaveReaction:
    def test_creates_file_and_appends(self, reactions_file):
        from app.reaction_store import save_reaction

        save_reaction(reactions_file, 100, "👍", is_added=True)

        lines = reactions_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["message_id"] == 100
        assert entry["emoji"] == "👍"
        assert entry["action"] == "added"
        assert "timestamp" in entry

    def test_removed_reaction(self, reactions_file):
        from app.reaction_store import save_reaction

        save_reaction(reactions_file, 100, "👎", is_added=False)

        entry = json.loads(reactions_file.read_text().strip())
        assert entry["action"] == "removed"

    def test_with_context(self, reactions_file):
        from app.reaction_store import save_reaction

        save_reaction(
            reactions_file, 100, "❤️", is_added=True,
            original_text_preview="Hello there!",
            message_type="chat",
        )

        entry = json.loads(reactions_file.read_text().strip())
        assert entry["original_text_preview"] == "Hello there!"
        assert entry["message_type"] == "chat"

    def test_preview_truncated_to_100(self, reactions_file):
        from app.reaction_store import save_reaction

        long_text = "x" * 200
        save_reaction(reactions_file, 100, "👍", is_added=True, original_text_preview=long_text)

        entry = json.loads(reactions_file.read_text().strip())
        assert len(entry["original_text_preview"]) == 100

    def test_multiple_appends(self, reactions_file):
        from app.reaction_store import save_reaction

        save_reaction(reactions_file, 100, "👍", is_added=True)
        save_reaction(reactions_file, 101, "👎", is_added=True)

        lines = reactions_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_optional_fields_omitted_when_empty(self, reactions_file):
        from app.reaction_store import save_reaction

        save_reaction(reactions_file, 100, "👍", is_added=True)

        entry = json.loads(reactions_file.read_text().strip())
        assert "original_text_preview" not in entry
        assert "message_type" not in entry


class TestLoadRecentReactions:
    def test_empty_file(self, reactions_file):
        from app.reaction_store import load_recent_reactions

        assert load_recent_reactions(reactions_file) == []

    def test_nonexistent_file(self, tmp_path):
        from app.reaction_store import load_recent_reactions

        assert load_recent_reactions(tmp_path / "missing.jsonl") == []

    def test_loads_all(self, reactions_file):
        from app.reaction_store import save_reaction, load_recent_reactions

        for i in range(5):
            save_reaction(reactions_file, 100 + i, "👍", is_added=True)

        reactions = load_recent_reactions(reactions_file)
        assert len(reactions) == 5

    def test_respects_max(self, reactions_file):
        from app.reaction_store import save_reaction, load_recent_reactions

        for i in range(10):
            save_reaction(reactions_file, 100 + i, "👍", is_added=True)

        reactions = load_recent_reactions(reactions_file, max_reactions=3)
        assert len(reactions) == 3
        # Should return the most recent 3
        assert reactions[0]["message_id"] == 107

    def test_skips_invalid_json(self, reactions_file):
        from app.reaction_store import load_recent_reactions

        reactions_file.write_text('{"message_id": 100, "emoji": "👍"}\ninvalid json\n{"message_id": 101, "emoji": "👎"}\n')

        reactions = load_recent_reactions(reactions_file)
        assert len(reactions) == 2


class TestLookupMessageContext:
    def test_finds_message_by_id(self, history_file):
        from app.reaction_store import lookup_message_context

        result = lookup_message_context(history_file, 100)
        assert result is not None
        assert result["text"] == "Hello there!"
        assert result["message_type"] == "chat"

    def test_finds_different_message(self, history_file):
        from app.reaction_store import lookup_message_context

        result = lookup_message_context(history_file, 101)
        assert result is not None
        assert result["message_type"] == "conclusion"

    def test_returns_none_for_unknown_id(self, history_file):
        from app.reaction_store import lookup_message_context

        assert lookup_message_context(history_file, 999) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        from app.reaction_store import lookup_message_context

        assert lookup_message_context(tmp_path / "missing.jsonl", 100) is None

    def test_message_without_id_field(self, history_file):
        from app.reaction_store import lookup_message_context

        # Message at index 2 has no message_id field
        assert lookup_message_context(history_file, 0) is None


class TestCompactReactions:
    def test_compacts_to_keep_limit(self, reactions_file):
        from app.reaction_store import save_reaction, compact_reactions, load_recent_reactions

        for i in range(20):
            save_reaction(reactions_file, 100 + i, "👍", is_added=True)

        compact_reactions(reactions_file, keep=5)

        reactions = load_recent_reactions(reactions_file)
        assert len(reactions) == 5
        # Most recent 5
        assert reactions[0]["message_id"] == 115

    def test_noop_on_missing_file(self, tmp_path):
        from app.reaction_store import compact_reactions

        compact_reactions(tmp_path / "missing.jsonl")  # Should not raise

    def test_noop_when_under_limit(self, reactions_file):
        from app.reaction_store import save_reaction, compact_reactions, load_recent_reactions

        for i in range(3):
            save_reaction(reactions_file, 100 + i, "👍", is_added=True)

        compact_reactions(reactions_file, keep=10)

        reactions = load_recent_reactions(reactions_file)
        assert len(reactions) == 3
