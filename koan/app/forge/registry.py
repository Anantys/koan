"""Forge registry — maps forge type strings to ForgeProvider classes.

Usage:
    from app.forge.registry import get_forge_class, FORGE_TYPES

    cls = get_forge_class("github")   # -> GitHubForge
    cls = get_forge_class("gitlab")   # -> GitLabForge (future)
    cls = get_forge_class("gitea")    # -> GiteaForge (future)
"""

from typing import Dict, Type

from app.forge.base import ForgeProvider


def _build_registry() -> Dict[str, Type[ForgeProvider]]:
    """Build the forge type string → provider class registry.

    Imports are done lazily inside the function so that importing
    app.forge.registry does not eagerly load every forge implementation.
    """
    from app.forge.github import GitHubForge

    return {
        "github": GitHubForge,
        # "gitlab": GitLabForge,  # Phase 2a
        # "gitea": GiteaForge,    # Phase 2b
    }


# Public alias — build once at import time
FORGE_TYPES: Dict[str, Type[ForgeProvider]] = _build_registry()

#: Default forge type when none is configured
DEFAULT_FORGE = "github"


def get_forge_class(forge_type: str) -> Type[ForgeProvider]:
    """Return the ForgeProvider class for the given forge type string.

    Args:
        forge_type: One of the supported forge type strings (e.g. ``"github"``).

    Returns:
        The corresponding ForgeProvider subclass.

    Raises:
        ValueError: If forge_type is not a known forge type.
    """
    cls = FORGE_TYPES.get(forge_type)
    if cls is None:
        known = ", ".join(sorted(FORGE_TYPES))
        raise ValueError(
            f"Unknown forge type: {forge_type!r}. "
            f"Supported types: {known}"
        )
    return cls
