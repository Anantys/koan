"""Base classes for messaging providers.

Defines the contract that all messaging providers must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Shared across all providers — both Telegram and Slack enforce ~4096 char limits.
# Using 4000 leaves headroom for API overhead and encoding.
DEFAULT_MAX_MESSAGE_SIZE = 4000


@dataclass
class Message:
    """Provider-agnostic message representation."""
    text: str
    role: str  # "user" or "assistant"
    timestamp: str = ""
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Reaction:
    """Provider-agnostic reaction representation."""
    message_id: int
    emoji: str  # The emoji string (e.g., "👍", "👎", "❤️")
    is_added: bool  # True = added, False = removed
    timestamp: str = ""


@dataclass
class Update:
    """Provider-agnostic update from polling."""
    update_id: int
    message: Optional[Message] = None
    reaction: Optional[Reaction] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)


class MessagingProvider(ABC):
    """Abstract base class for messaging providers.

    Each provider must implement send/receive operations and credential
    validation. The provider is initialized once at startup and used
    as a singleton throughout the process lifetime.
    """

    @abstractmethod
    def send_message(self, text: str) -> bool:
        """Send a message with provider-specific chunking.

        Args:
            text: Message text to send

        Returns:
            True if all chunks sent successfully
        """

    @abstractmethod
    def poll_updates(self, offset: Optional[int] = None) -> List[Update]:
        """Fetch new messages since the given offset.

        Args:
            offset: Provider-specific offset for pagination

        Returns:
            List of new updates (may be empty)
        """

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return the provider identifier (e.g., 'telegram', 'slack')."""

    @abstractmethod
    def get_channel_id(self) -> str:
        """Return the active channel/chat identifier."""

    @abstractmethod
    def configure(self) -> bool:
        """Validate credentials at startup.

        Returns:
            True if configuration is valid

        Should print clear error messages to stderr if credentials are missing.
        """

    def get_last_message_ids(self) -> List[int]:
        """Return message IDs from the last send_message() call.

        Providers that support message ID tracking override this to return
        the IDs of messages sent in the most recent send_message() invocation.
        Returns empty list by default (no tracking).
        """
        return []

    def send_typing(self) -> bool:
        """Send a typing indicator to the channel.

        Shows the user that the bot is "thinking". The indicator typically
        expires after a few seconds (5s on Telegram). Callers that need
        a persistent indicator should use TypingIndicator context manager.

        Returns:
            True if the action was sent (or is unsupported — no-op is success).
        """
        return True  # No-op by default; providers override if supported

    def chunk_message(self, text: str, max_size: int = DEFAULT_MAX_MESSAGE_SIZE) -> List[str]:
        """Split a message into chunks respecting the provider's size limit.

        Note: This is a simple character-based chunking. It does not
        preserve word boundaries. Providers can override this method
        if they need smarter chunking logic.

        Args:
            text: Full message text
            max_size: Maximum characters per chunk

        Returns:
            List of text chunks (at least one, even for empty text)
        """
        if len(text) <= max_size:
            return [text]
        return [text[i:i + max_size] for i in range(0, len(text), max_size)]
