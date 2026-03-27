"""Koan /dead_code skill -- queue a dead code scan mission."""


def handle(ctx):
    """Handle /dead_code command -- queue a dead code scan mission.

    Usage:
        /dead_code              -- scan the default project
        /dead_code <project>    -- scan a specific project
        /dead_code --no-queue   -- scan without queuing follow-up missions
    """
    args = ctx.args.strip()

    if args in ("-h", "--help"):
        return (
            "Usage: /dead_code [project-name] [--no-queue]\n\n"
            "Scans a project for unused imports, functions, classes, "
            "variables, and dead branches.\n"
            "Produces a report saved to project memory.\n\n"
            "Options:\n"
            "  --no-queue  Don't auto-queue suggested removal missions\n\n"
            "Examples:\n"
            "  /dead_code koan\n"
            "  /dc webapp --no-queue"
        )

    # Parse --no-queue flag
    no_queue = "--no-queue" in args
    clean_args = args.replace("--no-queue", "").strip()

    # Determine project name
    project_name = clean_args.split()[0] if clean_args else None

    return _queue_dead_code(ctx, project_name, no_queue)


def _queue_dead_code(ctx, project_name, no_queue):
    """Queue a dead code scan mission."""
    from app.utils import insert_pending_mission, resolve_project_path

    if project_name:
        path = resolve_project_path(project_name)
        if not path:
            from app.utils import get_known_projects

            known = ", ".join(n for n, _ in get_known_projects()) or "none"
            return (
                f"\u274c Unknown project '{project_name}'.\n"
                f"Known projects: {known}"
            )
    else:
        # Use first known project as default
        from app.utils import get_known_projects

        projects = get_known_projects()
        if not projects:
            return "\u274c No projects configured."
        project_name = projects[0][0]

    suffix = " --no-queue" if no_queue else ""
    mission_entry = f"- [project:{project_name}] /dead_code{suffix}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    return f"\U0001f50d Dead code scan queued for {project_name}"
