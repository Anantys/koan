"""
Kōan — Project paths sanity checker.

Validates that all configured project paths in projects.yaml exist and
are accessible git repositories.  Runs at startup to catch stale entries
before the agent wastes a run cycle on an invalid project.

Note: validate_project_paths() exists in projects_config.py but was
never wired into the startup checks. This module fills that gap via
the sanity runner.
"""

import os
import subprocess
from typing import List, Tuple


def _is_git_repo(path: str) -> bool:
    """Check if a directory is a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-dir"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(instance_dir: str) -> Tuple[bool, List[str]]:
    """Validate project paths from projects.yaml.

    Returns (was_modified, list_of_warnings). Never modifies files —
    only reports issues so the human can fix projects.yaml.
    """
    warnings: List[str] = []

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return False, []

    try:
        from app.projects_config import load_projects_config
        config = load_projects_config(koan_root)
    except Exception:
        return False, []

    if not config:
        return False, []

    projects = config.get("projects", {})
    if not projects:
        return False, []

    for name, project in projects.items():
        if project is None:
            continue
        path = project.get("path", "")
        if not path:
            continue  # Workspace-only override, no path to validate

        if not os.path.isdir(path):
            warnings.append(
                f"Project '{name}' path does not exist: {path}"
            )
            continue

        if not _is_git_repo(path):
            warnings.append(
                f"Project '{name}' path is not a git repository: {path}"
            )

    return False, warnings
