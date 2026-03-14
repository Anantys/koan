"""Koan /tech_debt skill -- queue a tech debt scan mission."""


def handle(ctx):
    """Handle /tech_debt command -- queue a tech debt scan mission.

    Usage:
        /tech_debt              -- scan the default project
        /tech_debt <project>    -- scan a specific project
        /tech_debt --no-queue   -- scan without queuing follow-up missions
    """
    args = ctx.args.strip()

    if args in ("-h", "--help"):
        return (
            "Usage: /tech_debt [project-name] [--no-queue]\n\n"
            "Scans a project for duplicated code, complex functions, "
            "testing gaps, and infrastructure issues.\n"
            "Produces a prioritized debt register saved to project learnings.\n\n"
            "Options:\n"
            "  --no-queue  Don't auto-queue suggested missions\n\n"
            "Examples:\n"
            "  /tech_debt koan\n"
            "  /td webapp --no-queue"
        )

    # Parse --no-queue flag
    no_queue = "--no-queue" in args
    clean_args = args.replace("--no-queue", "").strip()

    # Determine project name
    project_name = clean_args.split()[0] if clean_args else None

    return _queue_tech_debt(ctx, project_name, no_queue)


def _queue_tech_debt(ctx, project_name, no_queue):
    """Queue a tech debt scan mission."""
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
    mission_entry = f"- [project:{project_name}] /tech_debt{suffix}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    return f"\U0001f50d Tech debt scan queued for {project_name}"
