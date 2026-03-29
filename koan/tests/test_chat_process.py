"""Tests for the dedicated chat process and inbox/outbox protocol."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.chat_process import (
    read_and_clear_inbox,
    write_to_inbox,
    has_pending_requests,
    CHAT_RETRY_BACKOFF,
    CHAT_MAX_ATTEMPTS,
)


@pytest.fixture
def chat_inbox(instance_dir):
    """Provide the chat inbox path (inside instance_dir's parent as KOAN_ROOT)."""
    inbox = instance_dir / "chat-inbox.jsonl"
    return inbox


class TestInboxProtocol:
    """Test the file-based inbox protocol for chat requests."""

    def test_write_and_read_inbox(self, chat_inbox, monkeypatch):
        """Write a request, read it back, inbox is cleared."""
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", chat_inbox)

        write_to_inbox("Hello there")
        assert chat_inbox.exists()
        assert chat_inbox.stat().st_size > 0

        entries = read_and_clear_inbox()
        assert len(entries) == 1
        assert entries[0]["text"] == "Hello there"
        assert "timestamp" in entries[0]

        # Inbox should be cleared after read
        assert chat_inbox.read_text().strip() == ""

    def test_multiple_messages_fifo(self, chat_inbox, monkeypatch):
        """Multiple messages are returned in order."""
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", chat_inbox)

        write_to_inbox("First message")
        write_to_inbox("Second message")

        entries = read_and_clear_inbox()
        assert len(entries) == 2
        assert entries[0]["text"] == "First message"
        assert entries[1]["text"] == "Second message"

    def test_read_empty_inbox(self, chat_inbox, monkeypatch):
        """Reading a non-existent inbox returns empty list."""
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", chat_inbox)
        assert read_and_clear_inbox() == []

    def test_has_pending_requests_empty(self, chat_inbox, monkeypatch):
        """No pending requests when inbox doesn't exist."""
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", chat_inbox)
        assert has_pending_requests() is False

    def test_has_pending_requests_with_data(self, chat_inbox, monkeypatch):
        """Pending requests detected when inbox has content."""
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", chat_inbox)
        write_to_inbox("test")
        assert has_pending_requests() is True

    def test_has_pending_after_clear(self, chat_inbox, monkeypatch):
        """No pending requests after inbox is read and cleared."""
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", chat_inbox)
        write_to_inbox("test")
        read_and_clear_inbox()
        assert has_pending_requests() is False


class TestChatRouting:
    """Test that awake.py routes to chat process when available."""

    @patch("app.awake._is_chat_process_running", return_value=True)
    @patch("app.awake.send_telegram")
    def test_routes_to_chat_process_when_running(self, mock_send, mock_running, monkeypatch, instance_dir):
        """When chat process is alive, messages go to inbox."""
        from app.awake import _route_to_chat_process

        inbox = instance_dir / "chat-inbox.jsonl"
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", inbox)

        result = _route_to_chat_process("Hello")
        assert result is True
        # Verify it was written to inbox
        assert inbox.exists()
        entries = json.loads(inbox.read_text().strip())
        assert entries["text"] == "Hello"

    @patch("app.awake._is_chat_process_running", return_value=False)
    def test_falls_back_when_process_not_running(self, mock_running):
        """When chat process is not running, returns False for fallback."""
        from app.awake import _route_to_chat_process
        result = _route_to_chat_process("Hello")
        assert result is False

    @patch("app.awake._is_chat_process_running", return_value=True)
    @patch("app.awake.send_telegram")
    def test_busy_when_pending_requests(self, mock_send, mock_running, monkeypatch, instance_dir):
        """When inbox already has pending requests, send busy message."""
        from app.awake import _route_to_chat_process

        inbox = instance_dir / "chat-inbox.jsonl"
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", inbox)

        # Pre-fill inbox
        write_to_inbox("first message")
        monkeypatch.setattr("app.chat_process.CHAT_INBOX", inbox)

        result = _route_to_chat_process("second message")
        assert result is True
        mock_send.assert_called_once()
        assert "Busy" in mock_send.call_args[0][0]


class TestRetryConstants:
    """Verify retry configuration is sensible."""

    def test_backoff_is_increasing(self):
        for i in range(len(CHAT_RETRY_BACKOFF) - 1):
            assert CHAT_RETRY_BACKOFF[i] < CHAT_RETRY_BACKOFF[i + 1]

    def test_max_attempts_matches_backoff(self):
        assert CHAT_MAX_ATTEMPTS == 3
        assert len(CHAT_RETRY_BACKOFF) == 3


class TestMissionAwareness:
    """Test that the chat process detects active missions."""

    def test_detects_active_mission(self, tmp_path, monkeypatch):
        from app.chat_process import _is_mission_active
        monkeypatch.setattr("app.chat_process.KOAN_ROOT", tmp_path)
        (tmp_path / ".koan-status").write_text("Run 1/5 — executing mission on my-project")
        assert _is_mission_active() is True

    def test_no_mission_when_idle(self, tmp_path, monkeypatch):
        from app.chat_process import _is_mission_active
        monkeypatch.setattr("app.chat_process.KOAN_ROOT", tmp_path)
        (tmp_path / ".koan-status").write_text("Idle — sleeping 60s")
        assert _is_mission_active() is False

    def test_no_mission_when_no_status_file(self, tmp_path, monkeypatch):
        from app.chat_process import _is_mission_active
        monkeypatch.setattr("app.chat_process.KOAN_ROOT", tmp_path)
        assert _is_mission_active() is False
