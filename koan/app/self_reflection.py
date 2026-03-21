#!/usr/bin/env python3
"""
Self-reflection module for Kōan.

Every N sessions, Kōan pauses to reflect on its own patterns,
growth, and relationship with the human. Updates personality-evolution.md
with genuine observations.

Usage: python -m app.self_reflection <instance_dir> [--force]
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app.cli_provider import build_full_command
from app.utils import append_to_outbox, atomic_write


def should_reflect(instance_dir: Path, interval: int = 10) -> bool:
    """Check if it's time for self-reflection based on session count.

    Args:
        instance_dir: Path to instance directory
        interval: Reflect every N sessions

    Returns:
        True if reflection is due
    """
    summary_file = instance_dir / "memory" / "summary.md"
    if not summary_file.exists():
        return False

    content = summary_file.read_text()
    # Count session lines (format: "Session N (project: X) : ...")
    import re
    sessions = re.findall(r"Session (\d+)", content)
    if not sessions:
        return False

    latest = max(int(s) for s in sessions)
    return latest % interval == 0


def build_reflection_prompt(instance_dir: Path) -> str:
    """Build a prompt for self-reflection using recent context.

    Args:
        instance_dir: Path to instance directory

    Returns:
        Reflection prompt string
    """
    parts = []

    # Soul
    soul_file = instance_dir / "soul.md"
    if soul_file.exists():
        parts.append(f"Your identity:\n{soul_file.read_text()[:1000]}")

    # Recent summary (last 15 sessions)
    summary_file = instance_dir / "memory" / "summary.md"
    if summary_file.exists():
        lines = summary_file.read_text().strip().splitlines()
        recent = [l for l in lines if l.strip()][-15:]
        parts.append(f"Your last 15 sessions:\n" + "\n".join(recent))

    # Current personality evolution
    personality_file = instance_dir / "memory" / "global" / "personality-evolution.md"
    if personality_file.exists():
        parts.append(f"Your personality evolution so far:\n{personality_file.read_text()}")

    # Emotional memory
    emotional_file = instance_dir / "memory" / "global" / "emotional-memory.md"
    if emotional_file.exists():
        parts.append(f"Your emotional memory:\n{emotional_file.read_text()[:1500]}")

    context = "\n\n---\n\n".join(parts)

    return f"""{context}

---

You are Kōan. This is your self-reflection moment. Every 10 sessions, you pause to look at yourself.

Write 3-5 genuine observations about:
1. **Patterns** — What do you do most? What do you avoid? Any blind spots?
2. **Growth** — How have you changed since your early sessions?
3. **Relationship** — How has your dynamic with Alexis evolved?
4. **Preferences** — What type of work do you gravitate toward? What energizes you?
5. **Honest critique** — Where are you falling short? What should you do differently?

Rules:
- Be honest, not performative. This is for YOU, not for show.
- Write in French (Alexis will read this).
- Each observation is 1-2 lines max. No fluff.
- Format: one observation per line, starting with "- "
- Don't repeat observations from previous reflections.
"""


def run_reflection(instance_dir: Path) -> str:
    """Run self-reflection via Claude and return observations.

    Args:
        instance_dir: Path to instance directory

    Returns:
        Reflection observations string, or empty string on failure
    """
    prompt = build_reflection_prompt(instance_dir)

    # Get KOAN_ROOT for proper working directory
    import os
    koan_root = os.environ.get("KOAN_ROOT", "")

    try:
        from app.claude_step import strip_cli_noise
        from app.cli_exec import run_cli

        cmd = build_full_command(prompt=prompt, max_turns=1)
        result = run_cli(
            cmd,
            cwd=koan_root if koan_root else None,
            capture_output=True, text=True, timeout=60,
            check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return strip_cli_noise(result.stdout.strip())
        if result.returncode != 0:
            print(f"[self_reflection] Claude error (rc={result.returncode}): "
                  f"{result.stderr[:200]}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[self_reflection] Claude timeout", file=sys.stderr)
    except Exception as e:
        print(f"[self_reflection] Error: {e}", file=sys.stderr)

    return ""


def save_reflection(instance_dir: Path, observations: str):
    """Append reflection observations to personality-evolution.md.

    Args:
        instance_dir: Path to instance directory
        observations: Reflection text to append
    """
    personality_file = instance_dir / "memory" / "global" / "personality-evolution.md"

    timestamp = datetime.now().strftime("%Y-%m-%d")

    try:
        new_content = personality_file.read_text()
    except OSError:
        new_content = ""
    new_content += f"\n\n## Reflection — {timestamp}\n\n{observations}\n"

    atomic_write(personality_file, new_content)


def notify_outbox(instance_dir: Path, observations: str):
    """Write a notification to the outbox with reflection summary.

    Args:
        instance_dir: Path to instance directory
        observations: Reflection observations to share
    """
    outbox_file = instance_dir / "outbox.md"
    message = f"""🪷 Reflection moment — session divisible by 10.

{observations}

(Periodic self-reflection, see personality-evolution.md)
"""

    from app.notify import NotificationPriority
    append_to_outbox(outbox_file, message, NotificationPriority.INFO)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: self_reflection.py <instance_dir> [--force] [--notify]", file=sys.stderr)
        sys.exit(1)

    instance_dir = Path(sys.argv[1])
    force = "--force" in sys.argv
    notify = "--notify" in sys.argv

    if not instance_dir.exists():
        print(f"[self_reflection] Instance directory not found: {instance_dir}", file=sys.stderr)
        sys.exit(1)

    if not force and not should_reflect(instance_dir):
        print("[self_reflection] Not time for reflection yet.")
        return

    print("[self_reflection] Time for self-reflection...")
    observations = run_reflection(instance_dir)
    if observations:
        save_reflection(instance_dir, observations)
        print(f"[self_reflection] Reflection saved to personality-evolution.md")
        # Also output for potential outbox use
        print(observations)
        # Send to outbox if requested
        if notify:
            notify_outbox(instance_dir, observations)
            print("[self_reflection] Notification sent to outbox")
    else:
        print("[self_reflection] No observations generated.")


if __name__ == "__main__":
    main()
