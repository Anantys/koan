#!/usr/bin/env python3
"""Dedicated chat process — handles Telegram chat messages independently.

Runs as a separate process from awake.py and run.py. Watches
``instance/chat-inbox.jsonl`` for incoming chat requests and invokes
Claude CLI to generate responses, sending them directly via Telegram.

This decouples chat from mission execution: even when a mission is
hammering the API, the chat process has its own subprocess pipeline
and won't be starved.

Architecture:
- awake.py writes chat requests to chat-inbox.jsonl (atomic append)
- This process polls the inbox, processes FIFO, invokes Claude CLI
- Responses are sent directly via send_telegram()
- Conversation history is written here (not in awake.py) to avoid races

See issue #1084 for motivation.
"""

import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.utils import load_dotenv

load_dotenv()

KOAN_ROOT = Path(os.environ["KOAN_ROOT"])
INSTANCE_DIR = KOAN_ROOT / "instance"

# File paths
CHAT_INBOX = INSTANCE_DIR / "chat-inbox.jsonl"
CONVERSATION_HISTORY_FILE = INSTANCE_DIR / "conversation-history.jsonl"
MISSIONS_FILE = INSTANCE_DIR / "missions.md"

# Poll interval for inbox checking (seconds)
INBOX_POLL_INTERVAL = 0.5

# Graceful shutdown flag
_shutdown_requested = False


def _on_sigterm(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


def _load_soul() -> str:
    """Load soul.md content."""
    soul_path = INSTANCE_DIR / "soul.md"
    if soul_path.exists():
        return soul_path.read_text()
    return ""


def _load_summary() -> str:
    """Load summary.md content."""
    summary_path = INSTANCE_DIR / "memory" / "summary.md"
    if summary_path.exists():
        return summary_path.read_text()
    return ""


def _resolve_project_path() -> str:
    """Get the first project's path for CLI cwd fallback."""
    try:
        from app.utils import get_known_projects
        projects = get_known_projects()
        if projects:
            return projects[0][1]
    except Exception:
        pass
    return ""


def read_and_clear_inbox() -> list:
    """Atomically read all pending chat requests and clear the inbox.

    Returns a list of dicts, each with keys: text, timestamp.
    """
    if not CHAT_INBOX.exists():
        return []

    entries = []
    try:
        with open(CHAT_INBOX, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                if entries:
                    f.seek(0)
                    f.truncate()
                    f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass

    return entries


def write_to_inbox(text: str) -> None:
    """Append a chat request to the inbox file (called from awake.py).

    Uses file locking for safe concurrent access.
    """
    entry = json.dumps({
        "text": text,
        "timestamp": datetime.now().isoformat(),
    })
    try:
        with open(CHAT_INBOX, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(entry + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"[chat] Failed to write to inbox: {e}", file=sys.stderr)


def has_pending_requests() -> bool:
    """Check if there are unprocessed chat requests in the inbox."""
    if not CHAT_INBOX.exists():
        return False
    try:
        return CHAT_INBOX.stat().st_size > 0
    except OSError:
        return False


def process_chat_request(text: str, soul: str, summary: str, project_path: str) -> None:
    """Process a single chat request: build prompt, call Claude, send response.

    All conversation history writes happen here to avoid races with awake.py.
    """
    from app.chat_context import build_chat_prompt, clean_chat_response
    from app.cli_exec import run_cli
    from app.cli_provider import build_full_command
    from app.config import get_chat_tools, get_model_config
    from app.conversation_history import save_conversation_message
    from app.notify import TypingIndicator, send_telegram
    from app.prompt_guard import scan_mission_text
    from app.config import get_prompt_guard_config

    # Save user message to history
    save_conversation_message(CONVERSATION_HISTORY_FILE, "user", text)

    # Scan for prompt injection (warn-only — chat tools are read-only)
    guard_config = get_prompt_guard_config()
    if guard_config["enabled"]:
        guard_result = scan_mission_text(text)
        if guard_result.blocked:
            _log(f"WARNING chat guard: {guard_result.reason} | {text[:100]}")

    chat_timeout = int(os.environ.get("KOAN_CHAT_TIMEOUT", "180"))

    prompt = build_chat_prompt(
        text,
        instance_dir=INSTANCE_DIR,
        koan_root=KOAN_ROOT,
        soul=soul,
        summary=summary,
        conversation_history_file=CONVERSATION_HISTORY_FILE,
        missions_file=MISSIONS_FILE,
        project_path=project_path,
    )

    chat_tools_list = get_chat_tools().split(",")
    models = get_model_config()

    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=chat_tools_list,
        model=models["chat"],
        fallback=models["fallback"],
        max_turns=1,
    )

    import subprocess

    with TypingIndicator():
        try:
            result = run_cli(
                cmd,
                capture_output=True, text=True, timeout=chat_timeout,
                cwd=project_path or str(KOAN_ROOT),
            )
            response = clean_chat_response(result.stdout.strip(), text)
            if response:
                send_telegram(response)
                msg_id = _get_last_message_id()
                save_conversation_message(
                    CONVERSATION_HISTORY_FILE, "assistant", response,
                    message_id=msg_id, message_type="chat",
                )
                _log(f"Chat reply: {response[:80]}...")
            elif result.returncode != 0:
                _log(f"Claude error: {result.stderr[:200]}")
                error_msg = "⚠️ Hmm, I couldn't formulate a response. Try again?"
                send_telegram(error_msg)
                save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", error_msg)
            else:
                _log("Empty response from Claude — retrying with lite context...")
                _retry_with_lite(text, soul, summary, project_path, chat_timeout, chat_tools_list, models)
        except subprocess.TimeoutExpired:
            _log(f"Claude timed out ({chat_timeout}s). Retrying with lite context...")
            time.sleep(4)
            _retry_with_lite(text, soul, summary, project_path, chat_timeout, chat_tools_list, models)
        except Exception as e:
            _log(f"Claude error: {e}")
            error_msg = "⚠️ Something went wrong — try again?"
            send_telegram(error_msg)
            save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", error_msg)


def _retry_with_lite(text, soul, summary, project_path, chat_timeout, chat_tools_list, models):
    """Retry a chat request with reduced context and shorter timeout."""
    import subprocess
    from app.chat_context import build_chat_prompt, clean_chat_response
    from app.cli_exec import run_cli
    from app.cli_provider import build_full_command
    from app.conversation_history import save_conversation_message
    from app.notify import send_telegram

    retry_timeout = chat_timeout // 2
    lite_prompt = build_chat_prompt(
        text, lite=True,
        instance_dir=INSTANCE_DIR,
        koan_root=KOAN_ROOT,
        soul=soul,
        summary=summary,
        conversation_history_file=CONVERSATION_HISTORY_FILE,
        missions_file=MISSIONS_FILE,
        project_path=project_path,
    )
    lite_cmd = build_full_command(
        prompt=lite_prompt,
        allowed_tools=chat_tools_list,
        model=models["chat"],
        fallback=models["fallback"],
        max_turns=1,
    )
    try:
        result = run_cli(
            lite_cmd,
            capture_output=True, text=True, timeout=retry_timeout,
            cwd=project_path or str(KOAN_ROOT),
        )
        response = clean_chat_response(result.stdout.strip(), text)
        if response:
            send_telegram(response)
            msg_id = _get_last_message_id()
            save_conversation_message(
                CONVERSATION_HISTORY_FILE, "assistant", response,
                message_id=msg_id, message_type="chat",
            )
            _log(f"Chat reply (lite retry): {response[:80]}...")
        else:
            if result.stderr:
                _log(f"Lite retry stderr: {result.stderr[:500]}")
            timeout_msg = f"⏱ Timeout after {chat_timeout}s — try a shorter question, or send 'mission: ...' for complex tasks."
            send_telegram(timeout_msg)
            save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", timeout_msg)
    except subprocess.TimeoutExpired:
        timeout_msg = f"Timeout after {chat_timeout}s — try a shorter question, or send 'mission: ...' for complex tasks."
        send_telegram(timeout_msg)
        save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", timeout_msg)
    except Exception as e:
        _log(f"Lite retry error: {e}")
        error_msg = "⚠️ Something went wrong — try again?"
        send_telegram(error_msg)
        save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", error_msg)


def _get_last_message_id() -> int:
    """Get the message_id from the last send_telegram() call."""
    try:
        from app.messaging import get_messaging_provider
        provider = get_messaging_provider()
        ids = provider.get_last_message_ids()
        return ids[-1] if ids else 0
    except (SystemExit, Exception):
        return 0


def _log(msg: str) -> None:
    """Simple log output with timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [chat] {msg}", file=sys.stderr, flush=True)


def main():
    """Main loop: poll inbox, process requests, repeat."""
    from app.pid_manager import acquire_pidfile, release_pidfile

    signal.signal(signal.SIGTERM, _on_sigterm)

    # Enforce single instance
    pidfile_lock = acquire_pidfile(KOAN_ROOT, "chat")

    # Ensure PYTHONPATH includes the koan/ package directory
    koan_pkg_dir = str(KOAN_ROOT / "koan")
    current = os.environ.get("PYTHONPATH", "")
    if koan_pkg_dir not in current.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            f"{koan_pkg_dir}{os.pathsep}{current}" if current else koan_pkg_dir
        )

    _log("Chat process starting...")

    # Load context once at startup
    soul = _load_soul()
    summary = _load_summary()
    project_path = _resolve_project_path()

    _log(f"Soul: {len(soul)} chars loaded")
    _log(f"Polling inbox every {INBOX_POLL_INTERVAL}s")

    try:
        while not _shutdown_requested:
            entries = read_and_clear_inbox()
            for entry in entries:
                if _shutdown_requested:
                    break
                text = entry.get("text", "").strip()
                if text:
                    _log(f"Processing: {text[:60]}...")
                    try:
                        process_chat_request(text, soul, summary, project_path)
                    except Exception as e:
                        _log(f"Error processing chat: {e}")
                        try:
                            from app.notify import send_telegram
                            send_telegram("⚠️ Something went wrong — try again?")
                        except Exception:
                            pass

            time.sleep(INBOX_POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        release_pidfile(pidfile_lock, KOAN_ROOT, "chat")
        _log("Shutting down.")


if __name__ == "__main__":
    main()
