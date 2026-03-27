"""Phase 8: Integration tests for messaging provider abstraction.

Tests backward compatibility, provider switching, and migration paths
to validate that the messaging abstraction doesn't break existing behavior.
"""

from pathlib import Path
from typing import Callable
from unittest.mock import patch, MagicMock

import pytest

from app.messaging import (
    get_messaging_provider, reset_provider, register_provider,
    _providers, _resolve_provider_name,
)
from app.messaging.base import MessagingProvider, Update, Message


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_provider_state():
    """Reset singleton between tests."""
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def dummy_provider_class():
    """Minimal concrete provider for testing."""
    class DummyProvider(MessagingProvider):
        def __init__(self):
            self._configured = False

        def configure(self) -> bool:
            self._configured = True
            return True

        def send_message(self, text, **kw) -> bool:
            return True

        def poll_updates(self):
            return []

        def get_provider_name(self) -> str:
            return "dummy"

        def get_channel_id(self) -> str:
            return "test-channel"

    return DummyProvider


@pytest.fixture
def register_dummy_provider(dummy_provider_class):
    """Register and cleanup dummy provider."""
    _providers["dummy"] = dummy_provider_class
    yield
    _providers.pop("dummy", None)


@pytest.fixture
def prompts_dir():
    """Path to system prompts directory."""
    return Path(__file__).parent.parent / "system-prompts"


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

def create_history_file(directory: Path, filename: str, content: str) -> Path:
    """Helper to create history JSONL files for migration tests."""
    file_path = directory / filename
    file_path.write_text(content)
    return file_path


def clear_provider_env_vars(monkeypatch, provider: str):
    """Helper to clear all environment variables for a provider."""
    if provider == "telegram":
        monkeypatch.delenv("KOAN_TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("KOAN_TELEGRAM_CHAT_ID", raising=False)
    elif provider == "slack":
        monkeypatch.delenv("KOAN_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("KOAN_SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("KOAN_SLACK_CHANNEL_ID", raising=False)


def set_provider_env_vars(monkeypatch, provider: str):
    """Helper to set minimal environment variables for a provider."""
    if provider == "telegram":
        monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "test-token")
        monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "12345")
    elif provider == "slack":
        monkeypatch.setenv("KOAN_SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("KOAN_SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("KOAN_SLACK_CHANNEL_ID", "C12345")


# ---------------------------------------------------------------------------
# 1. Provider Switching
# ---------------------------------------------------------------------------

class TestProviderSwitching:
    """Verify KOAN_MESSAGING_PROVIDER correctly selects the provider."""

    @pytest.mark.parametrize("provider_input,expected", [
        ("telegram", "telegram"),
        ("slack", "slack"),
        ("Telegram", "telegram"),  # Case insensitive
        (" slack ", "slack"),      # Strips whitespace
    ])
    def test_env_var_selects_provider(self, monkeypatch, provider_input, expected):
        monkeypatch.setenv("KOAN_MESSAGING_PROVIDER", provider_input)
        assert _resolve_provider_name() == expected

    def test_default_is_telegram(self, monkeypatch):
        monkeypatch.delenv("KOAN_MESSAGING_PROVIDER", raising=False)
        with patch("app.messaging.load_config", return_value={}, create=True):
            assert _resolve_provider_name() == "telegram"

    def test_config_yaml_fallback(self, monkeypatch):
        monkeypatch.delenv("KOAN_MESSAGING_PROVIDER", raising=False)
        config = {"messaging": {"provider": "slack"}}
        with patch("app.utils.load_config", return_value=config):
            assert _resolve_provider_name() == "slack"

    def test_env_var_overrides_config(self, monkeypatch):
        monkeypatch.setenv("KOAN_MESSAGING_PROVIDER", "telegram")
        config = {"messaging": {"provider": "slack"}}
        with patch("app.utils.load_config", return_value=config):
            assert _resolve_provider_name() == "telegram"


# ---------------------------------------------------------------------------
# 2. Provider Instantiation with Override
# ---------------------------------------------------------------------------

class TestProviderOverride:
    """Test get_messaging_provider with override parameter."""

    def test_override_bypasses_singleton(self, register_dummy_provider):
        p1 = get_messaging_provider(provider_name_override="dummy")
        p2 = get_messaging_provider(provider_name_override="dummy")
        assert p1 is not p2, "Override should create new instances"

    def test_singleton_reused_without_override(self, monkeypatch, register_dummy_provider):
        monkeypatch.setenv("KOAN_MESSAGING_PROVIDER", "dummy")
        p1 = get_messaging_provider()
        p2 = get_messaging_provider()
        assert p1 is p2, "Without override, singleton should be reused"


# ---------------------------------------------------------------------------
# 3. Backward Compatibility: Import Aliases
# ---------------------------------------------------------------------------

class TestBackwardCompatImports:
    """Ensure old import paths and function names remain accessible."""

    @pytest.mark.parametrize("module,function_name", [
        ("app.notify", "send_telegram"),
        ("app.notify", "format_and_send"),
        ("app.notify", "reset_flood_state"),
    ])
    def test_notify_functions_importable(self, module, function_name):
        mod = __import__(module, fromlist=[function_name])
        func = getattr(mod, function_name)
        assert callable(func), f"{module}.{function_name} should be callable"

    @pytest.mark.parametrize("function_name", [
        "save_telegram_message",
        "load_recent_telegram_history",
        "compact_telegram_history",
    ])
    def test_telegram_history_aliases_in_utils(self, function_name):
        from app.utils import __dict__ as utils_dict
        assert function_name in utils_dict, f"app.utils.{function_name} should exist"
        assert callable(utils_dict[function_name])

    @pytest.mark.parametrize("function_name", [
        "save_conversation_message",
        "load_recent_history",
        "format_conversation_history",
        "compact_history",
    ])
    def test_conversation_history_functions_importable(self, function_name):
        from app.conversation_history import __dict__ as history_dict
        assert function_name in history_dict
        assert callable(history_dict[function_name])


# ---------------------------------------------------------------------------
# 4. Backward Compatibility: Notify Facade Delegation
# ---------------------------------------------------------------------------

class TestNotifyFacadeDelegation:
    """Verify notify.send_telegram delegates to messaging provider."""

    def test_send_telegram_delegates_to_provider(self):
        mock_provider = MagicMock(spec=MessagingProvider)
        mock_provider.send_message.return_value = True

        with patch("app.messaging.get_messaging_provider", return_value=mock_provider):
            from app.notify import send_telegram
            result = send_telegram("test message")

        mock_provider.send_message.assert_called_once_with("test message")
        assert result is True

    def test_send_telegram_falls_back_on_exit(self):
        """When provider raises SystemExit, falls back to direct send."""
        with patch("app.messaging.get_messaging_provider", side_effect=SystemExit(1)), \
             patch("app.notify._direct_send", return_value=True) as mock_direct:
            from app.notify import send_telegram
            result = send_telegram("fallback test")

        mock_direct.assert_called_once_with("fallback test")
        assert result is True


# ---------------------------------------------------------------------------
# 5. Conversation History Migration
# ---------------------------------------------------------------------------

class TestConversationHistoryMigration:
    """Test migration from telegram-history.jsonl to conversation-history.jsonl."""

    def test_migrate_renames_old_file(self, tmp_path):
        old_file = create_history_file(tmp_path, "telegram-history.jsonl", '{"role":"user","text":"hello"}\n')
        new_file = tmp_path / "conversation-history.jsonl"

        # Simulate migration logic
        if old_file.exists() and not new_file.exists():
            old_file.rename(new_file)

        assert not old_file.exists(), "Old file should be removed"
        assert new_file.exists(), "New file should exist"
        assert "hello" in new_file.read_text()

    def test_migration_skips_if_new_exists(self, tmp_path):
        old_file = create_history_file(tmp_path, "telegram-history.jsonl", '{"old": true}\n')
        new_file = create_history_file(tmp_path, "conversation-history.jsonl", '{"new": true}\n')

        # Migration should NOT overwrite existing new file
        if old_file.exists() and not new_file.exists():
            old_file.rename(new_file)

        assert old_file.exists(), "Old file should be preserved"
        assert '{"new": true}' in new_file.read_text(), "New file should be unchanged"

    def test_migration_noop_if_no_old_file(self, tmp_path):
        old_file = tmp_path / "telegram-history.jsonl"
        new_file = tmp_path / "conversation-history.jsonl"

        if old_file.exists() and not new_file.exists():
            old_file.rename(new_file)

        assert not old_file.exists()
        assert not new_file.exists()

    def test_actual_migrate_history_file_function(self, tmp_path, monkeypatch):
        """Test the real _migrate_history_file() from bridge_state."""
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        old_file = instance_dir / "telegram-history.jsonl"
        old_file.write_text('{"role":"user","text":"migrated"}\n')

        import app.bridge_state as bs
        monkeypatch.setattr(bs, "INSTANCE_DIR", instance_dir)

        result = bs._migrate_history_file()
        expected = instance_dir / "conversation-history.jsonl"
        assert result == expected, "Should return new path"
        assert expected.exists(), "New file should exist"
        assert not old_file.exists(), "Old file should be removed"
        assert "migrated" in expected.read_text()

    def test_actual_migrate_returns_old_on_error(self, tmp_path, monkeypatch):
        """Test _migrate_history_file() returns old_path on OSError."""
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        old_file = instance_dir / "telegram-history.jsonl"
        old_file.write_text('{"test": true}\n')

        import app.bridge_state as bs
        monkeypatch.setattr(bs, "INSTANCE_DIR", instance_dir)

        # Make rename fail
        with patch.object(Path, "rename", side_effect=OSError("permission denied")):
            result = bs._migrate_history_file()
        assert result == old_file, "Should fall back to old path on error"


# ---------------------------------------------------------------------------
# 5b. Functional backward compatibility
# ---------------------------------------------------------------------------

class TestFunctionalBackwardCompat:
    """Verify old function names actually work, not just that they're importable."""

    def test_save_telegram_message_writes_to_file(self, tmp_path):
        from app.utils import save_telegram_message
        history_file = tmp_path / "history.jsonl"
        save_telegram_message(history_file, "user", "hello old API")
        content = history_file.read_text()
        assert '"role": "user"' in content
        assert '"text": "hello old API"' in content

    def test_load_recent_telegram_history_reads_file(self, tmp_path):
        from app.utils import save_telegram_message, load_recent_telegram_history
        history_file = tmp_path / "history.jsonl"
        save_telegram_message(history_file, "user", "msg1")
        save_telegram_message(history_file, "assistant", "msg2")
        messages = load_recent_telegram_history(history_file, max_messages=10)
        assert len(messages) == 2
        assert messages[0]["text"] == "msg1"

    def test_compact_telegram_history_works(self, tmp_path):
        from app.utils import save_telegram_message, compact_telegram_history
        history_file = tmp_path / "history.jsonl"
        topics_file = tmp_path / "topics.json"
        for i in range(25):
            save_telegram_message(history_file, "user", f"msg {i}")
        count = compact_telegram_history(history_file, topics_file)
        assert count == 25

class TestProviderLifecycle:
    """Test provider registration, configuration, and lifecycle.
    
    Consolidated tests for both Telegram and Slack providers.
    """

    @pytest.mark.parametrize("provider_name,module_name", [
        ("telegram", "app.messaging.telegram"),
        ("slack", "app.messaging.slack"),
    ])
    def test_provider_registered_after_import(self, provider_name, module_name):
        __import__(module_name)
        assert provider_name in _providers, f"{provider_name} should be registered"

    @pytest.mark.parametrize("provider", ["telegram", "slack"])
    def test_configure_fails_without_env_vars(self, monkeypatch, provider):
        clear_provider_env_vars(monkeypatch, provider)
        
        if provider == "telegram":
            from app.messaging.telegram import TelegramProvider
            with patch("app.utils.load_dotenv"):
                provider_instance = TelegramProvider()
                assert provider_instance.configure() is False
        else:
            from app.messaging.slack import SlackProvider
            provider_instance = SlackProvider()
            assert provider_instance.configure() is False

    def test_telegram_configure_succeeds_with_env(self, monkeypatch):
        set_provider_env_vars(monkeypatch, "telegram")
        
        from app.messaging.telegram import TelegramProvider
        provider = TelegramProvider()
        
        assert provider.configure() is True
        assert provider.get_provider_name() == "telegram"
        assert provider.get_channel_id() == "12345"

    def test_slack_configure_handles_missing_sdk(self, monkeypatch):
        """Slack SDK may not be installed in test env - shouldn't crash."""
        set_provider_env_vars(monkeypatch, "slack")
        
        from app.messaging.slack import SlackProvider
        provider = SlackProvider()
        result = provider.configure()
        
        assert isinstance(result, bool), "configure() should return boolean even if SDK missing"


# ---------------------------------------------------------------------------
# 7. MessagingProvider Base Contract
# ---------------------------------------------------------------------------

class TestMessagingProviderContract:
    """Verify the abstract base class contract."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            MessagingProvider()

    def test_dummy_satisfies_contract(self, dummy_provider_class):
        provider = dummy_provider_class()
        provider.configure()
        
        assert provider.send_message("hi") is True
        assert provider.poll_updates() == []
        assert provider.get_provider_name() == "dummy"
        assert provider.get_channel_id() == "test-channel"

    def test_chunk_message_default_implementation(self, dummy_provider_class):
        provider = dummy_provider_class()
        text = "a" * 5000
        chunks = provider.chunk_message(text, max_size=4000)
        
        assert len(chunks) == 2, "Should split into 2 chunks"
        assert len(chunks[0]) == 4000
        assert len(chunks[1]) == 1000

    @pytest.mark.parametrize("text,role,timestamp,expected_text,expected_role", [
        ("hello", "user", None, "hello", "user"),
        ("hi", "assistant", "2025-01-01", "hi", "assistant"),
    ])
    def test_message_dataclass(self, text, role, timestamp, expected_text, expected_role):
        message = Message(text=text, role=role, timestamp=timestamp)
        assert message.text == expected_text
        assert message.role == expected_role
        if timestamp:
            assert message.timestamp == timestamp

    def test_update_dataclass(self):
        message = Message(text="hello", role="user")
        update = Update(update_id=1, message=message, raw_data={"key": "val"})
        
        assert update.update_id == 1
        assert update.message.text == "hello"
        assert update.raw_data == {"key": "val"}


# ---------------------------------------------------------------------------
# 8. System Prompt File Rename
# ---------------------------------------------------------------------------

class TestFormatPromptRename:
    """Verify format-telegram.md was renamed to format-message.md."""

    def test_format_message_prompt_exists(self, prompts_dir):
        assert (prompts_dir / "format-message.md").exists(), \
            "format-message.md should exist in system-prompts"

    def test_old_format_telegram_prompt_removed(self, prompts_dir):
        assert not (prompts_dir / "format-telegram.md").exists(), \
            "format-telegram.md should be removed"

    def test_format_outbox_uses_new_name(self):
        """format_outbox.py should reference format-message, not format-telegram."""
        import app.format_outbox as fo
        import inspect
        
        source = inspect.getsource(fo)
        assert "format-message" in source, "Should reference format-message.md"
        assert "format-telegram" not in source, "Should not reference old format-telegram.md"
