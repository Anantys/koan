#!/usr/bin/env python3
"""
Kōan — Messaging notification helper

Standalone module to send messages from any process (awake.py, run.py, workers).
Delegates to the active MessagingProvider (Telegram by default).

Usage from shell:
    python3 notify.py "Mission completed: security audit"

Usage from Python:
    from app.notify import send_telegram
    send_telegram("Mission completed: security audit")
"""

import os
import subprocess
import sys
from pathlib import Path

from app.utils import load_dotenv


def reset_flood_state():
    """Reset flood protection state on the active provider (for tests)."""
    try:
        from app.messaging import get_messaging_provider
        provider = get_messaging_provider()
        if hasattr(provider, "reset_flood_state"):
            provider.reset_flood_state()
    except SystemExit:
        pass


def _send_raw_bypass_flood(text: str) -> bool:
    """Send a message bypassing flood protection for testing. Returns True on success.

    Only used by reset_flood_state() and tests. In production, always use send_telegram().
    Falls back to direct API call when provider unavailable (CLI standalone mode).
    """
    try:
        from app.messaging import get_messaging_provider
        # Temporarily reset flood state, send, then restore would be complex.
        # For now, access provider's reset method if available.
        # This is a test-only function, so some coupling is acceptable.
        provider = get_messaging_provider()
        if hasattr(provider, "_send_raw"):
            # TelegramProvider has _send_raw that bypasses flood protection
            return provider._send_raw(text)
        # For other providers without flood protection, regular send is fine
        return provider.send_message(text)
    except SystemExit:
        # Provider not configured — fall back to direct send for CLI usage
        return _direct_send(text)


def _direct_send(text: str) -> bool:
    """Direct Telegram API send (standalone fallback when provider unavailable)."""
    import requests

    load_dotenv()
    bot_token = os.environ.get("KOAN_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("KOAN_TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        print("[notify] KOAN_TELEGRAM_TOKEN or KOAN_TELEGRAM_CHAT_ID not set.",
              file=sys.stderr)
        return False

    api_base = f"https://api.telegram.org/bot{bot_token}"
    
    # Use same chunking algorithm as MessagingProvider.chunk_message()
    # to ensure consistent behavior between provider and fallback path
    from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE
    chunks = [text[i:i + DEFAULT_MAX_MESSAGE_SIZE] for i in range(0, len(text), DEFAULT_MAX_MESSAGE_SIZE)] if text else [text]
    
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(
                f"{api_base}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                print(f"[notify] Telegram API error: {resp.text[:200]}",
                      file=sys.stderr)
                ok = False
        except (requests.RequestException, ValueError) as e:
            print(f"[notify] Send error: {e}", file=sys.stderr)
            ok = False
    return ok


def send_telegram(text: str) -> bool:
    """Send a message via the active messaging provider (with flood protection).

    Backward-compatible facade — existing call sites continue to work unchanged.
    Returns True on success (suppression counts as success).
    """
    try:
        from app.messaging import get_messaging_provider
        provider = get_messaging_provider()
        return provider.send_message(text)
    except SystemExit:
        return _direct_send(text)


def format_and_send(raw_message: str, instance_dir: str = None,
                     project_name: str = "") -> bool:
    """Format a message through Claude with Kōan's personality, then send to Telegram.

    Every message sent to Telegram should go through this function to ensure
    consistent personality and readability on mobile.

    Args:
        raw_message: The raw/technical message to format
        instance_dir: Path to instance directory (auto-detected from KOAN_ROOT if None)
        project_name: Optional project name for scoped memory context

    Returns:
        True if message was sent successfully
    """
    from app.format_outbox import (
        format_message, load_soul, load_human_prefs,
        load_memory_context, fallback_format
    )

    if not instance_dir:
        load_dotenv()
        koan_root = os.environ.get("KOAN_ROOT", "")
        if koan_root:
            instance_dir = str(Path(koan_root) / "instance")
        else:
            # Can't format without instance dir — send raw with basic cleanup
            return send_telegram(fallback_format(raw_message))

    instance_path = Path(instance_dir)
    try:
        soul = load_soul(instance_path)
        prefs = load_human_prefs(instance_path)
        memory = load_memory_context(instance_path, project_name)
        formatted = format_message(raw_message, soul, prefs, memory)

        # Expand bare #123 GitHub refs to full clickable URLs
        if project_name:
            try:
                from app.projects_merged import get_github_url
                from app.text_utils import expand_github_refs
                github_url = get_github_url(project_name)
                if github_url:
                    formatted = expand_github_refs(formatted, github_url)
            except Exception as e:
                print(f"[notify] GitHub ref expansion failed: {e}",
                      file=sys.stderr)

        return send_telegram(formatted)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"[notify] Format error, sending fallback: {e}", file=sys.stderr)
        return send_telegram(fallback_format(raw_message))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} [--format] <message>", file=sys.stderr)
        print(f"  --format: Format through Claude before sending", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    use_format = False
    if args[0] == "--format":
        use_format = True
        args = args[1:]

    if not args:
        print(f"Usage: {sys.argv[0]} [--format] <message>", file=sys.stderr)
        sys.exit(1)

    message = " ".join(args)

    if use_format:
        project_name = os.environ.get("KOAN_CURRENT_PROJECT", "")
        success = format_and_send(message, project_name=project_name)
    else:
        success = send_telegram(message)
    sys.exit(0 if success else 1)
