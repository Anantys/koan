"""Kōan live progress skill — show current mission progress."""


def _read_live_progress(instance_dir):
    """Read full live progress from journal/pending.md.

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
    """Format progress for Telegram: wrap activity lines in a code block.

    The pending.md format is:
        # Mission: ...
        Project: ...
        Started: ...
        ---
        HH:MM — did X
        HH:MM — did Y

    Everything after '---' is wrapped in a code block for clean rendering.
    """
    parts = content.split("\n---\n", 1)
    if len(parts) < 2 or not parts[1].strip():
        return content

    header = parts[0]
    activity = parts[1].strip()
    return f"{header}\n\n```\n{activity}\n```"


def handle(ctx):
    """Handle /live command — show live progress of current mission."""
    progress = _read_live_progress(ctx.instance_dir)
    if not progress:
        return "No mission running."
    return _format_progress(progress)
