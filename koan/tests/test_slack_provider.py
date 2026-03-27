"""Tests for SlackProvider — configuration, sending, polling, event handling."""

from unittest.mock import patch, MagicMock, PropertyMock
import queue

import pytest


class TestConfigure:
    def test_missing_bot_token(self, monkeypatch):
        monkeypatch.delenv("KOAN_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.setenv("KOAN_SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("KOAN_SLACK_CHANNEL_ID", "C123")
        from app.messaging.slack import SlackProvider
        p = SlackProvider()
        assert p.configure() is False

    def test_missing_app_token(self, monkeypatch):
        monkeypatch.setenv("KOAN_SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.delenv("KOAN_SLACK_APP_TOKEN", raising=False)
        monkeypatch.setenv("KOAN_SLACK_CHANNEL_ID", "C123")
        from app.messaging.slack import SlackProvider
        p = SlackProvider()
        assert p.configure() is False

    def test_missing_channel_id(self, monkeypatch):
        monkeypatch.setenv("KOAN_SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("KOAN_SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("KOAN_SLACK_CHANNEL_ID", raising=False)
        from app.messaging.slack import SlackProvider
        p = SlackProvider()
        assert p.configure() is False

    def test_missing_slack_sdk(self, monkeypatch):
        monkeypatch.setenv("KOAN_SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("KOAN_SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("KOAN_SLACK_CHANNEL_ID", "C123")
        import builtins
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if "slack_sdk" in name:
                raise ImportError("no slack_sdk")
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=mock_import):
            from app.messaging.slack import SlackProvider
            p = SlackProvider()
            assert p.configure() is False


@pytest.fixture
def provider():
    """Create a pre-configured SlackProvider with mocked SDK."""
    from app.messaging.slack import SlackProvider
    p = SlackProvider()
    p._bot_token = "xoxb-test"
    p._app_token = "xapp-test"
    p._channel_id = "C123"
    p._bot_user_id = "U999"
    p._web_client = MagicMock()
    p._socket_client = MagicMock()
    return p


class TestGetters:
    def test_provider_name(self, provider):
        assert provider.get_provider_name() == "slack"

    def test_channel_id(self, provider):
        assert provider.get_channel_id() == "C123"


class TestSendMessage:
    def test_short_message(self, provider):
        provider._web_client.chat_postMessage.return_value = {"ok": True}
        assert provider.send_message("hello") is True
        provider._web_client.chat_postMessage.assert_called_once_with(
            channel="C123", text="hello"
        )

    def test_long_message_chunked(self, provider):
        provider._web_client.chat_postMessage.return_value = {"ok": True}
        # Bypass rate-limit sleeps (1s between chunks) to keep test fast
        with patch("app.messaging.slack.time.sleep"):
            assert provider.send_message("x" * 8500) is True
        assert provider._web_client.chat_postMessage.call_count == 3

    def test_api_error(self, provider):
        provider._web_client.chat_postMessage.return_value = {
            "ok": False, "error": "channel_not_found"
        }
        assert provider.send_message("test") is False

    def test_exception(self, provider):
        provider._web_client.chat_postMessage.side_effect = Exception("network")
        assert provider.send_message("test") is False

    def test_not_configured(self):
        from app.messaging.slack import SlackProvider
        p = SlackProvider()
        assert p.send_message("test") is False

    def test_empty_message(self, provider):
        provider._web_client.chat_postMessage.return_value = {"ok": True}
        assert provider.send_message("") is True


class TestPollUpdates:
    def test_empty_queue(self, provider):
        provider._connected = True
        updates = provider.poll_updates()
        assert updates == []

    def test_drains_queue(self, provider):
        from app.messaging.base import Update, Message
        provider._connected = True
        provider._message_queue.put(
            Update(update_id=1, message=Message(text="hi", role="user"))
        )
        provider._message_queue.put(
            Update(update_id=2, message=Message(text="hey", role="user"))
        )
        updates = provider.poll_updates()
        assert len(updates) == 2
        assert updates[0].message.text == "hi"
        assert updates[1].message.text == "hey"

    def test_starts_socket_if_not_connected(self, provider):
        provider._connected = False
        provider.poll_updates()
        provider._socket_client.connect.assert_called_once()

    def test_handles_connection_failure(self, provider):
        provider._connected = False
        provider._socket_client.connect.side_effect = Exception("connection failed")
        updates = provider.poll_updates()
        assert updates == []


class TestHandleSocketEvent:
    """Test Socket Mode event handling and filtering logic."""
    
    def _make_request(self, event_type, channel, text,
                      bot_id=None, subtype=None, ts="123.456"):
        """Create a mock Socket Mode request with the given event properties."""
        req = MagicMock()
        req.envelope_id = "env-1"
        event = {
            "type": event_type,
            "channel": channel,
            "text": text,
            "ts": ts,
        }
        if bot_id:
            event["bot_id"] = bot_id
        if subtype:
            event["subtype"] = subtype
        req.payload = {"event": event}
        return req

    def test_message_event(self, provider):
        req = self._make_request("message", "C123", "hello")
        provider._handle_socket_event(MagicMock(), req)
        assert provider._message_queue.qsize() == 1
        update = provider._message_queue.get_nowait()
        assert update.message.text == "hello"

    def test_app_mention_strips_bot_mention(self, provider):
        req = self._make_request("app_mention", "C123", "<@U999> do something")
        provider._handle_socket_event(MagicMock(), req)
        update = provider._message_queue.get_nowait()
        assert update.message.text == "do something"

    def test_ignores_other_channels(self, provider):
        req = self._make_request("message", "C999", "wrong channel")
        provider._handle_socket_event(MagicMock(), req)
        assert provider._message_queue.empty()

    def test_ignores_bot_messages(self, provider):
        req = self._make_request("message", "C123", "bot msg", bot_id="B123")
        provider._handle_socket_event(MagicMock(), req)
        assert provider._message_queue.empty()

    def test_ignores_subtypes(self, provider):
        req = self._make_request("message", "C123", "edited", subtype="message_changed")
        provider._handle_socket_event(MagicMock(), req)
        assert provider._message_queue.empty()

    def test_ignores_empty_text(self, provider):
        req = self._make_request("message", "C123", "")
        provider._handle_socket_event(MagicMock(), req)
        assert provider._message_queue.empty()

    def test_ignores_non_message_events(self, provider):
        req = self._make_request("reaction_added", "C123", "")
        provider._handle_socket_event(MagicMock(), req)
        assert provider._message_queue.empty()

    def test_update_counter_increments(self, provider):
        req1 = self._make_request("message", "C123", "first")
        req2 = self._make_request("message", "C123", "second")
        provider._handle_socket_event(MagicMock(), req1)
        provider._handle_socket_event(MagicMock(), req2)
        u1 = provider._message_queue.get_nowait()
        u2 = provider._message_queue.get_nowait()
        assert u1.update_id == 1
        assert u2.update_id == 2

    def test_acknowledges_event(self, provider):
        req = self._make_request("message", "C123", "hello")
        mock_client = MagicMock()
        # Patch the slack_sdk import to succeed (even without slack_sdk installed)
        mock_response_cls = MagicMock()
        with patch.dict("sys.modules", {
            "slack_sdk": MagicMock(),
            "slack_sdk.socket_mode": MagicMock(),
            "slack_sdk.socket_mode.response": MagicMock(SocketModeResponse=mock_response_cls),
        }):
            provider._handle_socket_event(mock_client, req)
        mock_client.send_socket_mode_response.assert_called_once()


class TestSendRaw:
    def test_send_raw(self, provider):
        provider._web_client.chat_postMessage.return_value = {"ok": True}
        assert provider._send_raw("test") is True

    def test_send_raw_not_configured(self):
        from app.messaging.slack import SlackProvider
        p = SlackProvider()
        assert p._send_raw("test") is False

    def test_send_raw_error(self, provider):
        provider._web_client.chat_postMessage.side_effect = Exception("fail")
        assert provider._send_raw("test") is False
