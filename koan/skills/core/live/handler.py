"""Kōan live progress skill — show current mission progress."""

# Maximum activity lines to show in /live output.
# Keeps Telegram messages readable without scrolling.
_MAX_ACTIVITY_LINES = 30


def _read_live_progress(instance_dir):
    """Read live progress from journal/pending.md.

    Returns the mission header and all progress lines,
    or None if no mission is running.
    """
    pending_path = instance_dir / "journal" / "pending.md"
    if not pending_path.exists():
        return None

    content = pending_path.read_text().strip()
    if not content:
        return None

    return content


def _format_progress(content):
    """Format progress for Telegram: wrap activity tail in a code block.

    The pending.md format is:
        # Mission: ...
        Project: ...
        Started: ...
        ---
        HH:MM — did X
        HH:MM — did Y
        ... (CLI output when streaming)

    Shows the header plus the last N activity lines in a code block.
    When output is truncated, a note indicates how many lines were skipped.
    """
    parts = content.split("\n---\n", 1)
    if len(parts) < 2 or not parts[1].strip():
        return content

    header = parts[0]
    activity_lines = parts[1].strip().splitlines()

    total = len(activity_lines)
    if total > _MAX_ACTIVITY_LINES:
        skipped = total - _MAX_ACTIVITY_LINES
        tail = activity_lines[-_MAX_ACTIVITY_LINES:]
        activity = "\n".join(tail)
        return (
            f"{header}\n\n"
            f"_({skipped} earlier lines omitted)_\n"
            f"```\n{activity}\n```"
        )

    activity = "\n".join(activity_lines)
    return f"{header}\n\n```\n{activity}\n```"


def handle(ctx):
    """Handle /live command — show live progress of current mission."""
    progress = _read_live_progress(ctx.instance_dir)
    if not progress:
        return "No mission running."
    return _format_progress(progress)
