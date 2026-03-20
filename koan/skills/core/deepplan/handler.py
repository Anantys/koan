"""Kōan deepplan skill -- queue a spec-first design mission."""


def handle(ctx):
    """Handle /deepplan command -- queue a mission to spec-design an idea.

    Usage:
        /deepplan                          -- usage help
        /deepplan <idea>                   -- deepplan for default project
        /deepplan <project> <idea>         -- deepplan for a specific project

    Queues a mission that invokes Claude to explore 2-3 design approaches,
    run a spec review loop, post the spec as a GitHub issue, and queue a
    follow-up /plan mission for human approval.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage:\n"
            "  /deepplan <idea> -- spec-first design for default project\n"
            "  /deepplan <project> <idea> -- for a specific project\n\n"
            "Explores 2-3 design approaches, posts a spec as a GitHub issue,\n"
            "then queues /plan for your approval. Catches design flaws before\n"
            "any code is written."
        )

    # Parse optional project prefix
    project, idea = _parse_project_arg(args)

    if not idea:
        return "Please provide an idea. Ex: /deepplan Refactor the auth middleware"

    return _queue_deepplan(ctx, project, idea)


def _parse_project_arg(args):
    """Parse optional project prefix from args.

    Supports:
        /deepplan koan Fix the bug        -> ("koan", "Fix the bug")
        /deepplan [project:koan] Fix bug  -> ("koan", "Fix bug")
        /deepplan Fix the bug             -> (None, "Fix the bug")
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


def _queue_deepplan(ctx, project_name, idea):
    """Queue a deepplan mission."""
    from app.utils import insert_pending_mission

    project_path = _resolve_project_path(project_name)
    if not project_path:
        from app.utils import get_known_projects
        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return f"Project '{project_name}' not found. Known: {known}"

    project_label = project_name or _project_name_for_path(project_path)

    mission_entry = f"- [project:{project_label}] /deepplan {idea}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    preview = idea[:100] + ('...' if len(idea) > 100 else '')
    return f"\U0001f9e0 Deep plan queued: {preview} (project: {project_label})"


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
