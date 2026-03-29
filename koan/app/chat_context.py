"""Chat context building — shared between awake.py and chat_process.py.

Extracted from awake.py to allow the dedicated chat process to build
the same prompts without importing the full bridge module.
"""

import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional


def build_chat_prompt(
    text: str,
    *,
    lite: bool = False,
    instance_dir: Path,
    koan_root: Path,
    soul: str,
    summary: str,
    conversation_history_file: Path,
    missions_file: Path,
    project_path: Optional[str] = None,
) -> str:
    """Build the prompt for a chat response.

    Args:
        text: The user's message.
        lite: If True, strip heavy context (journal, summary) to stay under budget.
        instance_dir: Path to the instance directory.
        koan_root: Path to KOAN_ROOT.
        soul: Soul content string.
        summary: Summary content string.
        conversation_history_file: Path to conversation-history.jsonl.
        missions_file: Path to missions.md.
        project_path: Optional project working directory.
    """
    from app.conversation_history import load_recent_history, format_conversation_history
    from app.language_preference import get_language_instruction
    from app.config import get_chat_tools, get_tools_description
    from app.signals import PAUSE_FILE, STOP_FILE

    # Load recent conversation history
    history = load_recent_history(conversation_history_file, max_messages=10)
    history_context = format_conversation_history(history)

    journal_context = ""
    if not lite:
        from app.journal import read_all_journals
        journal_content = read_all_journals(instance_dir, date.today())
        if journal_content:
            if len(journal_content) > 2000:
                journal_context = "...\n" + journal_content[-2000:]
            else:
                journal_context = journal_content

    # Load human preferences for personality context
    prefs_context = ""
    prefs_path = instance_dir / "memory" / "global" / "human-preferences.md"
    if prefs_path.exists():
        prefs_context = prefs_path.read_text().strip()

    # Load live progress from pending.md (run in progress)
    pending_context = ""
    pending_path = instance_dir / "journal" / "pending.md"
    if pending_path.exists():
        try:
            pending_content = pending_path.read_text()
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
    elif missions_file.exists():
        from app.missions import parse_sections
        try:
            sections = parse_sections(missions_file.read_text())
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
    pause_file = koan_root / PAUSE_FILE
    stop_file = koan_root / STOP_FILE
    if pause_file.exists():
        run_loop_status = "\n\nRun loop status: ⏸️ PAUSED — Missions are NOT being executed"
    elif stop_file.exists():
        run_loop_status = "\n\nRun loop status: ⛔ STOP REQUESTED — Finishing current work"
    else:
        run_loop_status = "\n\nRun loop status: ▶️ RUNNING"

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

    tools_desc = get_tools_description()

    from app.prompts import load_prompt

    summary_budget = 0 if lite else 1500
    summary_block = f"Summary of past sessions:\n{summary[:summary_budget]}" if summary and summary_budget else ""
    prefs_block = f"About the human:\n{prefs_context}" if prefs_context else ""
    journal_block = f"Today's journal (excerpt):\n{journal_context}" if journal_context else ""
    missions_block = f"Current missions state:\n{missions_context}" if missions_context else ""

    # Load emotional memory for relationship-aware responses
    emotional_context = ""
    if not lite:
        emotional_path = instance_dir / "memory" / "global" / "emotional-memory.md"
        if emotional_path.exists():
            content = emotional_path.read_text().strip()
            if len(content) > 800:
                emotional_context = "...\n" + content[-800:]
            else:
                emotional_context = content

    prompt = load_prompt(
        "chat",
        SOUL=soul,
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
        return build_chat_prompt(
            text, lite=True,
            instance_dir=instance_dir,
            koan_root=koan_root,
            soul=soul,
            summary=summary,
            conversation_history_file=conversation_history_file,
            missions_file=missions_file,
            project_path=project_path,
        )

    return prompt


def clean_chat_response(text: str, user_message: str = "") -> str:
    """Clean Claude CLI output for Telegram delivery.

    Strips error artifacts, markdown, truncates for smartphone reading,
    and expands bare #123 GitHub refs to clickable URLs.
    """
    from app.text_utils import clean_cli_response, expand_github_refs_auto

    cleaned = clean_cli_response(text)
    return expand_github_refs_auto(cleaned, user_message)
