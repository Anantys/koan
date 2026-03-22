"""Kōan decompose skill — queue a mission tagged for LLM decomposition."""


def handle(ctx):
    """Handle /decompose <mission text> command.

    Queues the mission with a [decompose] tag so that iteration_manager
    will call the LLM classifier before executing it. If the mission is
    composite, it will be split into sub-tasks; if atomic, it runs as-is.
    """
    from app.utils import (
        parse_project as _parse_project,
        detect_project_from_text,
        insert_pending_mission,
        get_known_projects,
    )
    from app.missions import extract_now_flag

    raw_args = ctx.args.strip()
    if not raw_args:
        return (
            "Usage: /decompose <mission description>\n\n"
            "Queues a mission tagged [decompose]. Before execution, the agent\n"
            "will classify it: atomic missions run as-is, composite missions are\n"
            "split into focused sub-tasks.\n\n"
            "Examples:\n"
            "  /decompose refactor the auth module\n"
            "  /decompose [project:koan] add retry logic to all API calls\n"
            "  /decompose koan implement the new dashboard feature"
        )

    # Check for --now flag
    urgent, raw_args = extract_now_flag(raw_args)

    # Check for explicit [project:name] tag
    project, mission_text = _parse_project(raw_args)

    # Auto-detect project from first word
    if not project:
        project, detected_text = detect_project_from_text(raw_args)
        if project:
            mission_text = detected_text

    if not project:
        known = get_known_projects()
        if len(known) > 1 and not urgent:
            project_list = "\n".join(f"  - {name}" for name, _path in known)
            first_name = known[0][0]
            return (
                f"Which project for this mission?\n\n"
                f"{project_list}\n\n"
                f"Reply with the tag, e.g.:\n"
                f"  /decompose [project:{first_name}] {raw_args[:80]}"
            )

    # Strip any existing [decompose] tag to avoid duplication
    import re
    mission_text = re.sub(r'\s*\[decompose\]\s*', ' ', mission_text, flags=re.IGNORECASE).strip()

    # Build mission entry with [decompose] tag
    if project:
        mission_entry = f"- [project:{project}] [decompose] {mission_text}"
    else:
        mission_entry = f"- [decompose] {mission_text}"

    missions_file = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_file, mission_entry, urgent=urgent)

    ack = "✅ Mission queued for decomposition"
    if urgent:
        ack += " (priority)"
    if project:
        ack += f" (project: {project})"
    ack += (
        f":\n\n{mission_text[:500]}\n\n"
        "The agent will classify this mission before execution. "
        "If it is complex, it will be automatically split into sub-tasks."
    )
    return ack
