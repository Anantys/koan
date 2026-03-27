#!/usr/bin/env python3
"""
Format outbox messages via Claude for messaging delivery.

ALL outbox messages must be Claude-formatted to ensure they are:
- Conversational and human (not technical dumps)
- In the human's preferred language
- Plain text only (NO markdown, NO code blocks)
- Concise (2-4 sentences unless context requires more)
- Natural tone matching Kōan's personality from soul.md

Usage: python format_outbox.py <instance_dir> [project_name] < raw_message

Reads raw content from stdin, formats it via Claude, outputs to stdout.
"""

import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app.cli_provider import build_full_command
from app.language_preference import get_language_instruction
from app.config import get_model_config
from app.response_cache import get_format_cache


def load_soul(instance_dir: Path) -> str:
    """Load Kōan's identity from soul.md.

    Args:
        instance_dir: Path to instance directory

    Returns:
        Soul content or empty string if not found
    """
    soul_file = instance_dir / "soul.md"
    if soul_file.exists():
        return soul_file.read_text()
    return ""


def load_human_prefs(instance_dir: Path) -> str:
    """Load human preferences for context.

    Args:
        instance_dir: Path to instance directory

    Returns:
        Preferences content or empty string if not found
    """
    prefs_file = instance_dir / "memory" / "global" / "human-preferences.md"
    if prefs_file.exists():
        return prefs_file.read_text()
    return ""


def load_memory_context(instance_dir: Path, project_name: str = "") -> str:
    """Load recent memory context (summary + learnings) for richer formatting.

    Args:
        instance_dir: Path to instance directory
        project_name: Optional project name for scoped learnings

    Returns:
        Memory context string or empty string
    """
    parts = []

    # Personality evolution (acquired traits)
    personality_file = instance_dir / "memory" / "global" / "personality-evolution.md"
    if personality_file.exists():
        content = personality_file.read_text().strip()
        lines = content.splitlines()
        recent = [l for l in lines if l.strip() and not l.startswith("#")][-5:]
        if recent:
            parts.append("Acquired personality traits:\n" + "\n".join(recent))

    # Emotional memory (relationship context)
    emotional_file = instance_dir / "memory" / "global" / "emotional-memory.md"
    if emotional_file.exists():
        content = emotional_file.read_text().strip()
        lines = content.splitlines()
        # Take key sections — skip headers, keep substance
        recent = [l for l in lines if l.strip() and not l.startswith("#")][-15:]
        if recent:
            parts.append("Emotional memory (relationship context):\n" + "\n".join(recent))

    # Recent summary (last 5 lines)
    summary_file = instance_dir / "memory" / "summary.md"
    if summary_file.exists():
        lines = summary_file.read_text().strip().splitlines()
        recent = [l for l in lines if l.strip()][-5:]
        if recent:
            parts.append("Recent sessions:\n" + "\n".join(recent))

    # Project-specific learnings (last 10 lines)
    if project_name:
        learnings_file = instance_dir / "memory" / "projects" / project_name / "learnings.md"
        if learnings_file.exists():
            content = learnings_file.read_text().strip()
            lines = content.splitlines()
            recent = [l for l in lines if l.strip()][-10:]
            if recent:
                parts.append("Project learnings:\n" + "\n".join(recent))

    return "\n\n".join(parts)


def _get_time_hint() -> str:
    """Return a time-of-day hint for tone adaptation."""
    hour = datetime.now().hour
    if hour < 7:
        return "It's very early morning."
    elif hour < 12:
        return "It's morning."
    elif hour < 18:
        return "It's afternoon."
    elif hour < 22:
        return "It's evening."
    else:
        return "It's late night."


def format_message(raw_content: str, soul: str, prefs: str,
                   memory_context: str = "") -> str:
    """Format raw content via Claude for messaging delivery.

    Args:
        raw_content: Raw message text (journal dump, retrospective, etc.)
        soul: Kōan's identity from soul.md
        prefs: Human preferences context
        memory_context: Recent memory (summary + learnings) for richer context

    Returns:
        Formatted message (plain text, conversational)
    """
    from app.prompts import load_prompt

    prefs_block = f"Human preferences: {prefs}" if prefs else ""
    memory_block = f"Recent memory context:\n{memory_context}" if memory_context else ""
    time_hint = _get_time_hint()

    # Inject language preference override
    lang_instruction = get_language_instruction()

    # Check cache before invoking Claude CLI
    cache = get_format_cache()
    key_material = "\n".join([raw_content, soul, prefs, memory_context,
                              time_hint, lang_instruction or ""])
    cache_key = hashlib.sha256(key_material.encode()).hexdigest()

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    prompt = load_prompt(
        "format-message",
        SOUL=soul,
        PREFS=prefs_block,
        MEMORY=memory_block,
        TIME_HINT=time_hint,
        RAW_CONTENT=raw_content,
    )

    if lang_instruction:
        prompt += f"\n\n{lang_instruction}"

    # Get KOAN_ROOT for proper working directory
    import os
    koan_root = os.environ.get("KOAN_ROOT") or None
    if koan_root is None:
        print("[format_outbox] KOAN_ROOT not set, using current directory", file=sys.stderr)

    try:
        from app.cli_exec import run_cli

        models = get_model_config()
        cmd = build_full_command(prompt=prompt, model=models["lightweight"])
        result = run_cli(
            cmd,
            cwd=koan_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False
        )

        if result.returncode == 0 and result.stdout.strip():
            from app.text_utils import strip_markdown

            formatted = result.stdout.strip()

            # Safety check: remove any remaining markdown artifacts
            formatted = strip_markdown(formatted)

            # Cache successful result (15 min TTL)
            cache.put(cache_key, formatted, ttl=900)

            return formatted
        else:
            # Fallback: if Claude fails, return truncated raw content
            # Don't cache fallback results
            print(f"[format_outbox] Claude formatting failed: {result.stderr[:200]}", file=sys.stderr)
            return fallback_format(raw_content)

    except subprocess.TimeoutExpired:
        print("[format_outbox] Claude timeout (30s) - using fallback", file=sys.stderr)
        return fallback_format(raw_content)
    except Exception as e:
        print(f"[format_outbox] Error: {e} - using fallback", file=sys.stderr)
        return fallback_format(raw_content)


def fallback_format(raw_content: str) -> str:
    """Fallback formatting when Claude is unavailable.

    Args:
        raw_content: Raw message text

    Returns:
        Minimally cleaned message
    """
    from app.text_utils import strip_markdown, DEFAULT_MAX_LENGTH

    cleaned = strip_markdown(raw_content)
    # Strip list markers at line start
    cleaned = re.sub(r'^[\-\*>]\s+', '', cleaned, flags=re.MULTILINE)
    # Truncate for smartphone readability
    if len(cleaned) > DEFAULT_MAX_LENGTH:
        cleaned = cleaned[:DEFAULT_MAX_LENGTH - 3] + "..."
    return cleaned.strip()


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: format_outbox.py <instance_dir> [project_name]", file=sys.stderr)
        print("Reads raw message from stdin, outputs formatted message to stdout", file=sys.stderr)
        sys.exit(1)

    instance_dir = Path(sys.argv[1])
    project_name = sys.argv[2] if len(sys.argv) > 2 else ""

    if not instance_dir.exists():
        print(f"[format_outbox] Instance directory not found: {instance_dir}", file=sys.stderr)
        sys.exit(1)

    # Read raw message from stdin
    raw_message = sys.stdin.read()
    if not raw_message.strip():
        print("[format_outbox] No input received", file=sys.stderr)
        sys.exit(1)

    # Load context
    soul = load_soul(instance_dir)
    prefs = load_human_prefs(instance_dir)
    memory = load_memory_context(instance_dir, project_name)

    # Format via Claude
    formatted = format_message(raw_message, soul, prefs, memory)

    # Output to stdout (will be captured by run.py and appended to outbox)
    print(formatted)


if __name__ == "__main__":
    main()
