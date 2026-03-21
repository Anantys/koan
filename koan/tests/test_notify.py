"""Tests for notify.py — facade delegation, format_and_send, CLI entry point."""

import subprocess
import time
from unittest.mock import patch, MagicMock

import pytest

from app.notify import (
    send_telegram, format_and_send, reset_flood_state,
    send_typing, TypingIndicator,
    _send_raw_bypass_flood, _direct_send,
    invalidate_file_cache, _file_cache,
    NotificationPriority,
)

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _clear_file_cache():
    """Reset mtime file cache between tests."""
    invalidate_file_cache()
    yield
    invalidate_file_cache()


class TestSendTelegram:
    """Tests that send_telegram delegates to the messaging provider."""

    def setup_method(self):
        reset_flood_state()

    @patch("app.messaging.get_messaging_provider")
    def test_delegates_to_provider(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        assert send_telegram("hello") is True
        mock_provider.send_message.assert_called_once_with("hello")

    @patch("app.messaging.get_messaging_provider")
    def test_returns_false_on_failure(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = False
        mock_get_provider.return_value = mock_provider
        assert send_telegram("test") is False

    @patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1))
    @patch("app.notify._direct_send", return_value=True)
    def test_falls_back_to_direct_send(self, mock_direct, mock_get_provider):
        """If provider unavailable, falls back to direct Telegram API."""
        assert send_telegram("test") is True
        mock_direct.assert_called_once_with("test")

    @patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1))
    def test_no_token_via_fallback(self, mock_get_provider, monkeypatch):
        monkeypatch.delenv("KOAN_TELEGRAM_TOKEN", raising=False)
        assert send_telegram("test") is False

    @patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1))
    def test_no_chat_id_via_fallback(self, mock_get_provider, monkeypatch):
        monkeypatch.delenv("KOAN_TELEGRAM_CHAT_ID", raising=False)
        assert send_telegram("test") is False


class TestFormatAndSend:
    @patch("app.notify.send_telegram", return_value=True)
    def test_with_instance_dir(self, mock_send, instance_dir):
        """format_and_send with explicit instance_dir loads soul/prefs and formats."""
        with patch("app.format_outbox.format_message", return_value="formatted msg") as mock_fmt, \
             patch("app.format_outbox.load_soul", return_value="soul"), \
             patch("app.format_outbox.load_human_prefs", return_value="prefs"), \
             patch("app.format_outbox.load_memory_context", return_value="memory"):
            result = format_and_send("raw msg", instance_dir=str(instance_dir))

        assert result is True
        mock_fmt.assert_called_once_with("raw msg", "soul", "prefs", "memory")
        mock_send.assert_called_once_with("formatted msg",
                                          priority=NotificationPriority.ACTION)

    @patch("app.notify.send_telegram", return_value=True)
    def test_fallback_on_format_error(self, mock_send, instance_dir):
        """If formatting raises, fallback to basic cleanup."""
        with patch("app.format_outbox.load_soul", side_effect=OSError("boom")), \
             patch("app.format_outbox.fallback_format", return_value="clean msg") as mock_fb:
            result = format_and_send("raw", instance_dir=str(instance_dir))

        assert result is True
        mock_fb.assert_called_once_with("raw")
        mock_send.assert_called_once_with("clean msg",
                                          priority=NotificationPriority.ACTION)

    @patch("app.notify.send_telegram", return_value=True)
    @patch("app.notify.load_dotenv")
    def test_no_koan_root_sends_fallback(self, mock_dotenv, mock_send, monkeypatch):
        """Without KOAN_ROOT and no instance_dir, sends basic fallback."""
        monkeypatch.delenv("KOAN_ROOT", raising=False)
        result = format_and_send("raw technical msg")

        assert result is True
        # Should have called send_telegram with some version of the message
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert len(sent_text) > 0  # fallback_format produces non-empty output

    @patch("app.notify.send_telegram", return_value=True)
    def test_koan_root_auto_detect(self, mock_send, tmp_path, monkeypatch):
        """With KOAN_ROOT set, instance_dir is auto-detected."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        with patch("app.format_outbox.load_soul", return_value="s"), \
             patch("app.format_outbox.load_human_prefs", return_value="p"), \
             patch("app.format_outbox.load_memory_context", return_value="m"), \
             patch("app.format_outbox.format_message", return_value="fmt"):
            result = format_and_send("raw")

        assert result is True
        mock_send.assert_called_once_with("fmt", priority=NotificationPriority.ACTION)

    @patch("app.notify.send_telegram", return_value=True)
    def test_project_name_passed_to_memory(self, mock_send, instance_dir):
        """project_name argument is forwarded to load_memory_context."""
        with patch("app.format_outbox.load_soul", return_value="s"), \
             patch("app.format_outbox.load_human_prefs", return_value="p"), \
             patch("app.format_outbox.load_memory_context", return_value="m") as mock_mem, \
             patch("app.format_outbox.format_message", return_value="fmt"):
            format_and_send("raw", instance_dir=str(instance_dir),
                           project_name="myproject")

        mock_mem.assert_called_once()
        assert mock_mem.call_args[0][1] == "myproject"

    @patch("app.notify.send_telegram", return_value=True)
    def test_subprocess_error_uses_fallback(self, mock_send, instance_dir):
        """SubprocessError in formatting triggers fallback path."""
        with patch("app.format_outbox.load_soul",
                   side_effect=subprocess.SubprocessError("cmd failed")), \
             patch("app.format_outbox.fallback_format", return_value="fallback"):
            result = format_and_send("raw", instance_dir=str(instance_dir))
        assert result is True
        mock_send.assert_called_once_with("fallback",
                                          priority=NotificationPriority.ACTION)

    @patch("app.notify.send_telegram", return_value=True)
    def test_value_error_uses_fallback(self, mock_send, instance_dir):
        """ValueError in formatting triggers fallback path."""
        with patch("app.format_outbox.load_soul",
                   side_effect=ValueError("parse error")), \
             patch("app.format_outbox.fallback_format", return_value="fallback"):
            result = format_and_send("raw", instance_dir=str(instance_dir))
        assert result is True
        mock_send.assert_called_once_with("fallback",
                                          priority=NotificationPriority.ACTION)


class TestResetFloodState:
    """Tests for reset_flood_state() edge cases."""

    @patch("app.messaging.get_messaging_provider")
    def test_calls_reset_when_available(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.reset_flood_state = MagicMock()
        mock_get_provider.return_value = mock_provider
        reset_flood_state()
        mock_provider.reset_flood_state.assert_called_once()

    @patch("app.messaging.get_messaging_provider")
    def test_skips_when_no_reset_method(self, mock_get_provider):
        mock_provider = MagicMock(spec=[])  # no reset_flood_state attribute
        mock_get_provider.return_value = mock_provider
        reset_flood_state()  # should not raise

    @patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1))
    def test_silently_handles_system_exit(self, mock_get_provider):
        reset_flood_state()  # should not raise


class TestSendRawBypassFlood:
    """Tests for _send_raw_bypass_flood() function."""

    @patch("app.messaging.get_messaging_provider")
    def test_uses_provider_send_raw(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider._send_raw.return_value = True
        mock_get_provider.return_value = mock_provider
        assert _send_raw_bypass_flood("test") is True
        mock_provider._send_raw.assert_called_once_with("test")

    @patch("app.messaging.get_messaging_provider")
    def test_falls_back_to_send_message(self, mock_get_provider):
        """When provider has no _send_raw, use regular send_message."""
        mock_provider = MagicMock(spec=["send_message"])
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        assert _send_raw_bypass_flood("test") is True
        mock_provider.send_message.assert_called_once_with("test")

    @patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1))
    @patch("app.notify._direct_send", return_value=True)
    def test_system_exit_falls_back_to_direct(self, mock_direct, mock_get_provider):
        assert _send_raw_bypass_flood("test") is True
        mock_direct.assert_called_once_with("test")


class TestDirectSend:
    """Tests for _direct_send() — Telegram API fallback."""

    @patch("app.notify.load_dotenv")
    def test_no_token_returns_false(self, mock_dotenv, monkeypatch):
        monkeypatch.delenv("KOAN_TELEGRAM_TOKEN", raising=False)
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        assert _direct_send("hello") is False

    @patch("app.notify.load_dotenv")
    def test_no_chat_id_returns_false(self, mock_dotenv, monkeypatch):
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.delenv("KOAN_TELEGRAM_CHAT_ID", raising=False)
        assert _direct_send("hello") is False

    @patch("app.notify.load_dotenv")
    def test_successful_send(self, mock_dotenv, monkeypatch):
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            assert _direct_send("hello") is True
        mock_post.assert_called_once()
        assert "bottok" in mock_post.call_args[0][0]

    @patch("app.notify.load_dotenv")
    def test_api_error_returns_false(self, mock_dotenv, monkeypatch):
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False}
        mock_resp.text = "Bad Request"
        with patch("requests.post", return_value=mock_resp):
            assert _direct_send("hello") is False

    @patch("app.retry.time.sleep")
    @patch("app.notify.load_dotenv")
    def test_request_exception_returns_false(self, mock_dotenv, mock_sleep, monkeypatch):
        import requests as req_lib
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        with patch("requests.post", side_effect=req_lib.RequestException("timeout")):
            assert _direct_send("hello") is False
        # Retried 3 times before giving up
        assert mock_sleep.call_count == 2

    @patch("app.retry.time.sleep")
    @patch("app.notify.load_dotenv")
    def test_retry_on_network_error_then_success(self, mock_dotenv, mock_sleep, monkeypatch):
        """_direct_send retries on network error and succeeds on second attempt."""
        import requests as req_lib
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("requests.post", side_effect=[
            req_lib.ConnectionError("reset"),
            mock_resp,
        ]):
            assert _direct_send("hello") is True
        mock_sleep.assert_called_once_with(1)

    @patch("app.notify.load_dotenv")
    def test_chunking_long_message(self, mock_dotenv, monkeypatch):
        """Messages exceeding DEFAULT_MAX_MESSAGE_SIZE are chunked."""
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE
        long_msg = "x" * (DEFAULT_MAX_MESSAGE_SIZE + 100)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            assert _direct_send(long_msg) is True
        assert mock_post.call_count == 2  # split into 2 chunks

    @patch("app.notify.load_dotenv")
    def test_partial_chunk_failure_sends_truncation_notice(self, mock_dotenv, monkeypatch):
        """When some chunks fail, a truncation notice is sent."""
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE
        long_msg = "x" * (DEFAULT_MAX_MESSAGE_SIZE + 100)
        responses = [
            MagicMock(json=MagicMock(return_value={"ok": True})),
            MagicMock(json=MagicMock(return_value={"ok": False}), text="err"),
            MagicMock(json=MagicMock(return_value={"ok": True})),  # notice
        ]
        with patch("requests.post", side_effect=responses) as mock_post:
            assert _direct_send(long_msg) is False
        assert mock_post.call_count == 3  # 2 chunks + 1 notice
        notice_text = mock_post.call_args_list[2][1]["json"]["text"]
        assert "truncated" in notice_text.lower()
        assert "1/2" in notice_text

    @patch("app.notify.load_dotenv")
    def test_empty_message_sends_once(self, mock_dotenv, monkeypatch):
        """Empty string still sends a single API call."""
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            assert _direct_send("") is True
        mock_post.assert_called_once()


class TestNotifyCLI:
    """Tests for __main__ CLI entry point."""

    def test_cli_send_message(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["notify.py", "Hello", "world"])
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.notify", run_name="__main__")
        assert exc_info.value.code == 0

    def test_cli_format_flag(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["notify.py", "--format", "Raw msg"])
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider), \
             patch("app.format_outbox.subprocess.run") as mock_sub, \
             pytest.raises(SystemExit) as exc_info:
            mock_sub.return_value = MagicMock(returncode=0, stdout="Formatted", stderr="")
            run_module("app.notify", run_name="__main__")
        assert exc_info.value.code == 0

    def test_cli_format_passes_project_name(self, monkeypatch):
        """CLI --format reads KOAN_CURRENT_PROJECT env var."""
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["notify.py", "--format", "Raw msg"])
        monkeypatch.setenv("KOAN_CURRENT_PROJECT", "myproject")
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider), \
             patch("app.format_outbox.subprocess.run") as mock_sub, \
             pytest.raises(SystemExit) as exc_info:
            mock_sub.return_value = MagicMock(returncode=0, stdout="Formatted", stderr="")
            run_module("app.notify", run_name="__main__")
        assert exc_info.value.code == 0

    def test_cli_no_args(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["notify.py"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.notify", run_name="__main__")
        assert exc_info.value.code == 1

    def test_cli_format_no_message(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["notify.py", "--format"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.notify", run_name="__main__")
        assert exc_info.value.code == 1

    def test_cli_failure_exit_code(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["notify.py", "msg"])
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = False
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.notify", run_name="__main__")
        assert exc_info.value.code == 1


class TestSendTyping:
    """Tests for send_typing() facade."""

    @patch("app.messaging.get_messaging_provider")
    def test_delegates_to_provider(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.send_typing.return_value = True
        mock_get_provider.return_value = mock_provider
        assert send_typing() is True
        mock_provider.send_typing.assert_called_once()

    @patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1))
    def test_returns_false_on_system_exit(self, mock_get_provider):
        assert send_typing() is False


class TestTypingIndicator:
    """Tests for TypingIndicator context manager."""

    def test_sends_typing_on_enter(self):
        mock_provider = MagicMock()
        mock_provider.send_typing.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider):
            with TypingIndicator():
                time.sleep(0.02)
            mock_provider.send_typing.assert_called()

    def test_stops_sending_on_exit(self):
        mock_provider = MagicMock()
        mock_provider.send_typing.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider):
            with TypingIndicator(interval=0.05):
                time.sleep(0.02)
            initial_count = mock_provider.send_typing.call_count
            time.sleep(0.1)
            # No more calls after exit
            assert mock_provider.send_typing.call_count == initial_count

    def test_sends_periodically(self):
        mock_provider = MagicMock()
        mock_provider.send_typing.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider):
            with TypingIndicator(interval=0.05):
                time.sleep(0.15)
        # At least 2 calls: immediate + periodic
        assert mock_provider.send_typing.call_count >= 2

    def test_context_manager_propagates_exceptions(self):
        """Exceptions propagate through the context manager."""
        mock_provider = MagicMock()
        mock_provider.send_typing.return_value = True
        with patch("app.messaging.get_messaging_provider", return_value=mock_provider):
            with pytest.raises(ValueError):
                with TypingIndicator():
                    raise ValueError("test error")


class TestMtimeFileCaching:
    """Tests for mtime-based file read caching in format_and_send."""

    @patch("app.notify.send_telegram", return_value=True)
    def test_repeated_calls_use_cached_files(self, mock_send, instance_dir):
        """Repeated format_and_send with unchanged files should cache file reads."""
        call_counts = {"soul": 0, "prefs": 0, "memory": 0}

        def counting_soul(path):
            call_counts["soul"] += 1
            return "soul"

        def counting_prefs(path):
            call_counts["prefs"] += 1
            return "prefs"

        def counting_memory(path, project=""):
            call_counts["memory"] += 1
            return "memory"

        with patch("app.format_outbox.format_message", return_value="fmt"), \
             patch("app.format_outbox.load_soul", counting_soul), \
             patch("app.format_outbox.load_human_prefs", counting_prefs), \
             patch("app.format_outbox.load_memory_context", counting_memory):
            format_and_send("raw1", instance_dir=str(instance_dir))
            format_and_send("raw2", instance_dir=str(instance_dir))

        # Each loader should only be called once (second call hits cache)
        assert call_counts["soul"] == 1
        assert call_counts["prefs"] == 1
        assert call_counts["memory"] == 1

    @patch("app.notify.send_telegram", return_value=True)
    def test_file_modification_invalidates_cache(self, mock_send, instance_dir):
        """Modifying a file should cause a cache miss on next call."""
        call_counts = {"soul": 0}

        def counting_soul(path):
            call_counts["soul"] += 1
            return f"soul-v{call_counts['soul']}"

        with patch("app.format_outbox.format_message", return_value="fmt"), \
             patch("app.format_outbox.load_soul", counting_soul), \
             patch("app.format_outbox.load_human_prefs", return_value="p"), \
             patch("app.format_outbox.load_memory_context", return_value="m"):
            format_and_send("raw", instance_dir=str(instance_dir))
            assert call_counts["soul"] == 1

            # Modify the soul file to change mtime
            soul_file = instance_dir / "soul.md"
            import os
            mtime = soul_file.stat().st_mtime
            os.utime(str(soul_file), (mtime + 1, mtime + 1))

            format_and_send("raw", instance_dir=str(instance_dir))
            assert call_counts["soul"] == 2

    @patch("app.notify.send_telegram", return_value=True)
    def test_missing_file_cached_with_zero_mtime(self, mock_send, tmp_path):
        """Missing files should be cached with mtime=0."""
        # Instance dir with no soul.md
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        call_counts = {"soul": 0}

        def counting_soul(path):
            call_counts["soul"] += 1
            return ""

        with patch("app.format_outbox.format_message", return_value="fmt"), \
             patch("app.format_outbox.load_soul", counting_soul), \
             patch("app.format_outbox.load_human_prefs", return_value=""), \
             patch("app.format_outbox.load_memory_context", return_value=""):
            format_and_send("raw", instance_dir=str(instance_dir))
            format_and_send("raw", instance_dir=str(instance_dir))

        assert call_counts["soul"] == 1  # cached even for missing file

    def test_invalidate_clears_cache(self):
        """invalidate_file_cache should clear all entries."""
        _file_cache["test_key"] = ("value", 123.0)
        assert len(_file_cache) == 1
        invalidate_file_cache()
        assert len(_file_cache) == 0


class TestNotificationPriority:
    """Tests for NotificationPriority enum values and ordering."""

    def test_enum_values(self):
        """Priority enum has correct rank values."""
        assert NotificationPriority.INFO.value == 0
        assert NotificationPriority.WARNING.value == 1
        assert NotificationPriority.ACTION.value == 2
        assert NotificationPriority.URGENT.value == 3

    def test_priority_ordering(self):
        """Higher urgency = higher rank."""
        assert NotificationPriority.URGENT.value > NotificationPriority.ACTION.value
        assert NotificationPriority.ACTION.value > NotificationPriority.WARNING.value
        assert NotificationPriority.WARNING.value > NotificationPriority.INFO.value

    def test_priority_names(self):
        """Enum names are uppercase."""
        assert NotificationPriority.URGENT.name == "URGENT"
        assert NotificationPriority.ACTION.name == "ACTION"
        assert NotificationPriority.WARNING.name == "WARNING"
        assert NotificationPriority.INFO.name == "INFO"


class TestPriorityFiltering:
    """Tests for min_priority config filtering in send_telegram().

    Note: these tests import send_telegram fresh to avoid module-reload
    artifacts from TestNotifyCLI's run_module() calls that pop app.notify
    from sys.modules.
    """

    def _get_send_telegram(self):
        """Import send_telegram fresh to avoid module-reload artifacts."""
        import importlib
        import app.notify as notify_mod
        return notify_mod.send_telegram

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_urgent_always_sent(self, mock_get_provider, mock_min_priority):
        """URGENT messages are sent regardless of min_priority."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.URGENT
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        result = notify_mod.send_telegram("critical",
                                          priority=notify_mod.NotificationPriority.URGENT)
        assert result is True
        mock_provider.send_message.assert_called_once()

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_info_suppressed_when_min_action(self, mock_get_provider, mock_min_priority):
        """INFO messages are suppressed when min_priority is ACTION."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.ACTION
        mock_provider = MagicMock()
        mock_get_provider.return_value = mock_provider
        with patch("app.notify._write_suppressed_to_journal") as mock_journal:
            result = notify_mod.send_telegram("low prio",
                                              priority=notify_mod.NotificationPriority.INFO)
        assert result is True  # suppression counts as success
        mock_provider.send_message.assert_not_called()
        mock_journal.assert_called_once()

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_warning_suppressed_when_min_action(self, mock_get_provider, mock_min_priority):
        """WARNING messages are suppressed when min_priority is ACTION."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.ACTION
        mock_provider = MagicMock()
        mock_get_provider.return_value = mock_provider
        with patch("app.notify._write_suppressed_to_journal") as mock_journal:
            result = notify_mod.send_telegram("warning msg",
                                              priority=notify_mod.NotificationPriority.WARNING)
        assert result is True
        mock_provider.send_message.assert_not_called()
        mock_journal.assert_called_once()

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_info_sent_when_min_info(self, mock_get_provider, mock_min_priority):
        """INFO messages are sent when min_priority is INFO."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        result = notify_mod.send_telegram("info msg",
                                          priority=notify_mod.NotificationPriority.INFO)
        assert result is True
        mock_provider.send_message.assert_called_once()

    @patch("app.notify._get_min_priority")
    def test_journal_write_on_suppression(self, mock_min_priority, tmp_path, monkeypatch):
        """Suppressed messages are written to the journal."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.ACTION
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        with patch("app.notify._write_suppressed_to_journal") as mock_journal:
            notify_mod.send_telegram("soft ping",
                                     priority=notify_mod.NotificationPriority.INFO)
        mock_journal.assert_called_once()


class TestPriorityEmojiRendering:
    """Tests for priority emoji prefix in send_telegram().

    Note: these tests use `app.notify` module directly to avoid module-reload
    artifacts from TestNotifyCLI's run_module() calls.
    """

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_urgent_gets_siren_emoji(self, mock_get_provider, mock_min_priority):
        """URGENT messages get 🚨 prefix."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        notify_mod.send_telegram("server down",
                                 priority=notify_mod.NotificationPriority.URGENT)
        sent = mock_provider.send_message.call_args[0][0]
        assert sent.startswith("🚨")

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_warning_gets_warning_emoji(self, mock_get_provider, mock_min_priority):
        """WARNING messages get ⚠️ prefix."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        notify_mod.send_telegram("low quota",
                                 priority=notify_mod.NotificationPriority.WARNING)
        sent = mock_provider.send_message.call_args[0][0]
        assert sent.startswith("⚠️")

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_action_no_prefix(self, mock_get_provider, mock_min_priority):
        """ACTION messages get no emoji prefix."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        notify_mod.send_telegram("mission done",
                                 priority=notify_mod.NotificationPriority.ACTION)
        sent = mock_provider.send_message.call_args[0][0]
        assert sent == "mission done"

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_info_no_prefix(self, mock_get_provider, mock_min_priority):
        """INFO messages get no emoji prefix."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        notify_mod.send_telegram("update",
                                 priority=notify_mod.NotificationPriority.INFO)
        sent = mock_provider.send_message.call_args[0][0]
        assert sent == "update"

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_idempotent_urgent_emoji(self, mock_get_provider, mock_min_priority):
        """Emoji is not doubled if message already starts with it."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        notify_mod.send_telegram("🚨 server down",
                                 priority=notify_mod.NotificationPriority.URGENT)
        sent = mock_provider.send_message.call_args[0][0]
        assert sent.count("🚨") == 1

    @patch("app.notify._get_min_priority")
    @patch("app.messaging.get_messaging_provider")
    def test_idempotent_warning_emoji(self, mock_get_provider, mock_min_priority):
        """Warning emoji is not doubled if message already starts with it."""
        import app.notify as notify_mod
        mock_min_priority.return_value = notify_mod.NotificationPriority.INFO
        mock_provider = MagicMock()
        mock_provider.send_message.return_value = True
        mock_get_provider.return_value = mock_provider
        notify_mod.send_telegram("⚠️ disk space low",
                                 priority=notify_mod.NotificationPriority.WARNING)
        sent = mock_provider.send_message.call_args[0][0]
        assert sent.count("⚠️") == 1


# Flood protection tests moved to test_telegram_provider.py
# (flood logic lives in TelegramProvider, not notify.py facade)
