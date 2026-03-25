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

import logging
import os
import subprocess
import sys
import threading
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

from app.utils import load_dotenv


class NotificationPriority(Enum):
    """Four-level notification priority system.

    Priority ranks (higher = more important):
        urgent=3  — critical failures, quota exhausted
        action=2  — mission complete, command responses (default)
        warning=1 — quota low, focus validation
        info=0    — progress updates, reflections
    """
    INFO = 0
    WARNING = 1
    ACTION = 2
    URGENT = 3


# mtime-based file read cache for format_and_send context files.
# Keyed by (function_name, instance_dir, project_name) -> (result, mtime_signature).
# Thread-safe via _file_cache_lock.
_file_cache: Dict[str, Tuple[str, float]] = {}
_file_cache_lock = threading.Lock()

# Sentinel returned when a notification is suppressed by min_priority filtering.
# Truthy so fire-and-forget callers (`if send_telegram(...)`) still treat it as "ok",
# but distinguishable from True for callers that need to know delivery vs suppression.
NOTIFICATION_SUPPRESSED = "suppressed"

# Valid priority names for config parsing (lowercase)
_PRIORITY_NAME_MAP = {
    "info": NotificationPriority.INFO,
    "warning": NotificationPriority.WARNING,
    "action": NotificationPriority.ACTION,
    "urgent": NotificationPriority.URGENT,
}


def _get_min_priority() -> NotificationPriority:
    """Load min_priority from config at call time.

    Reads notifications.min_priority from instance/config.yaml.
    Defaults to ACTION if missing or invalid.

    Returns:
        NotificationPriority threshold — messages below this rank are suppressed.
    """
    try:
        from app.utils import load_config
        config = load_config()
        notifications = config.get("notifications", {})
        if not isinstance(notifications, dict):
            return NotificationPriority.ACTION
        raw = notifications.get("min_priority", "action")
        if isinstance(raw, str):
            key = raw.strip().lower()
            result = _PRIORITY_NAME_MAP.get(key)
            if result is not None:
                return result
            log.warning("Invalid min_priority value '%s', defaulting to 'action'.", raw)
    except Exception as e:
        log.warning("Could not load min_priority from config: %s", e)
    return NotificationPriority.ACTION


def _write_suppressed_to_journal(text: str, priority: NotificationPriority):
    """Write a suppressed notification to the daily journal.

    Used when a message's priority is below min_priority. Messages are preserved
    in the journal under a "notifications" project entry so nothing is lost.

    Args:
        text: The notification text that was suppressed
        priority: The priority level of the suppressed message
    """
    try:
        from datetime import datetime as _dt
        from app.utils import load_dotenv as _load_dotenv
        import os as _os

        _load_dotenv()
        koan_root = _os.environ.get("KOAN_ROOT", "")
        if not koan_root:
            return

        from app.journal import append_to_journal
        instance_dir = Path(koan_root) / "instance"
        timestamp = _dt.now().strftime("%H:%M:%S")
        entry = (
            f"\n### [{timestamp}] Suppressed ({priority.name.lower()})\n\n"
            f"{text.strip()}\n"
        )
        append_to_journal(instance_dir, "notifications", entry)
    except Exception as e:
        log.warning("Failed to write suppressed message to journal: %s", e)


class TypingIndicator:
    """Context manager that sends typing indicators at regular intervals.

    Telegram's typing indicator expires after ~5 seconds. This keeps
    re-sending it every `interval` seconds in a background thread until
    the context exits.

    Usage:
        with TypingIndicator():
            # ... long-running operation ...
    """

    def __init__(self, interval: float = 4.0):
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self):
        send_typing()  # Send immediately
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False

    def _loop(self):
        while not self._stop_event.wait(self._interval):
            send_typing()


def send_typing() -> bool:
    """Send a typing indicator via the active messaging provider."""
    try:
        from app.messaging import get_messaging_provider
        provider = get_messaging_provider()
        return provider.send_typing()
    except SystemExit:
        return False


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
    """Direct Telegram API send (standalone fallback when provider unavailable).

    Retries each chunk up to 3 times with exponential backoff (1s/2s/4s)
    on transient network failures.
    """
    import requests
    from app.retry import retry_with_backoff

    load_dotenv()
    bot_token = os.environ.get("KOAN_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("KOAN_TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        print("[notify] KOAN_TELEGRAM_TOKEN or KOAN_TELEGRAM_CHAT_ID not set.",
              file=sys.stderr)
        return False

    api_base = f"https://api.telegram.org/bot{bot_token}"

    # Auto-detect markdown code blocks and convert to HTML for rendering
    parse_mode = None
    if "```" in text:
        from app.messaging.telegram import _markdown_to_html
        text = _markdown_to_html(text)
        parse_mode = "HTML"

    # Use same chunking algorithm as MessagingProvider.chunk_message()
    # to ensure consistent behavior between provider and fallback path
    from app.messaging.base import DEFAULT_MAX_MESSAGE_SIZE
    chunks = [text[i:i + DEFAULT_MAX_MESSAGE_SIZE] for i in range(0, len(text), DEFAULT_MAX_MESSAGE_SIZE)] if text else [text]

    total = len(chunks)
    sent = 0
    failed = 0
    for chunk in chunks:
        try:
            if retry_with_backoff(
                lambda c=chunk, pm=parse_mode: _direct_send_chunk(api_base, chat_id, c, pm),
                retryable=(requests.RequestException, ValueError),
                label="telegram direct send",
            ):
                sent += 1
            else:
                failed += 1
        except (requests.RequestException, ValueError) as e:
            print(f"[notify] Send error after retries: {e}", file=sys.stderr)
            failed += 1

    if failed and sent:
        # Partial delivery — notify the recipient
        notice = f"[⚠️ Message truncated: {sent}/{total} parts delivered, {failed} failed]"
        try:
            _direct_send_chunk(api_base, chat_id, notice, parse_mode)
        except Exception as e:
            print(f"[notify] Failed to send truncation notice: {e}",
                  file=sys.stderr)

    return failed == 0


def _direct_send_chunk(api_base: str, chat_id: str, chunk: str,
                       parse_mode: str = None) -> bool:
    """Send a single message chunk via Telegram API. Raises on network error."""
    import requests

    payload = {"chat_id": chat_id, "text": chunk}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = requests.post(
        f"{api_base}/sendMessage",
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"[notify] Telegram API error: {resp.text[:200]}",
              file=sys.stderr)
        return False
    return True


def _apply_priority_emoji(text: str, priority: NotificationPriority) -> str:
    """Prepend priority emoji to text for urgent and warning messages.

    Idempotent: does not prepend if text already starts with the emoji.
    action and info levels get no prefix.

    Args:
        text: Message text
        priority: Notification priority level

    Returns:
        Text with emoji prepended if appropriate
    """
    _PRIORITY_EMOJIS = {
        NotificationPriority.URGENT: "🚨",
        NotificationPriority.WARNING: "⚠️",
    }
    emoji = _PRIORITY_EMOJIS.get(priority)
    if emoji and not text.startswith(emoji):
        return f"{emoji} {text}"
    return text


def send_telegram(text: str,
                  priority: NotificationPriority = NotificationPriority.ACTION) -> bool:
    """Send a message via the active messaging provider (with flood protection).

    Retry logic is handled at the HTTP request level inside the provider's
    _send_raw() and notify's _direct_send(), so transient network failures
    are retried transparently (up to 3 attempts with 1s/2s/4s backoff).

    Messages with priority below the configured min_priority are suppressed from
    Telegram and written to the daily journal instead (nothing is lost).

    Args:
        text: Message text to send
        priority: Notification priority level (default: ACTION)

    Returns:
        True if the message was delivered successfully.
        NOTIFICATION_SUPPRESSED if the message was silently dropped by min_priority.
        False if sending failed.
    """
    # Check priority filter before sending
    min_priority = _get_min_priority()
    if priority.value < min_priority.value:
        _write_suppressed_to_journal(text, priority)
        return NOTIFICATION_SUPPRESSED

    # Prepend priority emoji for urgent and warning messages (idempotent)
    text = _apply_priority_emoji(text, priority)

    try:
        from app.messaging import get_messaging_provider
        provider = get_messaging_provider()
        return provider.send_message(text)
    except SystemExit:
        return _direct_send(text)


def _get_file_mtime(path: Path) -> float:
    """Get file mtime or 0 if missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cached_file_read(cache_key: str, paths: list, loader):
    """Return cached result if file mtimes unchanged, else re-read.

    Args:
        cache_key: Unique key for this cache entry
        paths: List of Path objects to check mtimes against
        loader: Callable that returns the fresh value
    """
    current_mtime = max((_get_file_mtime(p) for p in paths), default=0.0)
    with _file_cache_lock:
        cached = _file_cache.get(cache_key)
        if cached is not None:
            value, cached_mtime = cached
            if cached_mtime == current_mtime:
                return value

    # Cache miss or mtime changed — re-read
    value = loader()
    with _file_cache_lock:
        _file_cache[cache_key] = (value, current_mtime)
    return value


def invalidate_file_cache():
    """Clear the file read cache (for tests)."""
    with _file_cache_lock:
        _file_cache.clear()


def format_and_send(raw_message: str, instance_dir: str = None,
                     project_name: str = "",
                     priority: NotificationPriority = NotificationPriority.ACTION) -> bool:
    """Format a message through Claude with Kōan's personality, then send to Telegram.

    Every message sent to Telegram should go through this function to ensure
    consistent personality and readability on mobile.

    Args:
        raw_message: The raw/technical message to format
        instance_dir: Path to instance directory (auto-detected from KOAN_ROOT if None)
        project_name: Optional project name for scoped memory context
        priority: Notification priority level (default: ACTION)

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
            return send_telegram(fallback_format(raw_message), priority=priority)

    instance_path = Path(instance_dir)
    try:
        # Use mtime-based caching for context files that rarely change
        soul_file = instance_path / "soul.md"
        soul = _cached_file_read(
            f"soul:{instance_dir}",
            [soul_file],
            lambda: load_soul(instance_path),
        )

        prefs_file = instance_path / "memory" / "global" / "human-preferences.md"
        prefs = _cached_file_read(
            f"prefs:{instance_dir}",
            [prefs_file],
            lambda: load_human_prefs(instance_path),
        )

        # Memory context reads up to 4 files — cache by max mtime across all
        memory_files = [
            instance_path / "memory" / "global" / "personality-evolution.md",
            instance_path / "memory" / "global" / "emotional-memory.md",
            instance_path / "memory" / "summary.md",
        ]
        if project_name:
            memory_files.append(
                instance_path / "memory" / "projects" / project_name / "learnings.md"
            )
        memory = _cached_file_read(
            f"memory:{instance_dir}:{project_name}",
            memory_files,
            lambda: load_memory_context(instance_path, project_name),
        )

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

        return send_telegram(formatted, priority=priority)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"[notify] Format error, sending fallback: {e}", file=sys.stderr)
        return send_telegram(fallback_format(raw_message), priority=priority)


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
