"""Koan incident skill -- queue an incident triage mission."""


# Maximum error text length to avoid consuming too much context window.
_MAX_ERROR_LENGTH = 4000


def handle(ctx):
    """Handle /incident command -- queue a mission to triage a production error.

    Usage:
        /incident <error text or stack trace>
        /incident <project> <error text>

    Queues a mission that invokes Claude to parse the error, identify
    affected code, check recent commits for regressions, and propose a fix.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage:\n"
            "  /incident <error text> -- triage for default project\n"
            "  /incident <project> <error text> -- triage for a specific project\n\n"
            "Paste a stack trace, error log, or error message. "
            "Queues a mission that analyzes the error, identifies root cause, "
            "and proposes a fix with tests."
        )

    project, error_text = _parse_project_arg(args)

    if not error_text:
        return "Please provide an error to triage. Paste a stack trace or error message."

    return _queue_incident(ctx, project, error_text)


def _parse_project_arg(args):
    """Parse optional project prefix from args.

    Supports:
        /incident koan TypeError: ...   -> ("koan", "TypeError: ...")
        /incident [project:koan] Error   -> ("koan", "Error")
        /incident TypeError: ...         -> (None, "TypeError: ...")
    """
    from app.utils import parse_project, get_known_projects

    # Try [project:X] tag first
    project, cleaned = parse_project(args)
    if project:
        return project, cleaned

    # Try first word as project name (only if it matches a known project)
    parts = args.split(None, 1)
    if len(parts) < 2:
        return None, args

    candidate = parts[0].lower()
    known = get_known_projects()
    for name, _ in known:
        if name.lower() == candidate:
            return name, parts[1]

    return None, args


def _resolve_project_path(project_name):
    """Resolve project name to its local path."""
    from pathlib import Path
    from app.utils import get_known_projects, resolve_project_path

    if project_name:
        path = resolve_project_path(project_name)
        if path:
            return path
        for name, path in get_known_projects():
            if name.lower() == project_name.lower():
                return path
            if Path(path).name.lower() == project_name.lower():
                return path
        return None

    # Fall back to the first known project
    projects = get_known_projects()
    if projects:
        return projects[0][1]

    return ""


def _queue_incident(ctx, project_name, error_text):
    """Queue a mission to triage a production error."""
    from app.utils import insert_pending_mission, project_name_for_path

    project_path = _resolve_project_path(project_name)
    if not project_path:
        from app.utils import get_known_projects
        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return f"Project '{project_name}' not found. Known: {known}"

    project_label = project_name or project_name_for_path(project_path)

    # Truncate very long error text
    truncated = error_text[:_MAX_ERROR_LENGTH]
    if len(error_text) > _MAX_ERROR_LENGTH:
        truncated += "\n[... truncated]"

    mission_entry = f"- [project:{project_label}] /incident {truncated}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    preview = error_text[:80].replace("\n", " ")
    return (
        f"\U0001f6a8 Incident queued: {preview}"
        f"{'...' if len(error_text) > 80 else ''}"
        f" (project: {project_label})"
    )
