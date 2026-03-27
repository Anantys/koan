"""Telegram messaging provider.

Encapsulates all Telegram-specific logic: sending messages,
polling updates, chunking, flood protection, and credential validation.
"""

import html as html_mod
import json
import os
import re
import sys
import threading
import time
from typing import List, Optional

import requests

from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE, Message, MessagingProvider, Reaction, Update
from app.messaging import register_provider


FLOOD_WINDOW_SECONDS = 300  # 5 minutes
MAX_MESSAGE_SIZE = DEFAULT_MAX_MESSAGE_SIZE

# Pattern for markdown code blocks: ```optional_lang\ncode\n```
_CODE_BLOCK_RE = re.compile(r'```(?:[a-zA-Z]*\n)?(.*?)```', re.DOTALL)


def _markdown_to_html(text: str) -> str:
    """Convert markdown code blocks to HTML for Telegram's parse_mode=HTML.

    Replaces ``` blocks with <pre> tags and escapes HTML entities everywhere.
    Non-code text is escaped so that <, >, & don't break Telegram's HTML parser.
    """
    parts = text.split("```")
    if len(parts) < 3:
        # No complete code block pair — return escaped plain text
        return html_mod.escape(text)

    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Outside code block — escape HTML entities
            result.append(html_mod.escape(part))
        else:
            # Inside code block — strip optional language identifier
            content = part
            if content and not content.startswith("\n"):
                # First line might be a language tag (e.g., "python\ncode...")
                first_newline = content.find("\n")
                if first_newline >= 0:
                    first_line = content[:first_newline].strip()
                    if first_line.isalpha():
                        content = content[first_newline + 1:]
                    # else: no language tag, keep full content
                else:
                    # Single-line code block, no language tag
                    pass

            content = content.strip("\n")
            result.append(f"<pre>{html_mod.escape(content)}</pre>")
    return "".join(result)


@register_provider("telegram")
class TelegramProvider(MessagingProvider):
    """Telegram Bot API provider.

    Uses the Bot API for both sending and long-polling.
    Credentials are read from KOAN_TELEGRAM_TOKEN and KOAN_TELEGRAM_CHAT_ID.
    """

    def __init__(self):
        self._bot_token: str = ""
        self._chat_id: str = ""
        self._api_base: str = ""

        # Flood protection state
        self._flood_lock = threading.Lock()
        self._flood_last_message: str = ""
        self._flood_last_sent_at: float = 0.0
        self._flood_warning_sent: bool = False

        # Message ID tracking — populated by _send_chunk(), cleared by _send_raw()
        self._last_message_ids: List[int] = []

    # -- MessagingProvider interface ------------------------------------------

    def configure(self) -> bool:
        from app.utils import load_dotenv
        load_dotenv()

        self._bot_token = os.environ.get("KOAN_TELEGRAM_TOKEN", "")
        self._chat_id = os.environ.get("KOAN_TELEGRAM_CHAT_ID", "")

        if not self._bot_token or not self._chat_id:
            print(
                "[telegram] KOAN_TELEGRAM_TOKEN or KOAN_TELEGRAM_CHAT_ID not set.",
                file=sys.stderr,
            )
            return False

        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"
        return True

    def get_provider_name(self) -> str:
        return "telegram"

    def get_channel_id(self) -> str:
        return self._chat_id

    def send_message(self, text: str) -> bool:
        """Send a message with flood protection and chunking.
        
        Empty messages bypass flood protection but are still sent
        (e.g., for clearing chat state in tests).
        
        Returns:
            True if message was sent OR suppressed (both count as success).
            False only on actual send failure.
        """
        if not text:
            return self._send_raw(text)

        now = time.time()
        action = "send"  # "send", "warn", or "suppress"

        with self._flood_lock:
            if (
                text == self._flood_last_message
                and (now - self._flood_last_sent_at) < FLOOD_WINDOW_SECONDS
            ):
                if not self._flood_warning_sent:
                    self._flood_warning_sent = True
                    action = "warn"
                else:
                    action = "suppress"
            else:
                self._flood_last_message = text
                self._flood_last_sent_at = now
                self._flood_warning_sent = False

        if action == "suppress":
            print("[telegram] Flood suppression: duplicate message dropped.",
                  file=sys.stderr)
            return True
        if action == "warn":
            self._send_raw(
                "[flood] Duplicate message detected — suppressing repeats for 5 min."
            )
            return True

        return self._send_raw(text)

    def get_last_message_ids(self) -> List[int]:
        """Return message IDs from the last send_message() call."""
        return list(self._last_message_ids)

    def poll_updates(self, offset: Optional[int] = None) -> List[Update]:
        """Long-poll the Telegram Bot API for new updates."""
        params: dict = {
            "timeout": 30,
            "allowed_updates": json.dumps(["message", "message_reaction"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(
                f"{self._api_base}/getUpdates",
                params=params,
                timeout=35,
            )
            data = resp.json()
            raw_updates = data.get("result", [])
        except (requests.RequestException, ValueError) as e:
            print(f"[telegram] poll_updates error: {e}", file=sys.stderr)
            return []

        updates: List[Update] = []
        for raw in raw_updates:
            msg_data = raw.get("message", {})
            message = None
            if msg_data:
                message = Message(
                    text=msg_data.get("text", ""),
                    role="user",
                    timestamp=str(msg_data.get("date", "")),
                    raw_data=msg_data,
                )

            reaction = self._parse_reaction(raw)

            updates.append(
                Update(
                    update_id=raw.get("update_id", 0),
                    message=message,
                    reaction=reaction,
                    raw_data=raw,
                )
            )
        return updates

    def _parse_reaction(self, raw: dict) -> Optional[Reaction]:
        """Parse a message_reaction update into a Reaction object."""
        reaction_data = raw.get("message_reaction")
        if not reaction_data:
            return None

        message_id = reaction_data.get("message_id", 0)
        timestamp = str(reaction_data.get("date", ""))

        new_emojis = {
            e.get("emoji", "")
            for e in reaction_data.get("new_reaction", [])
            if e.get("type") == "emoji"
        }
        old_emojis = {
            e.get("emoji", "")
            for e in reaction_data.get("old_reaction", [])
            if e.get("type") == "emoji"
        }

        added = new_emojis - old_emojis
        removed = old_emojis - new_emojis

        if added:
            return Reaction(
                message_id=message_id,
                emoji=next(iter(added)),
                is_added=True,
                timestamp=timestamp,
            )
        if removed:
            return Reaction(
                message_id=message_id,
                emoji=next(iter(removed)),
                is_added=False,
                timestamp=timestamp,
            )
        return None

    # -- Internal helpers -----------------------------------------------------

    def _send_raw(self, text: str) -> bool:
        """Send text to the Telegram API (no flood check).

        Retries each chunk up to 3 times with exponential backoff (1s/2s/4s)
        on transient network failures (connection errors, timeouts).

        If the text contains markdown code blocks (```), they are converted to
        HTML <pre> tags and sent with parse_mode=HTML so Telegram renders them
        as monospace blocks.

        Internal method exposed for notify.py's test-only _send_raw_bypass_flood().
        Normal callers should use send_message() which includes flood protection.
        """
        from app.retry import retry_with_backoff

        if not self._bot_token or not self._chat_id:
            print("[telegram] Not configured — cannot send.", file=sys.stderr)
            return False

        # Auto-detect markdown code blocks and convert to HTML for rendering
        parse_mode = None
        if "```" in text:
            text = _markdown_to_html(text)
            parse_mode = "HTML"

        self._last_message_ids = []
        chunks = self.chunk_message(text, max_size=MAX_MESSAGE_SIZE)
        total = len(chunks)
        sent = 0
        failed = 0
        for chunk in chunks:
            try:
                if retry_with_backoff(
                    lambda c=chunk, pm=parse_mode: self._send_chunk(c, pm),
                    retryable=(requests.RequestException, ValueError),
                    label="telegram send",
                ):
                    sent += 1
                else:
                    failed += 1
            except (requests.RequestException, ValueError) as e:
                print(f"[telegram] Send error after retries: {e}",
                      file=sys.stderr)
                failed += 1

        if failed and sent:
            # Partial delivery — notify the recipient
            notice = f"[⚠️ Message truncated: {sent}/{total} parts delivered, {failed} failed]"
            try:
                self._send_chunk(notice, parse_mode)
            except Exception as e:
                print(f"[telegram] Failed to send truncation notice: {e}",
                      file=sys.stderr)

        return failed == 0

    def _send_chunk(self, chunk: str, parse_mode: str = None) -> bool:
        """Send a single chunk via Telegram API. Raises on network error."""
        payload = {"chat_id": self._chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(
            f"{self._api_base}/sendMessage",
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(
                f"[telegram] API error: {resp.text[:200]}",
                file=sys.stderr,
            )
            return False
        # Capture message_id for reaction correlation
        result = data.get("result", {})
        msg_id = result.get("message_id", 0)
        if msg_id:
            self._last_message_ids.append(msg_id)
        return True

    def send_typing(self) -> bool:
        """Send 'typing...' indicator to the Telegram chat."""
        if not self._bot_token or not self._chat_id:
            return False
        try:
            resp = requests.post(
                f"{self._api_base}/sendChatAction",
                json={"chat_id": self._chat_id, "action": "typing"},
                timeout=5,
            )
            return resp.json().get("ok", False)
        except (requests.RequestException, ValueError):
            return False

    def reset_flood_state(self):
        """Reset flood protection state.
        
        Public method intended for testing and explicit flood state management.
        Called by notify.reset_flood_state() facade and test fixtures.
        """
        with self._flood_lock:
            self._flood_last_message = ""
            self._flood_last_sent_at = 0.0
            self._flood_warning_sent = False
