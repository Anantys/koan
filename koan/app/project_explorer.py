"""Shared project exploration utilities.

Functions used by both /magic (interactive Telegram) and /ai (queued mission)
to gather project context before asking Claude for creative ideas.
"""

import sys
from pathlib import Path
from typing import List, Tuple

from app.git_sync import run_git


def get_projects() -> List[Tuple[str, str]]:
    """Get list of (name, path) for configured projects.

    Returns only projects whose paths exist on disk.
    """
    from app.utils import get_known_projects

    try:
        return [(n, p) for n, p in get_known_projects() if Path(p).is_dir()]
    except Exception as e:
        print(f"[project_explorer] get_projects error: {e}", file=sys.stderr)
        return []


def gather_git_activity(project_path: str) -> str:
    """Gather recent git activity for a project.

    Runs git log, branch list, and diff stat to build a context string.
    """
    parts = []

    commits = run_git(project_path, "log", "--oneline", "-15", "--no-merges")
    if commits:
        parts.append("Recent commits:\n" + commits)

    branches_out = run_git(
        project_path, "branch", "-r", "--sort=-committerdate",
        "--format=%(refname:short)",
    )
    if branches_out:
        branches = branches_out.split("\n")[:10]
        parts.append("Active branches:\n" + "\n".join(branches))

    # Use diffstat from the log instead of HEAD~10 which fails on repos
    # with fewer than 10 commits.
    diff_stat = run_git(project_path, "log", "--stat", "--format=", "-10")
    if diff_stat:
        parts.append("Recent changes:\n" + diff_stat)

    return "\n\n".join(parts) if parts else "No git activity available."


def gather_project_structure(project_path: str) -> str:
    """Gather top-level project structure (dirs and files)."""
    try:
        p = Path(project_path)
        entries = sorted(p.iterdir())
        dirs = [
            e.name + "/"
            for e in entries
            if e.is_dir() and not e.name.startswith(".")
        ]
        files = [
            e.name
            for e in entries
            if e.is_file() and not e.name.startswith(".")
        ]
        parts = []
        if dirs:
            parts.append("Directories: " + ", ".join(dirs[:20]))
        if files:
            parts.append("Files: " + ", ".join(files[:20]))
        return "\n".join(parts)
    except OSError:
        return "Structure unavailable."


def get_missions_context(instance_dir: Path) -> str:
    """Get current missions context for exploration prompts."""
    missions_file = instance_dir / "missions.md"
    if not missions_file.exists():
        return "No active missions."

    from app.missions import parse_sections

    sections = parse_sections(missions_file.read_text())
    in_progress = sections.get("in_progress", [])
    pending = sections.get("pending", [])
    parts = []
    if in_progress:
        parts.append("In progress:\n" + "\n".join(in_progress[:5]))
    if pending:
        parts.append("Pending:\n" + "\n".join(pending[:5]))
    return "\n".join(parts) if parts else "No active missions."
