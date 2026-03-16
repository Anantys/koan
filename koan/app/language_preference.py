#!/usr/bin/env python3
"""
Language preference management for Kōan.

Stores and retrieves the user's preferred reply language.
When set, all Claude-mediated replies (chat, outbox) will be in that language.
When reset, Kōan replies in the same language as the input.

Storage: instance/language.json
"""

import json
import os
from pathlib import Path

from app.utils import atomic_write


def _get_language_file() -> Path:
    """Return path to the language preference file."""
    koan_root = Path(os.environ.get("KOAN_ROOT", "."))
    return koan_root / "instance" / "language.json"


def get_language() -> str:
    """Get the current language preference.

    Returns:
        Language name (e.g. "english", "french") or empty string if not set.
    """
    lang_file = _get_language_file()
    if not lang_file.exists():
        return ""
    try:
        data = json.loads(lang_file.read_text())
        return data.get("language", "")
    except (json.JSONDecodeError, OSError):
        return ""


def set_language(language: str) -> None:
    """Set the language preference.

    Args:
        language: Language name (e.g. "english", "french", "spanish").
    """
    lang_file = _get_language_file()
    lang_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(lang_file, json.dumps({"language": language.strip().lower()}))


def reset_language() -> None:
    """Reset the language preference (reply in same language as input)."""
    lang_file = _get_language_file()
    if lang_file.exists():
        lang_file.unlink()


def get_language_instruction() -> str:
    """Get a prompt instruction for language enforcement.

    Returns:
        Instruction string to inject into prompts, or empty string if no override.
    """
    lang = get_language()
    if not lang:
        return ""
    return f"IMPORTANT: You MUST reply in {lang}. This is a user-configured language preference. All your responses must be written in {lang}, regardless of the input language."
