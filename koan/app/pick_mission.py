#!/usr/bin/env python3
"""
Kōan — FIFO mission picker

Picks the first pending mission from missions.md in strict queue order.
The human controls priority via queue position (--now flag, /priority command).

Usage:
    python3 pick_mission.py <instance_dir> <projects_str> <run_num> <autonomous_mode> [last_project]

Output (stdout):
    project_name:mission title    — if a mission is picked
    (empty)                       — if autonomous mode (no pending missions)
"""

import re
import sys
from pathlib import Path


def fallback_extract(content: str, projects_str: str) -> tuple:
    """Extract the first pending mission in FIFO order."""
    from app.missions import extract_next_pending

    line = extract_next_pending(content)
    if not line:
        return (None, None)

    # Try to extract project from inline tag
    tag = re.search(r"\[projec?t:([a-zA-Z0-9_-]+)\]", line)
    if tag:
        project = tag.group(1)
        title = re.sub(r"\[projec?t:[a-zA-Z0-9_-]+\]\s*", "", line).lstrip("- ").strip()
    else:
        # Default to first project
        parts = [p for p in projects_str.split(";") if p]
        project = parts[0].split(":")[0] if parts else "default"
        title = line.lstrip("- ").strip()

    return (project, title)


def pick_mission(
    instance_dir: str,
    projects_str: str,
    run_num: str,
    autonomous_mode: str,
    last_project: str = "",
) -> str:
    """Pick the next mission in strict FIFO order.

    Always picks the first pending mission from missions.md.
    Queue position is the sole priority signal — no LLM-based reordering.
    Returns 'project:title' or empty string.
    """
    instance = Path(instance_dir)
    missions_path = instance / "missions.md"

    try:
        missions_content = missions_path.read_text()
    except FileNotFoundError:
        return ""

    # Quick check: any pending missions at all?
    from app.missions import count_pending
    pending_count = count_pending(missions_content)
    if pending_count == 0:
        return ""

    project, title = fallback_extract(missions_content, projects_str)
    if project and title:
        return f"{project}:{title}"
    return ""


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(
            f"Usage: {sys.argv[0]} <instance_dir> <projects_str> <run_num> <autonomous_mode> [last_project]",
            file=sys.stderr,
        )
        sys.exit(1)

    instance_dir = sys.argv[1]
    projects_str = sys.argv[2]
    run_num = sys.argv[3]
    autonomous_mode = sys.argv[4]
    last_project = sys.argv[5] if len(sys.argv) > 5 else ""

    result = pick_mission(instance_dir, projects_str, run_num, autonomous_mode, last_project)
    print(result)
