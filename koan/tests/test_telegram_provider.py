"""Tests for TelegramProvider — send, poll, chunking, flood protection, configuration."""

from unittest.mock import patch, MagicMock

import pytest
import requests

from app.messaging.telegram import TelegramProvider, FLOOD_WINDOW_SECONDS, _markdown_to_html


@pytest.fixture
def provider():
    """Create a configured TelegramProvider."""
    p = TelegramProvider()
    p._bot_token = "test-token"
    p._chat_id = "12345"
    p._api_base = "https://api.telegram.org/bottest-token"
    return p


class TestConfigure:
    def test_valid_credentials(self, monkeypatch):
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        p = TelegramProvider()
        assert p.configure() is True
        assert p._bot_token == "tok"
        assert p._chat_id == "123"
        assert "tok" in p._api_base

    @patch("app.utils.load_dotenv")
    def test_missing_token(self, mock_dotenv, monkeypatch):
        monkeypatch.delenv("KOAN_TELEGRAM_TOKEN", raising=False)
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123")
        p = TelegramProvider()
        assert p.configure() is False

    @patch("app.utils.load_dotenv")
    def test_missing_chat_id(self, mock_dotenv, monkeypatch):
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "tok")
        monkeypatch.delenv("KOAN_TELEGRAM_CHAT_ID", raising=False)
        p = TelegramProvider()
        assert p.configure() is False


class TestGetters:
    def test_provider_name(self, provider):
        assert provider.get_provider_name() == "telegram"

    def test_channel_id(self, provider):
        assert provider.get_channel_id() == "12345"


class TestSendRaw:
    @patch("app.messaging.telegram.requests.post")
    def test_short_message(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider._send_raw("hello") is True
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["json"]["text"] == "hello"

    @patch("app.messaging.telegram.requests.post")
    def test_long_message_chunked(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider._send_raw("x" * 8500) is True
        assert mock_post.call_count == 3  # 4000 + 4000 + 500

    @patch("app.messaging.telegram.requests.post")
    def test_exact_boundary(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider._send_raw("x" * 4000) is True
        assert mock_post.call_count == 1

    @patch("app.messaging.telegram.requests.post")
    def test_just_over_boundary(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider._send_raw("x" * 4001) is True
        assert mock_post.call_count == 2

    @patch("app.messaging.telegram.requests.post")
    def test_api_error(self, mock_post, provider):
        mock_post.return_value = MagicMock(
            json=lambda: {"ok": False, "description": "bad"},
            text='{"ok":false}',
        )
        assert provider._send_raw("test") is False

    @patch("app.retry.time.sleep")
    @patch("app.messaging.telegram.requests.post",
           side_effect=requests.RequestException("network"))
    def test_network_error(self, mock_post, mock_sleep, provider):
        assert provider._send_raw("test") is False
        # Retried 3 times (2 sleeps)
        assert mock_sleep.call_count == 2

    @patch("app.retry.time.sleep")
    @patch("app.messaging.telegram.requests.post",
           side_effect=ValueError("bad json"))
    def test_json_error(self, mock_post, mock_sleep, provider):
        assert provider._send_raw("test") is False

    @patch("app.retry.time.sleep")
    @patch("app.messaging.telegram.requests.post")
    def test_retry_on_network_then_success(self, mock_post, mock_sleep, provider):
        """_send_raw retries on transient network error and succeeds."""
        mock_post.side_effect = [
            requests.ConnectionError("reset"),
            MagicMock(json=lambda: {"ok": True}),
        ]
        assert provider._send_raw("test") is True
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(1)

    def test_not_configured(self):
        p = TelegramProvider()
        assert p._send_raw("test") is False

    @patch("app.messaging.telegram.requests.post")
    def test_partial_failure(self, mock_post, provider):
        """If one chunk fails, returns False."""
        responses = [
            MagicMock(json=lambda: {"ok": True}),
            MagicMock(json=lambda: {"ok": False, "description": "limit"}, text="limit"),
        ]
        mock_post.side_effect = responses
        assert provider._send_raw("a" * 5000) is False


class TestSendMessage:
    """Tests for send_message with flood protection."""

    @patch("app.messaging.telegram.requests.post")
    def test_first_message_passes(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider.send_message("hello") is True
        mock_post.assert_called_once()

    @patch("app.messaging.telegram.requests.post")
    def test_empty_message(self, mock_post, provider):
        """Empty string goes to _send_raw directly (no flood tracking)."""
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider.send_message("") is True

    @patch("app.messaging.telegram.requests.post")
    @patch("app.messaging.telegram.time.time")
    def test_duplicate_triggers_warning(self, mock_time, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        mock_time.return_value = 1000.0
        provider.send_message("hello")
        mock_time.return_value = 1010.0
        result = provider.send_message("hello")

        assert result is True
        assert mock_post.call_count == 2
        warning = mock_post.call_args_list[1][1]["json"]["text"]
        assert "flood" in warning.lower()

    @patch("app.messaging.telegram.requests.post")
    @patch("app.messaging.telegram.time.time")
    def test_third_duplicate_suppressed(self, mock_time, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        mock_time.return_value = 1000.0
        provider.send_message("hello")
        mock_time.return_value = 1010.0
        provider.send_message("hello")  # warning
        mock_time.return_value = 1020.0
        result = provider.send_message("hello")  # suppressed

        assert result is True
        assert mock_post.call_count == 2  # original + warning only

    @patch("app.messaging.telegram.requests.post")
    @patch("app.messaging.telegram.time.time")
    def test_different_message_resets(self, mock_time, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        mock_time.return_value = 1000.0
        provider.send_message("hello")
        mock_time.return_value = 1010.0
        provider.send_message("hello")  # warning
        mock_time.return_value = 1020.0
        result = provider.send_message("world")

        assert result is True
        assert mock_post.call_count == 3

    @patch("app.messaging.telegram.requests.post")
    @patch("app.messaging.telegram.time.time")
    def test_window_expiry(self, mock_time, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        mock_time.return_value = 1000.0
        provider.send_message("hello")
        mock_time.return_value = 1000.0 + FLOOD_WINDOW_SECONDS + 1
        result = provider.send_message("hello")

        assert result is True
        assert mock_post.call_count == 2
        for call in mock_post.call_args_list:
            assert call[1]["json"]["text"] == "hello"

    @patch("app.messaging.telegram.requests.post")
    @patch("app.messaging.telegram.time.time")
    def test_flood_with_chunks(self, mock_time, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        long_msg = "x" * 5000
        mock_time.return_value = 1000.0
        provider.send_message(long_msg)
        mock_time.return_value = 1010.0
        result = provider.send_message(long_msg)

        assert result is True
        assert mock_post.call_count == 3  # 2 chunks + 1 warning


class TestResetFloodState:
    @patch("app.messaging.telegram.requests.post")
    @patch("app.messaging.telegram.time.time")
    def test_reset_allows_resend(self, mock_time, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        mock_time.return_value = 1000.0
        provider.send_message("hello")
        mock_time.return_value = 1010.0
        provider.send_message("hello")  # warning

        provider.reset_flood_state()

        mock_time.return_value = 1020.0
        result = provider.send_message("hello")
        assert result is True
        assert mock_post.call_count == 3  # original + warning + after reset


class TestPollUpdates:
    @patch("app.messaging.telegram.requests.get")
    def test_returns_updates(self, mock_get, provider):
        mock_get.return_value = MagicMock()
        mock_get.return_value.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 42,
                    "message": {"text": "hello", "date": 12345,
                                "chat": {"id": 123}},
                }
            ],
        }
        updates = provider.poll_updates()
        assert len(updates) == 1
        assert updates[0].update_id == 42
        assert updates[0].message.text == "hello"
        assert updates[0].message.role == "user"

    @patch("app.messaging.telegram.requests.get")
    def test_passes_offset(self, mock_get, provider):
        mock_get.return_value = MagicMock()
        mock_get.return_value.json.return_value = {"ok": True, "result": []}
        provider.poll_updates(offset=42)
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["offset"] == 42

    @patch("app.messaging.telegram.requests.get")
    def test_no_offset(self, mock_get, provider):
        mock_get.return_value = MagicMock()
        mock_get.return_value.json.return_value = {"ok": True, "result": []}
        provider.poll_updates()
        _, kwargs = mock_get.call_args
        assert "offset" not in kwargs["params"]

    @patch("app.messaging.telegram.requests.get",
           side_effect=requests.RequestException("timeout"))
    def test_network_error(self, mock_get, provider):
        assert provider.poll_updates() == []

    @patch("app.messaging.telegram.requests.get",
           side_effect=requests.RequestException("connection refused"))
    def test_network_error_logs_to_stderr(self, mock_get, provider, capsys):
        """Network errors must be logged to stderr, not silently swallowed."""
        provider.poll_updates()
        captured = capsys.readouterr()
        assert "poll_updates error" in captured.err
        assert "connection refused" in captured.err

    @patch("app.messaging.telegram.requests.get")
    def test_json_error(self, mock_get, provider):
        mock_get.return_value = MagicMock()
        mock_get.return_value.json.side_effect = ValueError("bad")
        assert provider.poll_updates() == []

    @patch("app.messaging.telegram.requests.get")
    def test_json_error_logs_to_stderr(self, mock_get, provider, capsys):
        """JSON parse errors must be logged to stderr, not silently swallowed."""
        mock_get.return_value = MagicMock()
        mock_get.return_value.json.side_effect = ValueError("invalid json")
        provider.poll_updates()
        captured = capsys.readouterr()
        assert "poll_updates error" in captured.err
        assert "invalid json" in captured.err

    @patch("app.messaging.telegram.requests.get")
    def test_update_without_message(self, mock_get, provider):
        mock_get.return_value = MagicMock()
        mock_get.return_value.json.return_value = {
            "ok": True,
            "result": [{"update_id": 10}],
        }
        updates = provider.poll_updates()
        assert len(updates) == 1
        assert updates[0].update_id == 10
        assert updates[0].message is None


class TestSendTyping:
    @patch("app.messaging.telegram.requests.post")
    def test_sends_chat_action(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        assert provider.send_typing() is True
        mock_post.assert_called_once()
        call_json = mock_post.call_args[1]["json"]
        assert call_json["action"] == "typing"
        assert call_json["chat_id"] == "12345"

    @patch("app.messaging.telegram.requests.post")
    def test_returns_false_on_api_error(self, mock_post, provider):
        mock_post.return_value = MagicMock(json=lambda: {"ok": False})
        assert provider.send_typing() is False

    @patch("app.messaging.telegram.requests.post")
    def test_returns_false_on_network_error(self, mock_post, provider):
        mock_post.side_effect = requests.RequestException("timeout")
        assert provider.send_typing() is False

    def test_returns_false_when_not_configured(self):
        p = TelegramProvider()
        assert p.send_typing() is False


class TestMarkdownToHtml:
    """Tests for _markdown_to_html — markdown code block → HTML <pre> conversion."""

    def test_simple_code_block(self):
        text = "Header\n\n```\ncode here\n```"
        result = _markdown_to_html(text)
        assert "<pre>code here</pre>" in result
        assert "```" not in result

    def test_text_outside_block_is_escaped(self):
        text = "x < y & z > w\n```\ncode\n```"
        result = _markdown_to_html(text)
        assert "x &lt; y &amp; z &gt; w" in result
        assert "<pre>code</pre>" in result

    def test_code_inside_block_is_escaped(self):
        text = "header\n```\nif (x < 10) { foo(); }\n```"
        result = _markdown_to_html(text)
        assert "<pre>if (x &lt; 10) { foo(); }</pre>" in result

    def test_language_identifier_stripped(self):
        text = "header\n```python\nprint('hello')\n```"
        result = _markdown_to_html(text)
        assert "<pre>print(&#x27;hello&#x27;)</pre>" in result
        assert "python" not in result

    def test_no_code_blocks_returns_escaped_text(self):
        text = "just plain text with <html>"
        result = _markdown_to_html(text)
        assert result == "just plain text with &lt;html&gt;"

    def test_unclosed_code_block_returns_escaped_plaintext(self):
        """Unclosed code block (single ```) — no pair found, escape as plain text."""
        text = "header <b>\n```\nunclosed code"
        result = _markdown_to_html(text)
        # No complete pair — function escapes HTML entities but keeps backticks
        assert "&lt;b&gt;" in result
        assert "<pre>" not in result  # No code block rendered

    def test_multiple_code_blocks(self):
        text = "intro\n```\nblock1\n```\nmiddle\n```\nblock2\n```"
        result = _markdown_to_html(text)
        assert "<pre>block1</pre>" in result
        assert "<pre>block2</pre>" in result
        assert "middle" in result

    def test_non_alpha_first_line_kept_as_code(self):
        """First line with non-alpha chars is code, not a language tag."""
        text = "header\n```\n10:00 — Step 1\n10:05 — Step 2\n```"
        result = _markdown_to_html(text)
        assert "10:00" in result
        assert "Step 1" in result

    def test_live_skill_format(self):
        """Test the exact format produced by /live handler."""
        text = (
            "# Mission: fix bug\n"
            "Project: koan\n\n"
            "```\n"
            "10:00 — Investigating\n"
            "10:05 — Found root cause\n"
            "```"
        )
        result = _markdown_to_html(text)
        assert "<pre>" in result
        assert "10:00" in result
        assert "10:05" in result
        assert "```" not in result


class TestSendWithParseMode:
    """Tests that code blocks trigger HTML parse_mode in API calls."""

    @patch("app.messaging.telegram.requests.post")
    def test_plain_text_no_parse_mode(self, mock_post, provider):
        """Messages without code blocks are sent without parse_mode."""
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        provider._send_raw("plain text message")
        payload = mock_post.call_args[1]["json"]
        assert "parse_mode" not in payload
        assert payload["text"] == "plain text message"

    @patch("app.messaging.telegram.requests.post")
    def test_code_block_sends_html_parse_mode(self, mock_post, provider):
        """Messages with code blocks are converted to HTML and sent with parse_mode."""
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        provider._send_raw("header\n```\ncode\n```")
        payload = mock_post.call_args[1]["json"]
        assert payload["parse_mode"] == "HTML"
        assert "<pre>code</pre>" in payload["text"]
        assert "```" not in payload["text"]

    @patch("app.messaging.telegram.requests.post")
    def test_live_skill_output_rendered(self, mock_post, provider):
        """Full /live output is properly converted for Telegram."""
        mock_post.return_value = MagicMock(json=lambda: {"ok": True})
        live_output = (
            "# Mission: fix bug\n"
            "Project: koan\n\n"
            "```\n"
            "10:00 — Reading code\n"
            "10:05 — Found issue\n"
            "```"
        )
        provider._send_raw(live_output)
        payload = mock_post.call_args[1]["json"]
        assert payload["parse_mode"] == "HTML"
        assert "<pre>" in payload["text"]
        assert "10:00" in payload["text"]
