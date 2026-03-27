"""Tests for messaging provider abstraction — registry, resolution, base class."""

import os
from unittest.mock import patch

import pytest

from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE, MessagingProvider, Update, Message


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

class MockProvider(MessagingProvider):
    """Minimal concrete implementation for testing base class methods."""

    def send_message(self, text: str) -> bool:
        return True

    def poll_updates(self, offset=None):
        return []

    def get_provider_name(self) -> str:
        return "mock"

    def get_channel_id(self) -> str:
        return "test-channel"

    def configure(self) -> bool:
        return True


@pytest.fixture
def clean_registry():
    """Reset provider registry before and after each test."""
    import app.messaging as m

    original_providers = m._providers.copy()
    original_instance = m._instance

    m._providers.clear()
    m._instance = None

    yield

    m._providers.clear()
    m._providers.update(original_providers)
    m._instance = original_instance


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class TestMessage:
    def test_message_defaults(self):
        msg = Message(text="hello", role="user")
        assert msg.text == "hello"
        assert msg.role == "user"
        assert msg.timestamp == ""
        assert msg.raw_data == {}

    def test_message_with_all_fields(self):
        msg = Message(
            text="hi",
            role="assistant",
            timestamp="2026-01-01T00:00:00",
            raw_data={"id": 42},
        )
        assert msg.timestamp == "2026-01-01T00:00:00"
        assert msg.raw_data["id"] == 42


class TestUpdate:
    def test_update_defaults(self):
        up = Update(update_id=1)
        assert up.update_id == 1
        assert up.message is None
        assert up.raw_data == {}

    def test_update_with_message(self):
        msg = Message(text="test", role="user")
        up = Update(update_id=5, message=msg)
        assert up.message.text == "test"


# ---------------------------------------------------------------------------
# chunk_message (base class helper)
# ---------------------------------------------------------------------------

class TestChunkMessage:
    def test_short_message_single_chunk(self):
        provider = MockProvider()
        assert provider.chunk_message("hello") == ["hello"]

    def test_exact_limit_single_chunk(self):
        provider = MockProvider()
        text = "a" * 4000
        assert provider.chunk_message(text) == [text]

    def test_long_message_multiple_chunks(self):
        provider = MockProvider()
        text = "a" * 10000
        chunks = provider.chunk_message(text, max_size=4000)
        assert len(chunks) == 3
        assert chunks[0] == "a" * 4000
        assert chunks[1] == "a" * 4000
        assert chunks[2] == "a" * 2000

    def test_empty_message_returns_single_chunk(self):
        provider = MockProvider()
        assert provider.chunk_message("") == [""]

    def test_custom_max_size(self):
        provider = MockProvider()
        chunks = provider.chunk_message("hello world", max_size=5)
        assert chunks == ["hello", " worl", "d"]

    def test_chunks_do_not_respect_word_boundaries(self):
        """Character-based chunking may split words."""
        provider = MockProvider()
        chunks = provider.chunk_message("hello", max_size=3)
        assert chunks == ["hel", "lo"]


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

class TestProviderRegistry:
    def test_register_provider_decorator(self, clean_registry):
        from app.messaging import register_provider
        import app.messaging as m

        @register_provider("test")
        class TestProvider(MockProvider):
            pass

        assert "test" in m._providers
        assert m._providers["test"] is TestProvider

    def test_register_multiple_providers(self, clean_registry):
        from app.messaging import register_provider
        import app.messaging as m

        @register_provider("provider1")
        class Provider1(MockProvider):
            pass

        @register_provider("provider2")
        class Provider2(MockProvider):
            pass

        assert len(m._providers) == 2
        assert "provider1" in m._providers
        assert "provider2" in m._providers

    def test_get_provider_unknown_name_exits(self, clean_registry):
        from app.messaging import get_messaging_provider

        with pytest.raises(SystemExit):
            get_messaging_provider(provider_name_override="nonexistent")

    def test_get_provider_with_override(self, clean_registry):
        from app.messaging import register_provider, get_messaging_provider
        import app.messaging as m

        @register_provider("custom")
        class CustomProvider(MockProvider):
            pass

        provider = get_messaging_provider(provider_name_override="custom")
        assert provider.get_provider_name() == "mock"
        assert m._instance is None  # Override doesn't set singleton

    def test_get_provider_singleton_behavior(self, clean_registry):
        from app.messaging import register_provider, get_messaging_provider

        @register_provider("telegram")
        class MockTelegram(MockProvider):
            pass

        with patch.dict(os.environ, {"KOAN_MESSAGING_PROVIDER": "telegram"}):
            provider1 = get_messaging_provider()
            provider2 = get_messaging_provider()
            assert provider1 is provider2

    def test_reset_provider_clears_singleton(self, clean_registry):
        from app.messaging import register_provider, get_messaging_provider, reset_provider
        import app.messaging as m

        @register_provider("telegram")
        class MockTelegram(MockProvider):
            pass

        with patch.dict(os.environ, {"KOAN_MESSAGING_PROVIDER": "telegram"}):
            get_messaging_provider()
            assert m._instance is not None
            reset_provider()
            assert m._instance is None

    def test_configure_failure_exits(self, clean_registry):
        from app.messaging import register_provider, get_messaging_provider

        @register_provider("bad")
        class FailingProvider(MockProvider):
            def configure(self):
                return False

        with pytest.raises(SystemExit):
            get_messaging_provider(provider_name_override="bad")


# ---------------------------------------------------------------------------
# Provider name resolution
# ---------------------------------------------------------------------------

class TestProviderResolution:
    def test_resolve_from_env_var(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {"KOAN_MESSAGING_PROVIDER": "slack"}):
            assert _resolve_provider_name() == "slack"

    def test_resolve_from_env_var_with_whitespace(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {"KOAN_MESSAGING_PROVIDER": "  SLACK  "}):
            assert _resolve_provider_name() == "slack"

    def test_resolve_from_config(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_MESSAGING_PROVIDER", None)
            with patch(
                "app.utils.load_config",
                return_value={"messaging": {"provider": "slack"}},
            ):
                assert _resolve_provider_name() == "slack"

    def test_resolve_from_config_with_case_normalization(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_MESSAGING_PROVIDER", None)
            with patch(
                "app.utils.load_config",
                return_value={"messaging": {"provider": "TELEGRAM"}},
            ):
                assert _resolve_provider_name() == "telegram"

    def test_resolve_defaults_to_telegram(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_MESSAGING_PROVIDER", None)
            with patch("app.utils.load_config", return_value={}):
                assert _resolve_provider_name() == "telegram"

    def test_resolve_handles_invalid_config_structure(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_MESSAGING_PROVIDER", None)
            with patch("app.utils.load_config", return_value={"messaging": "invalid"}):
                assert _resolve_provider_name() == "telegram"

    def test_env_var_takes_precedence_over_config(self):
        from app.messaging import _resolve_provider_name

        with patch.dict(os.environ, {"KOAN_MESSAGING_PROVIDER": "slack"}):
            with patch(
                "app.utils.load_config",
                return_value={"messaging": {"provider": "telegram"}},
            ):
                assert _resolve_provider_name() == "slack"


# ---------------------------------------------------------------------------
# DEFAULT_MAX_MESSAGE_SIZE constant consistency
# ---------------------------------------------------------------------------

class TestDefaultMaxMessageSize:
    """Verify the shared constant is used consistently across all providers."""

    def test_constant_value(self):
        assert DEFAULT_MAX_MESSAGE_SIZE == 4000

    def test_exported_from_package(self):
        from app.messaging import DEFAULT_MAX_MESSAGE_SIZE as pkg_const
        assert pkg_const == 4000
        assert pkg_const is DEFAULT_MAX_MESSAGE_SIZE

    def test_telegram_uses_shared_constant(self):
        from app.messaging.telegram import MAX_MESSAGE_SIZE
        assert MAX_MESSAGE_SIZE == DEFAULT_MAX_MESSAGE_SIZE

    def test_slack_uses_shared_constant(self):
        from app.messaging.slack import MAX_MESSAGE_SIZE
        assert MAX_MESSAGE_SIZE == DEFAULT_MAX_MESSAGE_SIZE

    def test_chunk_message_default_matches_constant(self):
        """Base class chunk_message default matches DEFAULT_MAX_MESSAGE_SIZE."""
        provider = MockProvider()
        # A message exactly at the limit should be a single chunk
        text = "a" * DEFAULT_MAX_MESSAGE_SIZE
        assert provider.chunk_message(text) == [text]
        # One char over should split into two chunks
        text_plus_one = "a" * (DEFAULT_MAX_MESSAGE_SIZE + 1)
        chunks = provider.chunk_message(text_plus_one)
        assert len(chunks) == 2
        assert len(chunks[0]) == DEFAULT_MAX_MESSAGE_SIZE


# ---------------------------------------------------------------------------
# send_typing (base class default)
# ---------------------------------------------------------------------------

class TestSendTypingBase:
    def test_default_send_typing_returns_true(self):
        """Base class send_typing is a no-op that returns True."""
        provider = MockProvider()
        assert provider.send_typing() is True
