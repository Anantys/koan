"""Forge abstraction package.

Public API:
    get_forge(project_name) -> ForgeProvider
        Returns the appropriate ForgeProvider for the given project.
        Reads forge type from projects.yaml; defaults to GitHubForge.

    detect_forge_from_url(url) -> ForgeProvider
        Infers forge type from a URL domain.  Useful when parsing user-
        supplied URLs for projects that are not in projects.yaml.

Re-exports:
    ForgeProvider   — base class
    GitHubForge     — GitHub implementation
"""

import os
from typing import Optional

from app.forge.base import ForgeProvider
from app.forge.github import GitHubForge
from app.forge.registry import DEFAULT_FORGE, get_forge_class

__all__ = [
    "ForgeProvider",
    "GitHubForge",
    "get_forge",
    "detect_forge_from_url",
]


def get_forge(project_name: Optional[str] = None) -> ForgeProvider:
    """Return the ForgeProvider for the given project.

    Resolution order:
    1. ``forge`` field in projects.yaml for the project.
    2. Auto-detect from ``forge_url`` / ``github_url`` domain (Phase 4).
    3. Default: ``GitHubForge``.

    This function always succeeds — unknown or misconfigured projects fall
    back to ``GitHubForge`` so that all existing callers continue to work
    without any changes.

    Args:
        project_name: Project name as configured in projects.yaml.
            If None or not found, uses the default forge.

    Returns:
        An instantiated ForgeProvider.
    """
    forge_type, forge_url = _resolve_forge_config(project_name)
    cls = get_forge_class(forge_type)
    if forge_url:
        return cls(base_url=forge_url)
    return cls()


def _resolve_forge_config(project_name: Optional[str]) -> tuple:
    """Read forge type and URL from projects.yaml for a project.

    Returns:
        Tuple of (forge_type: str, forge_url: str).
        forge_type defaults to DEFAULT_FORGE; forge_url may be empty.
    """
    if not project_name:
        return DEFAULT_FORGE, ""

    try:
        koan_root = os.environ.get("KOAN_ROOT", "")
        if not koan_root:
            return DEFAULT_FORGE, ""

        from app.projects_config import get_project_config, load_projects_config

        config = load_projects_config(koan_root)
        if not config:
            return DEFAULT_FORGE, ""

        project_cfg = get_project_config(config, project_name)
        forge_type = str(project_cfg.get("forge", DEFAULT_FORGE)).strip().lower()
        # forge_url takes priority; fall back to github_url for backward compat
        forge_url = str(
            project_cfg.get("forge_url", "") or project_cfg.get("github_url", "")
        ).strip()

        if forge_type not in _known_forge_types():
            forge_type = DEFAULT_FORGE

        return forge_type, forge_url

    except Exception:  # noqa: BLE001 — never crash callers on config errors
        return DEFAULT_FORGE, ""


def _known_forge_types() -> set:
    from app.forge.registry import FORGE_TYPES
    return set(FORGE_TYPES.keys())


def detect_forge_from_url(url: str) -> ForgeProvider:
    """Infer a ForgeProvider from a URL domain.

    Used when a user pastes a PR/issue URL for a project that is not in
    projects.yaml, so there is no project config to consult.

    Currently recognised domains:
    - github.com → GitHubForge
    - gitlab.com → GitLabForge (Phase 2a, falls back to GitHubForge for now)
    - codeberg.org → GiteaForge (Phase 2b, falls back to GitHubForge for now)
    - gitea.io → GiteaForge (Phase 2b, falls back to GitHubForge for now)

    Unknown domains default to GitHubForge so existing code never breaks.

    Args:
        url: Full URL string.

    Returns:
        An instantiated ForgeProvider.
    """
    lower = url.lower()

    if "gitlab.com" in lower:
        # Phase 2a: return GitLabForge() once implemented
        return GitHubForge()

    if "codeberg.org" in lower or "gitea.io" in lower:
        # Phase 2b: return GiteaForge() once implemented
        return GitHubForge()

    # Default: GitHub (covers github.com and GitHub Enterprise with custom domains)
    return GitHubForge()
