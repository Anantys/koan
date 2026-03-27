"""Koan list skill -- show current missions (pending + in progress)."""

import re

# Unicode prefixes for mission categories.
_CATEGORY_PREFIXES = {
    "plan": "🧠",
    "implement": "🔨",
    "fix": "🐞",
    "rebase": "🔄",
    "recreate": "🔁",
    "ai": "✨",
    "magic": "✨",
    "review": "🔍",
    "check": "✅",
    "refactor": "🛠️",
    "claudemd": "📝",
    "claude": "📝",
    "claude_md": "📝",
}

_MISSION_PREFIX = "📋"

# Trailing marker appended by GitHub @mention missions.
_GITHUB_ORIGIN_MARKER = "📬"

# Extract slash command from raw mission line (after optional "- " and [project:X]).
_COMMAND_RE = re.compile(
    r"^(?:-\s*)?(?:\[projec?t:[a-zA-Z0-9_-]+\]\s*)?/([a-zA-Z0-9_.]+)"
)


def mission_prefix(raw_line):
    """Return a unicode prefix for a mission line based on its category.

    Known slash commands get their category emoji.
    Unknown slash commands and free-text missions both get the generic 📋.
    """
    m = _COMMAND_RE.match(raw_line.strip())
    if m:
        command = m.group(1).lower()
        return _CATEGORY_PREFIXES.get(command, _MISSION_PREFIX)
    return _MISSION_PREFIX


def handle(ctx):
    """Handle /list command -- display numbered mission list."""
    missions_file = ctx.instance_dir / "missions.md"

    if not missions_file.exists():
        return "ℹ️ No missions file found."

    from app.missions import parse_sections, clean_mission_display

    content = missions_file.read_text()
    sections = parse_sections(content)

    in_progress = sections.get("in_progress", [])
    pending = sections.get("pending", [])

    if not in_progress and not pending:
        return "ℹ️ No missions pending or in progress."

    parts = []

    if in_progress:
        parts.append("IN PROGRESS")
        for i, m in enumerate(in_progress, 1):
            prefix = mission_prefix(m)
            display = clean_mission_display(m)
            origin = _GITHUB_ORIGIN_MARKER if _GITHUB_ORIGIN_MARKER in m else ""
            if prefix:
                parts.append(f"  {i}. {origin}{prefix} {display}")
            else:
                parts.append(f"  {i}. {origin}{display}")
        parts.append("")

    if pending:
        parts.append("PENDING")
        for i, m in enumerate(pending, 1):
            prefix = mission_prefix(m)
            display = clean_mission_display(m)
            origin = _GITHUB_ORIGIN_MARKER if _GITHUB_ORIGIN_MARKER in m else ""
            if prefix:
                parts.append(f"  {i}. {origin}{prefix} {display}")
            else:
                parts.append(f"  {i}. {origin}{display}")

    return "\n".join(parts)
