"""Koan /profile skill -- queue a performance profiling mission."""

from app.github_skill_helpers import extract_github_url, handle_github_skill
from app.github_url_parser import parse_github_url


def handle(ctx):
    """Handle /profile command -- queue a profiling mission.

    Usage:
        /profile <project-name>
        /profile https://github.com/owner/repo/pull/123
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage: /profile <project-name-or-pr-url>\n"
            "Ex: /profile koan\n"
            "Ex: /profile https://github.com/sukria/koan/pull/42\n\n"
            "Queues a performance profiling mission."
        )

    # Try GitHub URL first
    result = extract_github_url(args, url_type="pr")
    if result:
        return handle_github_skill(
            ctx,
            command="profile",
            url_type="pr",
            parse_func=parse_github_url,
            success_prefix="Profile queued",
        )

    # Treat as project name
    project_name = args.split()[0]
    return _queue_project_profile(ctx, project_name)


def _queue_project_profile(ctx, project_name):
    """Queue a profile mission for a named project."""
    from app.utils import insert_pending_mission, resolve_project_path

    project_path = resolve_project_path(project_name)
    if not project_path:
        from app.utils import get_known_projects

        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return (
            f"\u274c Unknown project '{project_name}'.\n"
            f"Known projects: {known}"
        )

    mission_entry = f"- [project:{project_name}] /profile"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    return f"Profile queued for {project_name}"
