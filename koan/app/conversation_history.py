"""Conversation history management.

Handles saving, loading, formatting, and compacting conversation
history stored as JSONL files. Platform-agnostic — works with
any messaging provider.
"""

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List


def _atomic_write(path: Path, content: str):
    """Crash-safe file write using temp file + rename.

    Local wrapper to avoid circular import with utils.py (which re-exports
    from this module). Uses the same mkstemp + fsync + replace pattern.
    """
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".koan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_jsonl_lines(lines: list) -> List[Dict]:
    """Parse JSONL lines into a list of message dicts.

    Skips blank lines and lines with invalid JSON.
    """
    messages = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def save_conversation_message(
    history_file: Path,
    role: str,
    text: str,
    message_id: int = 0,
    message_type: str = "",
):
    """Save a message to the conversation history file (JSONL format).

    Args:
        history_file: Path to the history file (e.g., instance/conversation-history.jsonl)
        role: "user" or "assistant"
        text: Message content
        message_id: Telegram message_id for reaction correlation (0 = unknown)
        message_type: Origin context tag (e.g., "chat", "conclusion", "notification")
    """
    message = {
        "timestamp": datetime.now().isoformat(),
        "role": role,
        "text": text
    }
    if message_id:
        message["message_id"] = message_id
    if message_type:
        message["message_type"] = message_type
    try:
        with open(history_file, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"[conversation_history] Error saving message to history: {e}")


def load_recent_history(history_file: Path, max_messages: int = 10) -> List[Dict[str, str]]:
    """Load the most recent messages from conversation history.

    Args:
        history_file: Path to the history file
        max_messages: Maximum number of recent messages to return

    Returns:
        List of message dicts with keys: timestamp, role, text
    """
    if not history_file.exists():
        return []

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                lines = f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        messages = _parse_jsonl_lines(lines)
        return messages[-max_messages:] if len(messages) > max_messages else messages
    except OSError as e:
        print(f"[conversation_history] Error loading history: {e}")
        return []


def format_conversation_history(
    messages: List[Dict[str, str]],
    max_chars: int = 3000,
) -> str:
    """Format conversation history for inclusion in the prompt.

    Args:
        messages: List of message dicts from load_recent_history
        max_chars: Maximum total characters for the formatted history

    Returns:
        Formatted string ready to include in the prompt
    """
    if not messages:
        return ""

    lines = ["Recent conversation:"]
    total = len(lines[0])
    for msg in messages:
        role_label = "Human" if msg["role"] == "user" else "K\u014dan"
        text = msg["text"]
        if len(text) > 500:
            text = text[:500] + "..."
        line = f"{role_label}: {text}"
        total += len(line) + 1
        if total > max_chars:
            break
        lines.append(line)

    return "\n".join(lines)


def prune_topics(entries: list, max_entries: int = 20) -> list:
    """Keep only the most recent N compaction entries.

    Sorts by ``compacted_at`` timestamp (falling back to list order) and
    returns the last ``max_entries`` items.

    Args:
        entries: List of compaction entry dicts.
        max_entries: Maximum entries to retain.

    Returns:
        Pruned list (may be the same list if already within limit).
    """
    if not isinstance(entries, list) or len(entries) <= max_entries:
        return entries

    # Sort by compacted_at to ensure we keep the most recent
    try:
        entries.sort(key=lambda e: e.get("compacted_at", ""))
    except (TypeError, AttributeError):
        pass

    return entries[-max_entries:]


def compact_history(history_file: Path, topics_file: Path, min_messages: int = 20) -> int:
    """Compact conversation history at startup to avoid context bleed.

    Reads all messages from history_file, extracts discussion topics grouped
    by date, appends them to topics_file (JSON array), then truncates history.

    Args:
        history_file: Path to conversation-history.jsonl
        topics_file: Path to previous-discussions-topics.json
        min_messages: Minimum messages before compaction triggers (avoid compacting tiny histories)

    Returns:
        Number of messages compacted (0 if skipped)
    """
    if not history_file.exists():
        return 0

    # Read all messages
    messages = []
    try:
        with open(history_file, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                lines = f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return 0

    messages = _parse_jsonl_lines(lines)

    if len(messages) < min_messages:
        return 0

    # Group messages by date, extract topics from user messages
    topics_by_date: Dict[str, List[str]] = {}
    for msg in messages:
        ts = msg.get("timestamp", "")
        date = ts[:10] if len(ts) >= 10 else "unknown"
        if msg.get("role") == "user":
            text = msg.get("text", "").strip()
            # Take first sentence as topic hint (max 120 chars)
            topic = text.split(".")[0].split("?")[0].split("!")[0][:120].strip()
            if topic and len(topic) > 5:
                if date not in topics_by_date:
                    topics_by_date[date] = []
                if topic not in topics_by_date[date]:
                    topics_by_date[date].append(topic)

    if not topics_by_date:
        # No extractable topics, just purge atomically
        try:
            _atomic_write(history_file, "")
        except OSError:
            pass
        return len(messages)

    # Build compaction entry
    entry = {
        "compacted_at": datetime.now().isoformat(),
        "message_count": len(messages),
        "date_range": {
            "from": min(topics_by_date.keys()),
            "to": max(topics_by_date.keys()),
        },
        "topics_by_date": topics_by_date,
    }

    # Load existing topics file or create new
    existing = []
    if topics_file.exists():
        try:
            existing = json.loads(topics_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(entry)

    # Prune to keep only the most recent entries
    existing = prune_topics(existing)

    # Write topics file atomically
    try:
        _atomic_write(topics_file, json.dumps(existing, ensure_ascii=False, indent=2))
    except OSError:
        return 0

    # Truncate history atomically
    try:
        _atomic_write(history_file, "")
    except OSError:
        return 0

    count = len(messages)
    print(f"[conversation_history] Compacted {count} messages \u2192 {topics_file.name}")
    return count

