"""Koan brainstorm skill -- queue a brainstorm mission."""

import re


def handle(ctx):
    """Handle /brainstorm command -- queue a mission to decompose a topic.

    Usage:
        /brainstorm                                -- usage help
        /brainstorm <topic>                        -- brainstorm for default project
        /brainstorm <project> <topic>              -- brainstorm for a specific project
        /brainstorm <topic> --tag <label>          -- with explicit tag

    Queues a mission that invokes Claude to decompose the topic into
    3-8 detailed sub-issues, creates them on GitHub, and links them
    under a master tracking issue.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage:\n"
            "  /brainstorm <topic> -- decompose into linked issues\n"
            "  /brainstorm <project> <topic> -- for a specific project\n"
            "  /brainstorm <topic> --tag <label> -- with explicit tag\n\n"
            "Creates 3-8 detailed sub-issues grouped under a master "
            "tracking issue on GitHub. If --tag is omitted, a tag is "
            "auto-generated from the topic."
        )

    # Parse --tag from args
    tag, cleaned_args = _extract_tag(args)

    # Parse optional project prefix
    project, topic = _parse_project_arg(cleaned_args)

    if not topic:
        return "Please provide a topic to brainstorm. Ex: /brainstorm Improve caching strategy"

    # Build mission entry with tag if provided
    tag_suffix = f" --tag {tag}" if tag else ""
    mission_text = f"/brainstorm {topic}{tag_suffix}"

    return _queue_brainstorm(ctx, project, mission_text, topic)


def _extract_tag(args):
    """Extract --tag <label> from args.

    Returns (tag, remaining_args). tag is None if not found.
    """
    match = re.search(r'--tag\s+(\S+)', args)
    if not match:
        return None, args
    tag = match.group(1)
    remaining = args[:match.start()].rstrip() + args[match.end():]
    return tag, remaining.strip()


def _parse_project_arg(args):
    """Parse optional project prefix from args.

    Supports:
        /brainstorm koan Fix the bug        -> ("koan", "Fix the bug")
        /brainstorm [project:koan] Fix bug  -> ("koan", "Fix bug")
        /brainstorm Fix the bug             -> (None, "Fix the bug")
    """
    from app.utils import parse_project, get_known_projects

    # Try [project:X] tag first
    project, cleaned = parse_project(args)
    if project:
        return project, cleaned

    # Try first word as project name
    parts = args.split(None, 1)
    if len(parts) < 2:
        return None, args

    candidate = parts[0].lower()
    known = get_known_projects()
    for name, _ in known:
        if name.lower() == candidate:
            return name, parts[1]

    return None, args


def _queue_brainstorm(ctx, project_name, mission_text, topic):
    """Queue a brainstorm mission."""
    from app.utils import insert_pending_mission

    project_path = _resolve_project_path(project_name)
    if not project_path:
        from app.utils import get_known_projects
        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return f"Project '{project_name}' not found. Known: {known}"

    project_label = project_name or _project_name_for_path(project_path)

    mission_entry = f"- [project:{project_label}] {mission_text}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    preview = topic[:100] + ('...' if len(topic) > 100 else '')
    return f"\U0001f9e0 Brainstorm queued: {preview} (project: {project_label})"


def _resolve_project_path(project_name, fallback=False, owner=None):
    """Resolve project name to its local path."""
    from pathlib import Path
    from app.utils import get_known_projects, resolve_project_path

    if project_name:
        if owner:
            path = resolve_project_path(project_name, owner=owner)
            if path:
                return path
        for name, path in get_known_projects():
            if name.lower() == project_name.lower():
                return path
        for name, path in get_known_projects():
            if Path(path).name.lower() == project_name.lower():
                return path
        if not fallback:
            return None

    projects = get_known_projects()
    if projects:
        return projects[0][1]

    return ""


def _project_name_for_path(project_path):
    """Get project name from path, checking known projects first."""
    from app.utils import project_name_for_path
    return project_name_for_path(project_path)
