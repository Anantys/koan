#!/usr/bin/env python3
"""
Send session retrospective when budget is exhausted.

Extracts session summary from today's journal and appends to outbox
for delivery via Telegram. Called when usage tracker enters WAIT mode.

Usage: python send_retrospective.py <instance_dir> <project_name>
"""

import sys
from datetime import date
from pathlib import Path


def get_todays_journal(instance_dir: Path, project_name: str) -> Path:
    """Find today's journal file for the given project.

    Delegates to utils.get_journal_file for the actual path resolution.
    """
    from app.journal import get_journal_file
    return get_journal_file(instance_dir, date.today(), project_name)


def extract_session_summary(journal_path: Path, max_chars: int = 800) -> str:
    """Extract a summary of the entire session from journal.

    Args:
        journal_path: Path to today's journal file
        max_chars: Maximum characters to extract

    Returns:
        Summary text for retrospective (raw, will be formatted by Claude later)
    """
    if not journal_path.exists():
        return "No journal entries today — session was brief."

    content = journal_path.read_text()
    if not content.strip():
        return "No journal entries today — session was brief."

    if len(content) <= max_chars:
        return content

    # Try to extract last 2-3 markdown sections for coherent context
    sections = content.split("\n## ")
    if len(sections) > 1:
        summary = "\n## ".join(sections[-3:])
        if len(summary) <= max_chars:
            return summary

    # Fall back to raw tail
    return "..." + content[-max_chars:]


def append_to_outbox(instance_dir: Path, message: str, priority=None):
    """Append message to outbox.md with file locking.

    Args:
        instance_dir: Path to instance directory
        message: Message to append
        priority: Optional NotificationPriority for the message
    """
    from app.utils import append_to_outbox as _append

    outbox_file = instance_dir / "outbox.md"
    try:
        _append(outbox_file, message + "\n", priority=priority)
    except OSError as e:
        print(f"[send_retrospective] Error writing to outbox: {e}", file=sys.stderr)


def create_retrospective(instance_dir: Path, project_name: str):
    """Create and send retrospective when budget is exhausted.

    Args:
        instance_dir: Path to instance directory
        project_name: Name of current project
    """
    journal_path = get_todays_journal(instance_dir, project_name)
    summary = extract_session_summary(journal_path)

    # Create retrospective message (raw format - will be formatted by format_outbox.py)
    retrospective = f"""# Session Retrospective — {date.today():%Y-%m-%d}

**Project**: {project_name}
**Status**: Budget exhausted, Kōan paused

{summary}

---
*Kōan paused due to quota limit. Use /resume command when quota resets.*
"""

    from app.notify import NotificationPriority
    append_to_outbox(instance_dir, retrospective, priority=NotificationPriority.WARNING)
    print(f"[send_retrospective] Retrospective sent to outbox ({len(retrospective)} chars)")

    # Also email the digest if email is configured
    try:
        from app.email_notify import send_session_digest
        if send_session_digest(project_name, retrospective):
            print("[send_retrospective] Session digest emailed to owner")
    except Exception as e:
        print(f"[send_retrospective] Email digest skipped: {e}", file=sys.stderr)


def main():
    """CLI entry point."""
    if len(sys.argv) < 3:
        print("Usage: send_retrospective.py <instance_dir> <project_name>", file=sys.stderr)
        sys.exit(1)

    instance_dir = Path(sys.argv[1])
    project_name = sys.argv[2]

    if not instance_dir.exists():
        print(f"[send_retrospective] Instance directory not found: {instance_dir}", file=sys.stderr)
        sys.exit(1)

    create_retrospective(instance_dir, project_name)


if __name__ == "__main__":
    main()
