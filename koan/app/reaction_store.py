"""Reaction storage for Telegram message reactions.

Stores reactions as JSONL (one reaction per line) for lightweight,
append-only persistence. Supports loading recent reactions and
correlating them with conversation history messages.
"""

import fcntl
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


def save_reaction(
    reactions_file: Path,
    message_id: int,
    emoji: str,
    is_added: bool,
    original_text_preview: str = "",
    message_type: str = "",
):
    """Append a reaction to the reactions JSONL file.

    Args:
        reactions_file: Path to reactions.jsonl
        message_id: Telegram message_id of the reacted-to message
        emoji: The emoji string (e.g., "👍", "👎")
        is_added: True if reaction was added, False if removed
        original_text_preview: First ~100 chars of the original message
        message_type: Origin context of the message (chat, conclusion, notification)
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "message_id": message_id,
        "emoji": emoji,
        "action": "added" if is_added else "removed",
    }
    if original_text_preview:
        entry["original_text_preview"] = original_text_preview[:100]
    if message_type:
        entry["message_type"] = message_type

    try:
        with open(reactions_file, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"[reaction_store] Error saving reaction: {e}")


def load_recent_reactions(
    reactions_file: Path,
    max_reactions: int = 50,
) -> List[Dict]:
    """Load the most recent reactions from the JSONL file.

    Args:
        reactions_file: Path to reactions.jsonl
        max_reactions: Maximum number of reactions to return

    Returns:
        List of reaction dicts, most recent last
    """
    if not reactions_file.exists():
        return []

    try:
        with open(reactions_file, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                lines = f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return []

    reactions = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                reactions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return reactions[-max_reactions:] if len(reactions) > max_reactions else reactions


def lookup_message_context(
    history_file: Path,
    message_id: int,
) -> Optional[Dict]:
    """Find a message in conversation history by its message_id.

    Args:
        history_file: Path to conversation-history.jsonl
        message_id: Telegram message_id to look up

    Returns:
        Message dict if found, None otherwise
    """
    if not history_file.exists():
        return None

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                lines = f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("message_id") == message_id:
                return msg
        except json.JSONDecodeError:
            continue

    return None


def compact_reactions(reactions_file: Path, keep: int = 200):
    """Compact the reactions file to keep only the most recent entries.

    Args:
        reactions_file: Path to reactions.jsonl
        keep: Number of recent reactions to retain
    """
    if not reactions_file.exists():
        return

    reactions = load_recent_reactions(reactions_file, max_reactions=keep)
    if not reactions:
        return

    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(reactions_file.parent), prefix=".koan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for entry in reactions:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(reactions_file))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
