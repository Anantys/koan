#!/usr/bin/env python3
"""
Post-mission reflection module for Kōan.

After significant missions (audit, major feature, deep work), Kōan writes
a deeper reflection to the shared-journal.md — asynchronous conversation space.

Dual heuristic gate: keyword match on mission title AND substantial journal
content (>= 500 chars). Both must be true to prevent noise from trivial
missions. Duration >= 45min overrides the journal length check.

Usage: python -m app.post_mission_reflection <instance_dir> <mission_text> <duration_minutes> [--journal-file <path>]
"""

import sys
from datetime import datetime
from pathlib import Path

from app.prompts import get_prompt_path
from app.utils import atomic_write


# Keywords indicating significant missions
SIGNIFICANT_KEYWORDS = [
    "audit",
    "security",
    "feature",
    "refactor",
    "architecture",
    "deep",
    "strategic",
    "migration",
    "overhaul",
    "redesign",
    "introspect",
    "retrospective",
    "breaking",
    "performance",
]

# Minimum duration (minutes) to qualify as significant without keywords
MIN_DURATION_MINUTES = 45

# Minimum journal content length to consider the mission substantial
MIN_JOURNAL_LENGTH = 500


def is_significant_mission(
    mission_text: str,
    duration_minutes: int,
    journal_content: str = "",
) -> bool:
    """Determine if a mission warrants a journal reflection.

    Dual heuristic:
    - Keyword match on mission title (audit, feature, refactor, etc.)
    - Journal content is substantial (>= 500 chars = real work happened)

    Both conditions must be met to prevent noise. Exception: long missions
    (>= 45 min) are significant regardless, as duration signals deep work.

    Args:
        mission_text: The mission description text
        duration_minutes: How long the mission took
        journal_content: Content of the journal entry for this mission

    Returns:
        True if mission is significant enough for reflection
    """
    if not mission_text:
        return False

    mission_lower = mission_text.lower()

    # Check keywords
    has_keyword = any(kw in mission_lower for kw in SIGNIFICANT_KEYWORDS)

    # Long missions are significant regardless of other conditions
    is_long = duration_minutes >= MIN_DURATION_MINUTES
    if is_long:
        return True

    # For keyword matches, also require substantial journal content
    has_substance = len(journal_content.strip()) >= MIN_JOURNAL_LENGTH if journal_content else False

    return has_keyword and has_substance


def _get_prompt_template() -> str:
    """Load the prompt template from system-prompts directory.

    Returns:
        Prompt template string
    """
    return get_prompt_path("post-mission-reflection").read_text()


def build_reflection_prompt(
    instance_dir: Path,
    mission_text: str,
    journal_content: str = "",
) -> str:
    """Build a prompt for journal reflection.

    Args:
        instance_dir: Path to instance directory
        mission_text: The mission that was completed
        journal_content: Content of the mission's journal entry (what actually happened)

    Returns:
        Prompt string for Claude
    """
    # Load context from instance files
    soul_context = ""
    soul_file = instance_dir / "soul.md"
    if soul_file.exists():
        soul_context = soul_file.read_text()[:800]

    emotional_context = ""
    emotional_file = instance_dir / "memory" / "global" / "emotional-memory.md"
    if emotional_file.exists():
        emotional_context = emotional_file.read_text()[:1000]

    journal_context = ""
    journal_file = instance_dir / "shared-journal.md"
    if journal_file.exists():
        content = journal_file.read_text()
        if len(content) > 1000:
            journal_context = f"...{content[-1000:]}"
        elif content.strip():
            journal_context = content

    # Truncate mission journal to keep prompt reasonable
    mission_journal = journal_content[:3000] if len(journal_content) > 3000 else journal_content

    # Load and fill template
    template = _get_prompt_template()
    prompt = template.format(
        INSTANCE=str(instance_dir),
        SOUL_CONTEXT=soul_context or "(no soul.md found)",
        EMOTIONAL_CONTEXT=emotional_context or "(no emotional-memory.md found)",
        JOURNAL_CONTEXT=journal_context or "(empty journal)",
        MISSION_TEXT=mission_text,
        MISSION_JOURNAL=mission_journal or "(no journal content available)",
    )

    # Append spec comparison if a spec was generated for this mission
    try:
        from app.spec_generator import load_spec_for_mission
        spec = load_spec_for_mission(str(instance_dir), mission_text)
        if spec:
            spec_excerpt = spec[:1500]
            prompt += (
                "\n\n## Mission Spec Comparison\n\n"
                "A spec was generated before this mission started. Compare the "
                "implementation against the original spec. Note any deviations "
                "and whether they were justified.\n\n"
                f"### Original Spec\n\n{spec_excerpt}\n"
            )
    except Exception as e:
        print(f"[post_mission_reflection] Spec comparison skipped: {e}", file=sys.stderr)

    return prompt


def write_to_journal(instance_dir: Path, reflection: str):
    """Append a reflection to the shared journal.

    Args:
        instance_dir: Path to instance directory
        reflection: The reflection text to append
    """
    journal_file = instance_dir / "shared-journal.md"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        existing = journal_file.read_text()
    except OSError:
        existing = ""
    new_content = existing.rstrip() + f"\n\n### Kōan — {timestamp}\n\n{reflection}\n"

    atomic_write(journal_file, new_content)


def run_reflection(
    instance_dir: Path,
    mission_text: str,
    journal_content: str = "",
) -> str:
    """Generate a journal reflection via Claude.

    Args:
        instance_dir: Path to instance directory
        mission_text: The mission that was completed
        journal_content: Content of the mission's journal entry

    Returns:
        Reflection text, or empty string on failure/skip
    """
    prompt = build_reflection_prompt(instance_dir, mission_text, journal_content)

    try:
        from app.claude_step import run_claude, strip_cli_noise
        from app.cli_provider import build_full_command

        cmd = build_full_command(prompt=prompt, max_turns=1)
        result = run_claude(cmd, cwd=str(instance_dir), timeout=60)

        if result["success"]:
            output = strip_cli_noise(result["output"])
            # Check for skip signal
            if output in ["—", "-", ""]:
                return ""
            return output
    except Exception as e:
        print(f"[post_mission_reflection] Error: {e}", file=sys.stderr)

    return ""


def _read_journal_file(instance_dir: Path, project_name: str, journal_path: str = "") -> str:
    """Read journal content for the current mission.

    Tries explicit path first, then falls back to today's journal file.

    Args:
        instance_dir: Path to instance directory
        project_name: Project name (for fallback path)
        journal_path: Explicit path to journal file (optional)

    Returns:
        Journal content, or empty string if not found.
    """
    if journal_path:
        path = Path(journal_path)
        if path.exists():
            return path.read_text()

    # Fallback: today's journal
    today = datetime.now().strftime("%Y-%m-%d")
    fallback = instance_dir / "journal" / today / f"{project_name}.md"
    if fallback.exists():
        return fallback.read_text()

    return ""


def main():
    """CLI entry point."""
    if len(sys.argv) < 4:
        print(
            "Usage: post_mission_reflection.py <instance_dir> <mission_text> <duration_minutes> "
            "[--journal-file <path>] [--project-name <name>] [--force]",
            file=sys.stderr,
        )
        sys.exit(1)

    instance_dir = Path(sys.argv[1])
    mission_text = sys.argv[2]
    try:
        duration_minutes = int(sys.argv[3])
    except ValueError:
        duration_minutes = 0

    force = "--force" in sys.argv

    # Parse --journal-file
    journal_path = ""
    if "--journal-file" in sys.argv:
        idx = sys.argv.index("--journal-file")
        if idx + 1 < len(sys.argv):
            journal_path = sys.argv[idx + 1]

    # Parse --project-name
    project_name = ""
    if "--project-name" in sys.argv:
        idx = sys.argv.index("--project-name")
        if idx + 1 < len(sys.argv):
            project_name = sys.argv[idx + 1]

    if not instance_dir.exists():
        print(
            f"[post_mission_reflection] Instance directory not found: {instance_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    journal_content = _read_journal_file(instance_dir, project_name, journal_path)

    if not force and not is_significant_mission(mission_text, duration_minutes, journal_content):
        print("[post_mission_reflection] Mission not significant enough for reflection.")
        return

    print("[post_mission_reflection] Generating journal reflection...")
    reflection = run_reflection(instance_dir, mission_text, journal_content)

    if reflection:
        write_to_journal(instance_dir, reflection)
        print("[post_mission_reflection] Reflection written to shared-journal.md")
        print(reflection)
    else:
        print("[post_mission_reflection] No reflection generated (or skipped).")


if __name__ == "__main__":
    main()
