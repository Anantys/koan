"""Slack messaging provider.

Uses the Slack SDK (WebClient for sending, SocketModeClient for receiving).
Requires slack-sdk package: pip install slack-sdk

Environment variables:
    KOAN_SLACK_BOT_TOKEN    — Bot User OAuth Token (xoxb-...)
    KOAN_SLACK_APP_TOKEN    — App-Level Token for Socket Mode (xapp-...)
    KOAN_SLACK_CHANNEL_ID   — Channel ID to operate in (C...)
"""

import itertools
import os
import queue
import re
import sys
import threading
import time
from typing import List, Optional

from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE, Message, MessagingProvider, Update
from app.messaging import register_provider


# Rate limit: Slack allows ~1 msg/sec for chat.postMessage
SLACK_RATE_LIMIT_SECONDS = 1.0
MAX_MESSAGE_SIZE = DEFAULT_MAX_MESSAGE_SIZE


@register_provider("slack")
class SlackProvider(MessagingProvider):
    """Slack provider using Bot API and Socket Mode.

    Socket Mode maintains a persistent WebSocket connection for receiving
    events. Messages are buffered in a thread-safe queue and returned
    by poll_updates().
    """

    def __init__(self):
        self._bot_token: str = ""
        self._app_token: str = ""
        self._channel_id: str = ""
        self._web_client = None
        self._socket_client = None
        self._bot_user_id: str = ""

        # Thread-safe message buffer for poll_updates()
        self._message_queue: queue.Queue = queue.Queue()
        self._update_counter = itertools.count(1)
        self._send_lock = threading.Lock()
        self._last_send_time: float = 0.0
        self._connect_lock = threading.Lock()
        self._connected: bool = False

    # -- MessagingProvider interface ------------------------------------------

    def configure(self) -> bool:
        from app.utils import load_dotenv
        load_dotenv()

        self._bot_token = os.environ.get("KOAN_SLACK_BOT_TOKEN", "")
        self._app_token = os.environ.get("KOAN_SLACK_APP_TOKEN", "")
        self._channel_id = os.environ.get("KOAN_SLACK_CHANNEL_ID", "")

        if not self._bot_token:
            print("[slack] KOAN_SLACK_BOT_TOKEN not set.", file=sys.stderr)
            return False
        if not self._app_token:
            print("[slack] KOAN_SLACK_APP_TOKEN not set (required for Socket Mode).",
                  file=sys.stderr)
            return False
        if not self._channel_id:
            print("[slack] KOAN_SLACK_CHANNEL_ID not set.", file=sys.stderr)
            return False

        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
        except ImportError:
            print("[slack] slack-sdk not installed. Run: pip install slack-sdk",
                  file=sys.stderr)
            return False

        self._web_client = WebClient(token=self._bot_token)

        # Resolve bot user ID for stripping @mentions
        try:
            auth = self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id", "")
        except Exception as e:
            print(f"[slack] Auth test failed: {e}", file=sys.stderr)
            return False

        # Set up Socket Mode client
        self._socket_client = SocketModeClient(
            app_token=self._app_token,
            web_client=self._web_client,
        )
        self._socket_client.socket_mode_request_listeners.append(
            self._handle_socket_event
        )

        return True

    def get_provider_name(self) -> str:
        return "slack"

    def get_channel_id(self) -> str:
        return self._channel_id

    def send_message(self, text: str) -> bool:
        """Send a message to the configured Slack channel with rate limiting.
        
        Applies rate limiting between chunks to comply with Slack's ~1 msg/sec limit.
        
        Returns:
            True if all chunks sent successfully, False otherwise.
        """
        if not self._web_client:
            print("[slack] Not configured — cannot send.", file=sys.stderr)
            return False

        ok = True
        for chunk in self.chunk_message(text, max_size=MAX_MESSAGE_SIZE):
            with self._send_lock:
                self._apply_rate_limit()

                try:
                    resp = self._web_client.chat_postMessage(
                        channel=self._channel_id,
                        text=chunk,
                    )
                    if not resp.get("ok"):
                        print(f"[slack] API error: {resp.get('error', 'unknown')}",
                              file=sys.stderr)
                        ok = False
                except Exception as e:
                    print(f"[slack] Send error: {e}", file=sys.stderr)
                    ok = False
                finally:
                    self._last_send_time = time.time()
        return ok

    def poll_updates(self, offset: Optional[int] = None) -> List[Update]:
        """Return buffered updates from Socket Mode.

        Socket Mode receives events asynchronously in a background thread.
        This method drains the queue and returns all buffered updates.
        """
        if not self._connected and self._socket_client:
            with self._connect_lock:
                if not self._connected:
                    self._start_socket_mode()

        updates: List[Update] = []
        while not self._message_queue.empty():
            try:
                updates.append(self._message_queue.get_nowait())
            except queue.Empty:
                break
        return updates

    # -- Internal helpers -----------------------------------------------------

    def _apply_rate_limit(self):
        """Sleep if needed to comply with Slack's rate limit (~1 msg/sec)."""
        elapsed = time.time() - self._last_send_time
        if elapsed < SLACK_RATE_LIMIT_SECONDS:
            time.sleep(SLACK_RATE_LIMIT_SECONDS - elapsed)

    def _start_socket_mode(self):
        """Start Socket Mode connection in a background thread."""
        try:
            self._socket_client.connect()
            self._connected = True
            print("[slack] Socket Mode connected.", file=sys.stderr)
        except Exception as e:
            print(f"[slack] Socket Mode connection failed: {e}", file=sys.stderr)

    def _handle_socket_event(self, client, req):
        """Handle incoming Socket Mode events.
        
        Processes message and app_mention events from the configured channel,
        strips bot mentions, and queues updates for poll_updates().
        """
        # Acknowledge the event immediately
        self._acknowledge_event(client, req)

        payload = req.payload or {}
        event = payload.get("event", {})

        # Only process relevant events from configured channel
        if not self._should_process_event(event):
            return

        text = self._extract_message_text(event)
        if not text:
            return

        self._queue_update(text, event, payload)

    def _acknowledge_event(self, client, req):
        """Send acknowledgement for Socket Mode event."""
        try:
            from slack_sdk.socket_mode.response import SocketModeResponse
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
        except ImportError:
            pass

    def _should_process_event(self, event: dict) -> bool:
        """Check if event should be processed (correct type, channel, not a bot)."""
        event_type = event.get("type", "")
        if event_type not in ("message", "app_mention"):
            return False

        # Filter to configured channel only
        if event.get("channel", "") != self._channel_id:
            return False

        # Skip bot messages and subtypes (edits, joins, etc.)
        if event.get("bot_id") or event.get("subtype"):
            return False

        return True

    def _extract_message_text(self, event: dict) -> str:
        """Extract and clean message text from event."""
        text = event.get("text", "")
        if not text:
            return ""

        # Strip @bot mentions from text
        if self._bot_user_id:
            text = re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

        return text

    def _queue_update(self, text: str, event: dict, payload: dict):
        """Create and queue an Update from processed event data."""
        update = Update(
            update_id=next(self._update_counter),
            message=Message(
                text=text,
                role="user",
                timestamp=event.get("ts", ""),
                raw_data=event,
            ),
            raw_data=payload,
        )
        self._message_queue.put(update)

    def _send_raw(self, text: str) -> bool:
        """Send a message without rate limiting.
        
        Internal method for testing purposes. Production code should use
        send_message() which includes proper rate limiting.
        
        Returns:
            True if message sent successfully, False otherwise.
        """
        if not self._web_client:
            return False
        try:
            resp = self._web_client.chat_postMessage(
                channel=self._channel_id,
                text=text,
            )
            return resp.get("ok", False)
        except Exception as e:
            print(f"[slack] Send error: {e}", file=sys.stderr)
            return False
