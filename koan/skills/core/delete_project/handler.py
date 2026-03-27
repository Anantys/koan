"""Kōan delete_project skill — remove a project from the workspace.

Usage: /delete_project <project-name>

Removes workspace/<name> directory and its projects.yaml entry if present.
"""

import shutil
from pathlib import Path


def handle(ctx):
    """Handle /delete_project command."""
    project_name = ctx.args.strip()
    if not project_name:
        return (
            "Usage: /delete_project <project-name>\n\n"
            "Examples:\n"
            "  /delete_project myrepo\n"
            "  /del myrepo"
        )

    # Take only the first token as project name
    project_name = project_name.split()[0]

    koan_root = str(ctx.koan_root)
    workspace_dir = Path(koan_root) / "workspace"
    project_dir = workspace_dir / project_name

    # Validate: project must exist in workspace
    if not project_dir.exists():
        return f"Project '{project_name}' not found in workspace/"

    # Remove the workspace directory
    ctx.send_message(f"Removing workspace/{project_name}...")
    try:
        shutil.rmtree(str(project_dir))
    except OSError as e:
        return f"Failed to remove workspace/{project_name}: {e}"

    # Remove from projects.yaml if present
    removed_from_config = _remove_from_projects_yaml(koan_root, project_name)

    # Refresh project cache
    try:
        from app.projects_merged import refresh_projects
        refresh_projects(koan_root)
    except Exception:
        pass

    # Build result message
    lines = [f"Project '{project_name}' removed from workspace."]
    if removed_from_config:
        lines.append("  Entry removed from projects.yaml")
    lines.append(f"  Deleted: {project_dir}")
    return "\n".join(lines)


def _remove_from_projects_yaml(koan_root, project_name):
    """Remove a project entry from projects.yaml if it exists.

    Returns True if an entry was removed, False otherwise.
    """
    from app.projects_config import load_projects_config, save_projects_config

    config = load_projects_config(koan_root)
    if not config:
        return False

    projects = config.get("projects", {})
    if project_name not in projects:
        return False

    del projects[project_name]
    config["projects"] = projects
    save_projects_config(koan_root, config)
    return True
