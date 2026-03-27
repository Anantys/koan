"""Mission complexity detection for spec-first execution.

Determines whether a mission is complex enough to warrant generating a
spec document before implementation. Uses a dual-heuristic gate:
keyword match AND description length threshold (same pattern as
post_mission_reflection.is_significant_mission).

Simple missions ("fix typo", "/plan something") skip spec generation.
"""

import re

# Keywords indicating complex missions that benefit from a spec
COMPLEXITY_KEYWORDS = [
    "feature",
    "refactor",
    "migration",
    "architecture",
    "redesign",
    "overhaul",
    "implement",
    "system",
    "pipeline",
    "framework",
    "integration",
]

# Default minimum description length (after stripping project prefix)
DEFAULT_COMPLEXITY_THRESHOLD = 80

# Pattern to strip [project:name] tags
_PROJECT_TAG_RE = re.compile(r"^\[project:\w+\]\s*")


def _get_complexity_threshold() -> int:
    """Load complexity threshold from config, with default fallback."""
    try:
        from app.utils import load_config
        config = load_config()
        value = config.get("spec_complexity_threshold")
        if value is not None:
            return int(value)
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return DEFAULT_COMPLEXITY_THRESHOLD


def _strip_project_tag(title: str) -> str:
    """Strip [project:name] tag prefix from a mission title."""
    return _PROJECT_TAG_RE.sub("", title).strip()


def is_complex_mission(title: str) -> bool:
    """Determine if a mission is complex enough for spec generation.

    Dual heuristic:
    - Keyword match (feature, refactor, migration, etc.)
    - Description length >= threshold (default 80 chars)

    Returns False for:
    - Empty titles
    - Skill missions (starting with /)
    - Short descriptions (below threshold)
    - Titles without complexity keywords

    Args:
        title: The mission title text (may include [project:name] tag).

    Returns:
        True if the mission warrants a spec document.
    """
    if not title:
        return False

    stripped = _strip_project_tag(title)

    # Skill missions have their own flow
    if stripped.startswith("/"):
        return False

    # Check length threshold
    threshold = _get_complexity_threshold()
    if len(stripped) < threshold:
        return False

    # Check keywords
    lower = stripped.lower()
    return any(kw in lower for kw in COMPLEXITY_KEYWORDS)
