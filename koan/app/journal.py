"""Journal management — extracted from utils.py for clarity.

Handles reading, writing, and locating daily journal files.
Supports both flat (journal/YYYY-MM-DD.md) and nested
(journal/YYYY-MM-DD/project.md) structures.
"""

import fcntl
from pathlib import Path
from typing import Optional


def _to_date_string(target_date) -> str:
    """Convert a date object or string to YYYY-MM-DD format."""
    if hasattr(target_date, 'strftime'):
        return target_date.strftime("%Y-%m-%d")
    return str(target_date)


def get_journal_file(instance_dir: Path, target_date, project_name: str) -> Path:
    """Find journal file for a project on a given date.

    Supports both nested (journal/YYYY-MM-DD/project.md) and
    flat (journal/YYYY-MM-DD.md) structures. Returns nested path as default.

    Args:
        instance_dir: Path to instance directory
        target_date: date object or string "YYYY-MM-DD"
        project_name: Project name (used for nested structure)

    Returns:
        Path to journal file (may not exist)
    """
    date_str = _to_date_string(target_date)

    journal_dir = instance_dir / "journal"
    nested = journal_dir / date_str / f"{project_name}.md"
    if nested.exists():
        return nested

    flat = journal_dir / f"{date_str}.md"
    if flat.exists():
        return flat

    return nested


def read_all_journals(instance_dir: Path, target_date) -> str:
    """Read all journal entries for a date across all project subdirs.

    Combines flat (legacy) and nested per-project files.

    Args:
        instance_dir: Path to instance directory
        target_date: date object or string "YYYY-MM-DD"

    Returns:
        Combined journal content
    """
    date_str = _to_date_string(target_date)

    journal_base = instance_dir / "journal"
    journal_dir = journal_base / date_str
    parts = []

    # Check for flat file (legacy)
    flat = journal_base / f"{date_str}.md"
    if flat.is_file():
        parts.append(flat.read_text())

    # Check nested per-project files
    if journal_dir.is_dir():
        for f in sorted(journal_dir.iterdir()):
            if f.suffix == ".md":
                parts.append(f"[{f.stem}]\n{f.read_text()}")

    return "\n\n---\n\n".join(parts)


def get_latest_journal(instance_dir: Path, project: Optional[str] = None,
                       target_date=None, max_chars: int = 500) -> str:
    """Read the latest journal entry, optionally filtered by project.

    Args:
        instance_dir: Path to instance directory
        project: Project name filter (None = all projects)
        target_date: date object or "YYYY-MM-DD" string (None = today)
        max_chars: Maximum characters to return (tail)

    Returns:
        Formatted journal excerpt or informative "nothing found" message
    """
    from datetime import date as _date

    if target_date is None:
        target_date = _date.today()

    date_str = _to_date_string(target_date)

    if project:
        journal_path = get_journal_file(instance_dir, target_date, project)
        if not journal_path.exists():
            return f"No journal for {project} on {date_str}."
        content = journal_path.read_text().strip()
        if not content:
            return f"Empty journal for {project} on {date_str}."
        header = f"\U0001f4d3 {project} \u2014 {date_str}"
    else:
        content = read_all_journals(instance_dir, target_date)
        if not content:
            return f"No journal for {date_str}."
        header = f"\U0001f4d3 Journal \u2014 {date_str}"

    # Tail: keep last max_chars
    if len(content) > max_chars:
        content = "...\n" + content[-(max_chars - 4):]

    return f"{header}\n\n{content}"


def append_to_journal(instance_dir: Path, project_name: str, content: str):
    """Append content to today's journal file for a project.

    Creates the directory structure if needed. Uses file locking.

    Args:
        instance_dir: Path to instance directory
        project_name: Project name
        content: Content to append
    """
    from datetime import datetime as _dt
    date_str = _dt.now().strftime("%Y-%m-%d")
    journal_dir = instance_dir / "journal" / date_str
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_file = journal_dir / f"{project_name}.md"

    from app.leak_detector import scan_and_redact

    content = scan_and_redact(content, context="journal")

    with open(journal_file, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(content)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
