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
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.utils import load_dotenv


# mtime-based file read cache for format_and_send context files.
# Keyed by (function_name, instance_dir, project_name) -> (result, mtime_signature).
# Thread-safe via _file_cache_lock.
_file_cache: Dict[str, Tuple[str, float]] = {}
_file_cache_lock = threading.Lock()


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
        except Exception:
            pass  # Best-effort notice

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


def send_telegram(text: str) -> bool:
    """Send a message via the active messaging provider (with flood protection).

    Retry logic is handled at the HTTP request level inside the provider's
    _send_raw() and notify's _direct_send(), so transient network failures
    are retried transparently (up to 3 attempts with 1s/2s/4s backoff).

    Returns True on success (suppression counts as success).
    """
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
