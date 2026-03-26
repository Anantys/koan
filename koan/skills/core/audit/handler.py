"""Koan /audit skill -- queue a codebase audit mission."""


def handle(ctx):
    """Handle /audit command -- queue a codebase audit mission.

    Usage:
        /audit <project>                   -- audit the project
        /audit <project> <extra context>   -- audit with focus guidance
    """
    args = ctx.args.strip()

    if args in ("-h", "--help"):
        return (
            "Usage: /audit <project-name> [extra context]\n\n"
            "Audits a project for optimizations, simplifications, "
            "and potential issues. Creates a GitHub issue for each finding.\n\n"
            "Examples:\n"
            "  /audit koan\n"
            "  /audit myapp focus on the auth module\n"
            "  /audit webapp look for performance bottlenecks"
        )

    if not args:
        return (
            "\u274c Usage: /audit <project-name> [extra context]\n"
            "Example: /audit koan focus on error handling"
        )

    # First word is project name, rest is extra context
    parts = args.split(None, 1)
    project_name = parts[0]
    extra_context = parts[1] if len(parts) > 1 else ""

    return _queue_audit(ctx, project_name, extra_context)


def _queue_audit(ctx, project_name, extra_context):
    """Queue an audit mission."""
    from app.utils import insert_pending_mission, resolve_project_path

    path = resolve_project_path(project_name)
    if not path:
        from app.utils import get_known_projects

        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return (
            f"\u274c Unknown project '{project_name}'.\n"
            f"Known projects: {known}"
        )

    suffix = f" {extra_context}" if extra_context else ""
    mission_entry = f"- [project:{project_name}] /audit{suffix}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    context_hint = f" (focus: {extra_context})" if extra_context else ""
    return f"\U0001f50e Audit queued for {project_name}{context_hint}"
