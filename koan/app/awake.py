#!/usr/bin/env python3
"""
Kōan Messaging Bridge — v2

Fast-response architecture:
- Polls messaging provider every 3s (configurable)
- Chat messages → lightweight Claude call → instant reply
- Mission-like messages → written to missions.md → ack sent immediately
- Outbox flushed every cycle (no more waiting for next poll)
- /stop, /status handled locally (no Claude needed)

Module layout:
- bridge_state.py — shared constants (KOAN_ROOT, INSTANCE_DIR, etc.)
- command_handlers.py — /command dispatch and handler functions
- awake.py (this file) — main loop, chat, outbox, message classification
"""

import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from typing import Optional, Tuple

from app.bridge_log import log
from app.bridge_state import (
    BOT_TOKEN,
    CHAT_ID,
    CHAT_TIMEOUT,
    INSTANCE_DIR,
    KOAN_ROOT,
    MISSIONS_FILE,
    OUTBOX_FILE,
    POLL_INTERVAL,
    PROJECT_PATH,
    SOUL,
    SUMMARY,
    CONVERSATION_HISTORY_FILE,
    TOPICS_FILE,
    _get_registry,
)
from app.cli_provider import build_full_command
from app.command_handlers import (
    handle_command,
    handle_mission,
    set_callbacks,
)
from app.health_check import write_heartbeat
from app.language_preference import get_language_instruction
from app.notify import TypingIndicator, reset_flood_state, send_telegram
from app.outbox_manager import OutboxManager, parse_outbox_priority
from app.shutdown_manager import is_shutdown_requested, clear_shutdown
from app.config import (
    get_chat_tools,
    get_tools_description,
    get_model_config,
)
from app.conversation_history import (
    save_conversation_message,
    load_recent_history,
    format_conversation_history,
    compact_history,
)
from app.signals import HEARTBEAT_FILE, PAUSE_FILE, STOP_FILE
from app.utils import (
    parse_project as _parse_project,
)


# ---------------------------------------------------------------------------
# Outbox manager — singleton instance, created at module load
# ---------------------------------------------------------------------------

_outbox_mgr = OutboxManager(OUTBOX_FILE, INSTANCE_DIR, CONVERSATION_HISTORY_FILE)


def _get_last_message_id() -> int:
    """Get the message_id from the last send_telegram() call."""
    return OutboxManager._get_last_message_id()


def check_config():
    if not BOT_TOKEN or not CHAT_ID:
        log("error", "Set KOAN_TELEGRAM_TOKEN and KOAN_TELEGRAM_CHAT_ID env vars.")
        sys.exit(1)
    if not INSTANCE_DIR.exists():
        log("error", "No instance/ directory. Run: cp -r instance.example instance")
        sys.exit(1)


def get_updates(offset=None):
    """Fetch new updates from the messaging provider.

    Returns a list of raw-dict-compatible updates for backward compatibility
    with the existing message processing pipeline.
    """
    from app.messaging import get_messaging_provider
    provider = get_messaging_provider()
    updates = provider.poll_updates(offset)
    # Convert Update objects to raw dicts for backward compat with main loop
    return [u.raw_data for u in updates]


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------

# Patterns that indicate a mission (imperative, actionable request)
MISSION_PATTERNS = [
    r"^(implement|create|add|fix|audit|review|analyze|explore|build|write|run|deploy|test|refactor)\b",
    r"^mission\s*:",
]
MISSION_RE = re.compile("|".join(MISSION_PATTERNS), re.IGNORECASE)


def is_mission(text: str) -> bool:
    """Heuristic: does this message look like a mission assignment?"""
    # Explicit prefix always wins
    if text.lower().startswith("mission:") or text.lower().startswith("mission :"):
        return True
    # Long messages (>200 chars) that start with imperative verbs are likely missions
    if len(text) > 200 and MISSION_RE.match(text):
        return True
    # Short imperative sentences
    if MISSION_RE.match(text):
        return True
    return False


def is_command(text: str) -> bool:
    return text.startswith("/")


def parse_project(text: str) -> Tuple[Optional[str], str]:
    """Extract [project:name] or [projet:name] from message."""
    return _parse_project(text)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def _build_chat_prompt(text: str, *, lite: bool = False) -> str:
    """Build the prompt for a chat response.

    Args:
        text: The user's message.
        lite: If True, strip heavy context (journal, summary) to stay under budget.
    """
    # Load recent conversation history
    history = load_recent_history(CONVERSATION_HISTORY_FILE, max_messages=10)
    history_context = format_conversation_history(history)

    journal_context = ""
    if not lite:
        # Load today's journal for recent context
        from app.journal import read_all_journals
        journal_content = read_all_journals(INSTANCE_DIR, date.today())
        if journal_content:
            if len(journal_content) > 2000:
                journal_context = "...\n" + journal_content[-2000:]
            else:
                journal_context = journal_content

    # Load human preferences for personality context
    prefs_context = ""
    prefs_path = INSTANCE_DIR / "memory" / "global" / "human-preferences.md"
    if prefs_path.exists():
        prefs_context = prefs_path.read_text().strip()

    # Load live progress from pending.md (run in progress)
    pending_context = ""
    pending_path = INSTANCE_DIR / "journal" / "pending.md"
    if pending_path.exists():
        try:
            pending_content = pending_path.read_text()
            # Take last 1500 chars for recent progress
            if len(pending_content) > 1500:
                pending_context = "Live progress (pending.md, last entries):\n...\n" + pending_content[-1500:]
            else:
                pending_context = "Live progress (pending.md):\n" + pending_content
        except OSError:
            pass

    # Load current mission state (live sync with run loop)
    missions_context = ""
    if pending_context:
        missions_context = pending_context
    elif MISSIONS_FILE.exists():
        from app.missions import parse_sections
        try:
            sections = parse_sections(MISSIONS_FILE.read_text())
        except OSError:
            sections = {}
        in_progress = sections.get("in_progress", [])
        pending = sections.get("pending", [])
        if in_progress or pending:
            parts = []
            if in_progress:
                parts.append("In progress: " + "; ".join(in_progress[:3]))
            if pending:
                parts.append(f"Pending: {len(pending)} mission(s)")
            missions_context = "\n".join(parts)

    # Run loop status (CRITICAL for pause awareness)
    run_loop_status = ""
    pause_file = KOAN_ROOT / PAUSE_FILE
    stop_file = KOAN_ROOT / STOP_FILE
    if pause_file.exists():
        run_loop_status = "\n\nRun loop status: ⏸️ PAUSED — Missions are NOT being executed"
    elif stop_file.exists():
        run_loop_status = "\n\nRun loop status: ⛔ STOP REQUESTED — Finishing current work"
    else:
        run_loop_status = "\n\nRun loop status: ▶️ RUNNING"

    # Append run loop status to missions context
    if missions_context:
        missions_context += run_loop_status
    else:
        missions_context = f"No pending missions.{run_loop_status}"

    # Determine time-of-day for natural tone
    hour = datetime.now().hour
    if hour < 7:
        time_hint = "It's very early morning."
    elif hour < 12:
        time_hint = "It's morning."
    elif hour < 18:
        time_hint = "It's afternoon."
    elif hour < 22:
        time_hint = "It's evening."
    else:
        time_hint = "It's late night."

    # Load tools description
    tools_desc = get_tools_description()

    from app.prompts import load_prompt

    summary_budget = 0 if lite else 1500
    summary_block = f"Summary of past sessions:\n{SUMMARY[:summary_budget]}" if SUMMARY and summary_budget else ""
    prefs_block = f"About the human:\n{prefs_context}" if prefs_context else ""
    journal_block = f"Today's journal (excerpt):\n{journal_context}" if journal_context else ""
    missions_block = f"Current missions state:\n{missions_context}" if missions_context else ""

    # Load emotional memory for relationship-aware responses
    emotional_context = ""
    if not lite:
        emotional_path = INSTANCE_DIR / "memory" / "global" / "emotional-memory.md"
        if emotional_path.exists():
            content = emotional_path.read_text().strip()
            # Take last 800 chars — enough for tone, not too heavy
            if len(content) > 800:
                emotional_context = "...\n" + content[-800:]
            else:
                emotional_context = content

    prompt = load_prompt(
        "chat",
        SOUL=SOUL,
        TOOLS_DESC=tools_desc or "",
        PREFS=prefs_block,
        SUMMARY=summary_block,
        JOURNAL=journal_block,
        MISSIONS=missions_block,
        HISTORY=history_context or "",
        TIME_HINT=time_hint,
        TEXT=text,
    )

    # Inject language preference override
    lang_instruction = get_language_instruction()
    if lang_instruction:
        prompt += f"\n\n{lang_instruction}"

    # Inject emotional memory before the user message (if available)
    if emotional_context:
        prompt = prompt.replace(
            f"« {text} »",
            f"Emotional memory (relationship context, use to color your tone):\n{emotional_context}\n\nThe human sends you this message on Telegram:\n\n  « {text} »",
        )

    # Hard cap: if prompt exceeds 12k chars, force lite mode
    MAX_PROMPT_CHARS = 12000
    if len(prompt) > MAX_PROMPT_CHARS and not lite:
        return _build_chat_prompt(text, lite=True)

    # Last resort: if lite mode still exceeds the cap, truncate user message
    if len(prompt) > MAX_PROMPT_CHARS:
        overflow = len(prompt) - MAX_PROMPT_CHARS
        max_text_len = max(200, len(text) - overflow - 50)  # 50 chars margin for ellipsis/safety
        if len(text) > max_text_len:
            truncated_text = text[:max_text_len] + "… [truncated]"
            prompt = prompt.replace(text, truncated_text)

    return prompt


def _clean_chat_response(text: str, user_message: str = "") -> str:
    """Clean Claude CLI output for Telegram delivery.

    Strips error artifacts, markdown, truncates for smartphone reading,
    and expands bare #123 GitHub refs to clickable URLs.
    """
    from app.text_utils import clean_cli_response, expand_github_refs_auto

    cleaned = clean_cli_response(text)
    return expand_github_refs_auto(cleaned, user_message)


def handle_chat(text: str):
    """Lightweight Claude call for conversational messages — fast response.

    Uses restricted tools (Read/Glob/Grep by default) to prevent prompt
    injection attacks via Telegram messages. No Bash, Edit, or Write access.
    """
    from app.cli_exec import run_cli

    # Save user message to history
    save_conversation_message(CONVERSATION_HISTORY_FILE, "user", text)

    # Scan for prompt injection — warn-only (never block chat; tools are read-only)
    from app.prompt_guard import scan_mission_text
    from app.config import get_prompt_guard_config
    from app.command_handlers import quarantine_mission

    guard_config = get_prompt_guard_config()
    if guard_config["enabled"]:
        guard_result = scan_mission_text(text)
        if guard_result.blocked:
            log("guard", f"WARNING chat: {guard_result.reason} | {text[:100]}")
            quarantine_mission(text, guard_result.reason, source="telegram-chat")

    prompt = _build_chat_prompt(text)
    chat_tools_list = get_chat_tools().split(",")
    models = get_model_config()

    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=chat_tools_list,
        model=models["chat"],
        fallback=models["fallback"],
        max_turns=1,
    )

    with TypingIndicator():
        try:
            result = run_cli(
                cmd,
                capture_output=True, text=True, timeout=CHAT_TIMEOUT,
                cwd=PROJECT_PATH or str(KOAN_ROOT),
            )
            response = _clean_chat_response(result.stdout.strip(), text)
            if response:
                send_telegram(response)
                msg_id = _get_last_message_id()
                save_conversation_message(
                    CONVERSATION_HISTORY_FILE, "assistant", response,
                    message_id=msg_id, message_type="chat",
                )
                log("chat", f"Chat reply: {response[:80]}...")
            elif result.returncode != 0:
                log("error", f"Claude error: {result.stderr[:200]}")
                error_msg = "⚠️ Hmm, I couldn't formulate a response. Try again?"
                send_telegram(error_msg)
                save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", error_msg)
            else:
                log("chat", "Empty response from Claude.")
                empty_msg = "⚠️ I didn't get a response — please try again."
                send_telegram(empty_msg)
                save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", empty_msg)
        except subprocess.TimeoutExpired:
            log("error", f"Claude timed out ({CHAT_TIMEOUT}s). Retrying with lite context...")
            # Brief backoff before retry to let API pressure ease
            time.sleep(4)
            # Retry with reduced context and shorter timeout
            retry_timeout = CHAT_TIMEOUT // 2
            lite_prompt = _build_chat_prompt(text, lite=True)
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
                    cwd=PROJECT_PATH or str(KOAN_ROOT),
                )
                response = _clean_chat_response(result.stdout.strip(), text)
                if response:
                    send_telegram(response)
                    msg_id = _get_last_message_id()
                    save_conversation_message(
                        CONVERSATION_HISTORY_FILE, "assistant", response,
                        message_id=msg_id, message_type="chat",
                    )
                    log("chat", f"Chat reply (lite retry): {response[:80]}...")
                else:
                    if result.stderr:
                        log("error", f"Lite retry stderr: {result.stderr[:500]}")
                    timeout_msg = f"⏱ Timeout after {CHAT_TIMEOUT}s — try a shorter question, or send 'mission: ...' for complex tasks."
                    send_telegram(timeout_msg)
                    save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", timeout_msg)
            except subprocess.TimeoutExpired:
                timeout_msg = f"Timeout after {CHAT_TIMEOUT}s — try a shorter question, or send 'mission: ...' for complex tasks."
                send_telegram(timeout_msg)
                save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", timeout_msg)
            except Exception as e:
                log("error", f"Lite retry error: {e}")
                error_msg = "⚠️ Something went wrong — try again?"
                send_telegram(error_msg)
                save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", error_msg)
        except Exception as e:
            log("error", f"Claude error: {e}")
            error_msg = "⚠️ Something went wrong — try again?"
            send_telegram(error_msg)
            save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", error_msg)


# ---------------------------------------------------------------------------
# Outbox — delegated to OutboxManager (backward-compatible wrappers)
#
# These wrappers create a fresh OutboxManager from the current module-level
# values (OUTBOX_FILE, INSTANCE_DIR, etc.) so that test patches on those
# names propagate correctly.  In production, the main loop uses
# _outbox_mgr.flush_async() which goes through the singleton directly.
# ---------------------------------------------------------------------------


def _make_outbox_mgr() -> OutboxManager:
    """Create an OutboxManager from the current (possibly patched) module values."""
    return OutboxManager(OUTBOX_FILE, INSTANCE_DIR, CONVERSATION_HISTORY_FILE)


def _staging_path():
    """Return path of the outbox staging file (crash-recovery backup)."""
    return _make_outbox_mgr().staging_path


# Keep _parse_outbox_priority importable from awake for backward compat
_parse_outbox_priority = parse_outbox_priority


def _recover_staged_outbox():
    """Recover content from a staging file left by a previous crash."""
    _make_outbox_mgr().recover_staged()


def flush_outbox():
    """Relay messages from the run loop outbox."""
    _make_outbox_mgr().flush()


def _requeue_outbox(content: str):
    """Re-append content to outbox.md after a failed send attempt."""
    _make_outbox_mgr().requeue(content)


def _write_outbox_failed(content: str, original_error: Exception):
    """Last-resort persistence: write lost outbox content to outbox-failed.md."""
    _make_outbox_mgr()._write_failed(content, original_error)


def _expand_outbox_github_refs(formatted: str, raw_content: str) -> str:
    """Expand bare #123 GitHub refs in an outbox message to full URLs."""
    return OutboxManager._expand_github_refs(formatted, raw_content)


def _format_outbox_message(raw_content: str) -> str:
    """Format outbox content via Claude with full personality context."""
    return _make_outbox_mgr()._format_message(raw_content)


# ---------------------------------------------------------------------------
# Worker thread — runs handle_chat in background so polling stays responsive
# ---------------------------------------------------------------------------

_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _run_in_worker(fn, *args):
    """Run fn(*args) in a background thread. One worker at a time."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            send_telegram("⏳ Busy with a previous message. Try again in a moment.")
            return
        _worker_thread = threading.Thread(target=fn, args=args, daemon=True)
        _worker_thread.start()


# ---------------------------------------------------------------------------
# Outbox flush thread — delegated to OutboxManager
# ---------------------------------------------------------------------------


def _flush_outbox_async():
    """Run flush_outbox() in a background thread if not already running."""
    _outbox_mgr.flush_async()


# Inject callbacks into command_handlers to break circular dependency
set_callbacks(handle_chat=handle_chat, run_in_worker=_run_in_worker)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

REACTIONS_FILE = INSTANCE_DIR / "reactions.jsonl"


def _handle_reaction_update(update: dict):
    """Process a message_reaction update from Telegram.

    Looks up the reacted-to message in conversation history to enrich
    the reaction with context, then stores it in reactions.jsonl.
    """
    from app.reaction_store import save_reaction, lookup_message_context

    reaction_data = update.get("message_reaction", {})
    chat_id = str(reaction_data.get("chat", {}).get("id", ""))
    if chat_id != CHAT_ID:
        return

    message_id = reaction_data.get("message_id", 0)
    if not message_id:
        return

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

    # Look up original message context
    context = lookup_message_context(CONVERSATION_HISTORY_FILE, message_id)
    text_preview = ""
    msg_type = ""
    if context:
        text_preview = context.get("text", "")[:100]
        msg_type = context.get("message_type", "")

    for emoji in added:
        save_reaction(
            REACTIONS_FILE, message_id, emoji,
            is_added=True,
            original_text_preview=text_preview,
            message_type=msg_type,
        )
        log("reaction", f"Reaction {emoji} added on message {message_id}")

    for emoji in removed:
        save_reaction(
            REACTIONS_FILE, message_id, emoji,
            is_added=False,
            original_text_preview=text_preview,
            message_type=msg_type,
        )
        log("reaction", f"Reaction {emoji} removed from message {message_id}")


def handle_message(text: str):
    text = text.strip()
    if not text:
        return

    # Each incoming user message resets flood protection so identical
    # command responses (e.g. /help twice) are never suppressed.
    reset_flood_state()

    if is_command(text):
        handle_command(text)
    elif is_mission(text):
        handle_mission(text)
    else:
        _run_in_worker(handle_chat, text)


def _ensure_runner_alive() -> None:
    """Start the runner if it's not running.

    Called after a /restart re-exec so the bridge can bring the runner
    back when the runner wasn't alive to detect the restart signal itself.
    """
    from app.pid_manager import check_pidfile, start_runner

    if check_pidfile(KOAN_ROOT, "run"):
        return  # Already running — it will restart itself via exit code 42

    log("init", "Runner not running — starting it as part of restart")
    ok, msg = start_runner(KOAN_ROOT)
    if ok:
        log("init", f"Runner started: {msg}")
    else:
        log("error", f"Failed to start runner: {msg}")


def main():
    from app.banners import print_bridge_banner
    from app.github_auth import setup_github_auth
    from app.pid_manager import acquire_pidfile, release_pidfile
    from app.restart_manager import check_restart, clear_restart, reexec_bridge

    check_config()

    # Ensure PYTHONPATH includes the koan/ package directory so that
    # subprocess calls (e.g. local LLM runner via python -m app.local_llm_runner)
    # can resolve app.* modules regardless of the subprocess CWD.
    koan_pkg_dir = str(KOAN_ROOT / "koan")
    current = os.environ.get("PYTHONPATH", "")
    if koan_pkg_dir not in current.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            f"{koan_pkg_dir}{os.pathsep}{current}" if current else koan_pkg_dir
        )

    # Run pending data migrations (e.g. French→English header conversion)
    from app.migration_runner import run_pending_migrations
    applied = run_pending_migrations()
    if applied:
        log("init", f"Applied {len(applied)} migration(s)")

    # Enforce single instance — abort if another awake process is running
    pidfile_lock = acquire_pidfile(KOAN_ROOT, "awake")

    setup_github_auth()

    provider_name = "telegram"  # about to become dynamic with provider abstraction
    print_bridge_banner(f"messaging bridge — {provider_name.lower()}")

    # Record startup time — used to ignore stale signal files in the
    # main loop (only react to files created after we started).
    startup_time = time.time()

    # Compact old conversation history to avoid context bleed across sessions
    compacted = compact_history(CONVERSATION_HISTORY_FILE, TOPICS_FILE)
    if compacted:
        log("health", f"Compacted {compacted} old messages at startup")

    # Purge stale heartbeat so health_check doesn't report STALE on restart
    heartbeat_file = KOAN_ROOT / HEARTBEAT_FILE
    heartbeat_file.unlink(missing_ok=True)
    write_heartbeat(str(KOAN_ROOT))
    log("init", f"Token: ...{BOT_TOKEN[-8:]}")
    log("init", f"Chat ID: {CHAT_ID}")
    log("init", f"Soul: {len(SOUL)} chars loaded")
    log("init", f"Summary: {len(SUMMARY)} chars loaded")
    registry = _get_registry()
    core_count = len(registry.list_by_scope("core"))
    extra_count = len(registry) - core_count
    skills_info = f"{core_count} core"
    if extra_count:
        skills_info += f" + {extra_count} extra"
    log("init", f"Skills: {skills_info}")

    # Initialize messaging provider and log startup banner
    from app.messaging import get_messaging_provider
    try:
        provider = get_messaging_provider()
        provider_name = provider.get_provider_name().upper()
        channel_id = provider.get_channel_id()
        log("init", f"Messaging provider: {provider_name}, Channel: {channel_id}")
    except SystemExit:
        log("error", "Failed to initialize messaging provider")
        sys.exit(1)

    log("init", f"Polling every {POLL_INTERVAL}s (chat mode: fast reply)")
    offset = None
    first_poll = True

    try:
        while True:
            try:
                updates = get_updates(offset)
            except StopIteration:
                raise
            except Exception as e:
                log("error", f"get_updates failed: {e}")
                time.sleep(POLL_INTERVAL)
                continue

            for update in updates:
                offset = update["update_id"] + 1

                # Handle reaction updates
                if "message_reaction" in update:
                    try:
                        _handle_reaction_update(update)
                    except Exception as e:
                        log("error", f"Reaction handling failed: {e}")
                    continue

                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id == CHAT_ID and text:
                    log("chat", f"Received: {text[:60]}")
                    try:
                        handle_message(text)
                    except Exception as e:
                        log("error", f"Message handling failed: {e}")
                        try:
                            send_telegram(f"⚠️ Error processing message: {type(e).__name__}: {e}")
                        except Exception as notify_err:
                            print(f"[bridge] error notification also failed: {notify_err}", file=sys.stderr)

            # After the first poll cycle, clear any stale signal files
            # left from a previous incarnation.  During the first poll
            # these files act as dedup guards: if Telegram re-delivers
            # the /restart or /shutdown message that triggered our exit,
            # the skill handler re-creates the file — but we clear it
            # right after so the check below finds nothing.
            if first_poll:
                # Check if we're coming back from a /restart before clearing
                was_restart = check_restart(str(KOAN_ROOT))
                clear_restart(str(KOAN_ROOT))
                clear_shutdown(str(KOAN_ROOT))
                first_poll = False

                # If this is a restart-triggered re-exec and the runner
                # is dead, start it.  The runner can't self-restart if
                # it wasn't running when the signal was created.
                if was_restart:
                    _ensure_runner_alive()

            try:
                _flush_outbox_async()
            except Exception as e:
                log("error", f"flush_outbox failed: {e}")

            try:
                write_heartbeat(str(KOAN_ROOT))
            except Exception as e:
                log("error", f"write_heartbeat failed: {e}")

            # Check for restart signal (set by /restart command).
            # Only react to files created AFTER we started — stale files
            # were already cleared above after the first poll.
            if check_restart(str(KOAN_ROOT), since=startup_time):
                log("init", "Restart signal detected. Re-executing...")
                release_pidfile(pidfile_lock, KOAN_ROOT, "awake")
                reexec_bridge()

            # Check for /shutdown signal (timestamp-validated)
            if is_shutdown_requested(str(KOAN_ROOT), startup_time):
                log("init", "Shutdown requested. Exiting.")
                clear_shutdown(str(KOAN_ROOT))
                release_pidfile(pidfile_lock, KOAN_ROOT, "awake")
                sys.exit(0)

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        release_pidfile(pidfile_lock, KOAN_ROOT, "awake")
        log("init", "Shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
