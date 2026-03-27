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


def _get_in_progress_missions(instance_dir):
    """Get in-progress missions from missions.md.

    Returns a list of (project, mission_text) tuples, or empty list.
    """
    missions_file = instance_dir / "missions.md"
    if not missions_file.exists():
        return []

    try:
        from app.missions import parse_sections, extract_project_tag, strip_timestamps
        from app.utils import parse_project

        content = missions_file.read_text()
        sections = parse_sections(content)
        in_progress = sections.get("in_progress", [])
        if not in_progress:
            return []

        result = []
        for mission in in_progress:
            first_line = mission.split("\n")[0].lstrip("- ").strip()
            project = extract_project_tag(first_line)
            _, display = parse_project(first_line)
            display = strip_timestamps(display).strip()
            result.append((project, display))
        return result
    except Exception:
        return []


def _format_no_output(missions):
    """Format a message for running missions with no output available."""
    if len(missions) == 1:
        project, text = missions[0]
        return f"Mission [{project}] running: {text}\nNo output available yet."

    lines = []
    for project, text in missions:
        lines.append(f"- [{project}] {text}")
    return "Missions running:\n" + "\n".join(lines) + "\nNo output available yet."


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
    if progress:
        return _format_progress(progress)

    # No pending.md — check if missions are actually in progress
    missions = _get_in_progress_missions(ctx.instance_dir)
    if missions:
        return _format_no_output(missions)

    return "No mission running."
