"""Gather system information for the startup banner.

Lightweight module that reads config files directly — no heavy imports
(bridge_state, awake) that trigger side effects.
"""

import os
from pathlib import Path


def _get_config_value(key: str, default: str) -> str:
    """Load a value from config.yaml with fallback to default.
    
    Args:
        key: The configuration key to retrieve.
        default: The fallback value if key is not found or config fails to load.
    
    Returns:
        The configuration value or default.
    """
    try:
        from app.utils import load_config
        config = load_config()
        return config.get(key, default)
    except (ImportError, OSError, ValueError):
        return default


def gather_startup_info(koan_root: Path) -> dict:
    """Collect system info for display in the startup banner.

    Returns a dict with keys like 'provider', 'projects', 'skills', etc.
    All values are strings ready for display. Missing values default to
    placeholder text rather than crashing.
    """
    instance = koan_root / "instance"
    info = {}

    # Provider
    info["provider"] = _get_provider(koan_root)

    # Projects
    info["projects"] = _get_projects_summary(koan_root)

    # Skills
    info["skills"] = _get_skills_summary(koan_root, instance)

    # Soul
    info["soul"] = _get_file_size(instance / "soul.md")

    # Messaging
    info["messaging"] = _get_messaging_provider()

    return info


def _get_provider(koan_root: Path) -> str:
    """Detect the CLI provider from env or config."""
    try:
        from app.utils import get_cli_provider_env
        provider = get_cli_provider_env()
    except (ImportError, OSError, ValueError):
        provider = ""
    if not provider:
        provider = _get_config_value("cli_provider", "claude")
    return provider


def _get_projects_summary(koan_root: Path) -> str:
    """Count configured projects."""
    try:
        from app.utils import get_known_projects
        projects = get_known_projects()
        count = len(projects)
        if count == 0:
            return "none configured"
        names = [p[0] for p in projects[:3]]
        suffix = f" +{count - 3} more" if count > 3 else ""
        return f"{count} ({', '.join(names)}{suffix})"
    except (ImportError, OSError, ValueError):
        return "unavailable"


def _get_skills_summary(koan_root: Path, instance: Path) -> str:
    """Count core and extra skills."""
    try:
        from app.skills import build_registry
        extra_dirs = []
        instance_skills = instance / "skills"
        if instance_skills.is_dir():
            extra_dirs.append(instance_skills)
        registry = build_registry(extra_dirs)
        core = len(registry.list_by_scope("core"))
        total = len(registry.list_all())
        extra = total - core
        if extra > 0:
            return f"{core} core + {extra} extra"
        return f"{core} core"
    except (ImportError, OSError, ValueError):
        return "unavailable"


def _get_file_size(path: Path) -> str:
    """Return human-readable file size."""
    try:
        if not path.exists():
            return "not found"
        size = len(path.read_text())
        if size >= 1000:
            return f"{size // 1000}k chars"
        return f"{size} chars"
    except OSError:
        return "unavailable"


def _get_messaging_provider() -> str:
    """Detect configured messaging provider."""
    provider = os.environ.get("KOAN_MESSAGING_PROVIDER", "").strip()
    if not provider:
        provider = _get_config_value("messaging_provider", "telegram")
    return provider
