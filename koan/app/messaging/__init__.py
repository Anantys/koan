"""Messaging provider abstraction layer.

Decouples Kōan's communication logic from any specific messaging platform.
Supports Telegram (default) and Slack providers.

Usage:
    from app.messaging import get_messaging_provider
    provider = get_messaging_provider()
    provider.send_message("Hello from Kōan")
"""

import os
import sys
import threading
from typing import Dict, Optional, Type

from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE, MessagingProvider, Update, Message, Reaction

# Provider registry: name -> class
_providers: Dict[str, Type[MessagingProvider]] = {}

# Singleton instance (guarded by _instance_lock)
_instance: Optional[MessagingProvider] = None
_instance_lock = threading.Lock()

# List of known provider modules for auto-loading
_PROVIDER_MODULES = [
    "app.messaging.telegram",
    "app.messaging.slack",
]


def _write_error(message: str):
    """Write an error message to stderr."""
    print(f"[messaging] {message}", file=sys.stderr)


def register_provider(name: str):
    """Decorator to register a messaging provider class.

    Usage:
        @register_provider("telegram")
        class TelegramProvider(MessagingProvider):
            ...
    """
    def decorator(cls: Type[MessagingProvider]):
        _providers[name] = cls
        return cls
    return decorator


def _create_provider(name: str) -> MessagingProvider:
    """Create and configure a provider instance.

    Args:
        name: Provider identifier (must be registered)

    Returns:
        Configured MessagingProvider instance

    Raises:
        SystemExit: If provider unknown or configuration fails
    """
    # Ensure providers are imported (triggers @register_provider decorators)
    _ensure_providers_loaded()

    if name not in _providers:
        valid = ", ".join(sorted(_providers.keys())) or "(none loaded)"
        _write_error(f"Unknown messaging provider: {name!r}. Valid options: {valid}")
        raise SystemExit(1)

    instance = _providers[name]()
    if not instance.configure():
        raise SystemExit(1)

    return instance


def get_messaging_provider(provider_name_override: Optional[str] = None) -> MessagingProvider:
    """Get the active messaging provider (lazy singleton).

    Resolution order:
        1. provider_name_override parameter (for testing)
        2. KOAN_MESSAGING_PROVIDER env var
        3. messaging.provider from config.yaml
        4. Default: "telegram"

    Args:
        provider_name_override: Override provider name (bypasses singleton cache)

    Returns:
        Configured MessagingProvider instance

    Raises:
        SystemExit: If provider name is unknown or credentials are missing
    """
    global _instance

    if _instance is not None and provider_name_override is None:
        return _instance

    with _instance_lock:
        # Double-check under lock
        if _instance is not None and provider_name_override is None:
            return _instance

        name = provider_name_override or _resolve_provider_name()
        instance = _create_provider(name)

        if provider_name_override is None:
            _instance = instance

    return instance


def reset_provider():
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None


def _resolve_provider_name() -> str:
    """Resolve provider name from env var or config."""
    name = os.environ.get("KOAN_MESSAGING_PROVIDER", "")
    if name:
        return name.lower().strip()

    try:
        from app.utils import load_config
        config = load_config()
        messaging = config.get("messaging", {})
        if isinstance(messaging, dict):
            name = messaging.get("provider", "")
            if name:
                return name.lower().strip()
    except (ImportError, AttributeError):
        pass
    except Exception as e:
        _write_error(f"Error reading messaging config: {e}")

    return "telegram"


def _ensure_providers_loaded():
    """Import provider modules to trigger registration."""
    if _providers:
        return

    for module_name in _PROVIDER_MODULES:
        try:
            __import__(module_name)
        except ImportError:
            pass


__all__ = [
    "DEFAULT_MAX_MESSAGE_SIZE",
    "MessagingProvider",
    "Update",
    "Message",
    "Reaction",
    "get_messaging_provider",
    "register_provider",
    "reset_provider",
]
