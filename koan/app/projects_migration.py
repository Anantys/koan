"""Automatic migration from env vars to projects.yaml.

Generates projects.yaml from KOAN_PROJECTS / KOAN_PROJECT_PATH env vars
when projects.yaml doesn't exist yet. Imports per-project git_auto_merge
overrides from config.yaml if present.

Migration is idempotent: if projects.yaml already exists, nothing happens.
"""

import os
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


def should_migrate(koan_root: str) -> bool:
    """Check if migration should run.

    Returns True if projects.yaml doesn't exist AND env vars are configured.
    """
    projects_yaml = Path(koan_root) / "projects.yaml"
    if projects_yaml.exists():
        return False

    return bool(
        os.environ.get("KOAN_PROJECTS", "").strip()
        or os.environ.get("KOAN_PROJECT_PATH", "").strip()
    )


def _parse_env_projects() -> List[Tuple[str, str]]:
    """Parse project list from env vars.

    Tries KOAN_PROJECTS first, then KOAN_PROJECT_PATH.
    Returns list of (name, path) tuples.
    """
    projects_str = os.environ.get("KOAN_PROJECTS", "").strip()
    if projects_str:
        result = []
        for pair in projects_str.split(";"):
            pair = pair.strip()
            if ":" in pair:
                name, path = pair.split(":", 1)
                name, path = name.strip(), path.strip()
                if name and path:
                    result.append((name, path))
        return result

    single_path = os.environ.get("KOAN_PROJECT_PATH", "").strip()
    if single_path:
        # Derive name from directory basename
        name = Path(single_path).name.lower().replace(" ", "-")
        return [(name, single_path)]

    return []


def _load_config_auto_merge(koan_root: str) -> dict:
    """Load per-project git_auto_merge overrides from config.yaml.

    Returns dict mapping project_name -> git_auto_merge config.
    """
    config_path = Path(koan_root) / "instance" / "config.yaml"
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}

    projects_section = config.get("projects", {})
    if not isinstance(projects_section, dict):
        return {}

    return {
        name: proj_config["git_auto_merge"]
        for name, proj_config in projects_section.items()
        if isinstance(proj_config, dict)
        and isinstance(proj_config.get("git_auto_merge"), dict)
    }


def build_projects_yaml(
    projects: List[Tuple[str, str]],
    auto_merge_overrides: Optional[dict] = None,
) -> str:
    """Build projects.yaml content from project list and optional overrides.

    Args:
        projects: List of (name, path) tuples.
        auto_merge_overrides: Dict mapping project_name -> git_auto_merge config.

    Returns:
        YAML string ready to write to projects.yaml.
    """
    auto_merge_overrides = auto_merge_overrides or {}

    data = {
        "defaults": {
            "git_auto_merge": {
                "enabled": False,
                "base_branch": "main",
                "strategy": "squash",
            }
        },
        "projects": {},
    }

    for name, path in sorted(projects, key=lambda x: x[0].lower()):
        entry = {"path": path}
        if name in auto_merge_overrides:
            entry["git_auto_merge"] = auto_merge_overrides[name]
        data["projects"][name] = entry

    header = (
        "# projects.yaml — Auto-generated from environment variables\n"
        "#\n"
        "# This file was created by Kōan's automatic migration.\n"
        "# You can now remove KOAN_PROJECTS / KOAN_PROJECT_PATH from .env.\n"
        "#\n"
        "# See projects.example.yaml for full documentation.\n\n"
    )
    return header + yaml.dump(data, default_flow_style=False, sort_keys=False)


def run_migration(koan_root: str) -> List[str]:
    """Run the env-to-projects.yaml migration.

    Returns list of log messages describing what was done.
    Empty list if nothing was migrated.
    """
    if not should_migrate(koan_root):
        return []

    messages = []

    projects = _parse_env_projects()
    if not projects:
        return []

    # Load auto-merge overrides from config.yaml
    overrides = _load_config_auto_merge(koan_root)

    # Build and write projects.yaml
    content = build_projects_yaml(projects, overrides)
    from app.utils import atomic_write
    projects_yaml_path = Path(koan_root) / "projects.yaml"
    atomic_write(projects_yaml_path, content)

    source = "KOAN_PROJECTS" if os.environ.get("KOAN_PROJECTS") else "KOAN_PROJECT_PATH"
    messages.append(
        f"Migrated {len(projects)} project(s) from {source} to projects.yaml"
    )

    if overrides:
        names = ", ".join(sorted(overrides.keys()))
        messages.append(
            f"Imported git_auto_merge overrides for: {names}"
        )

    messages.append(
        "You can now remove KOAN_PROJECTS / KOAN_PROJECT_PATH from .env"
    )

    return messages
