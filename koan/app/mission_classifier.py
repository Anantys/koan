"""Kōan — Mission type classifier.

Lightweight keyword-based heuristic to classify mission titles into work types.
Used by prompt_builder to inject type-specific guidance into the agent prompt.

Pure logic — no file I/O, no side effects.
"""

import re


# Ordered by specificity: most specific types first.
# "fix the implementation" → debug (not implement).
_TYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "debug",
        re.compile(
            r"\b(?:fix|bug|debug|broken|crash|fail|error|regress|corriger|réparer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review",
        re.compile(
            r"\b(?:review|audit|check|inspect|analys[ez]|verify|auditer|vérifier)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "refactor",
        re.compile(
            r"\b(?:refactor|clean\s*up|simplif\w*|extract|split|reorgani[sz]e|refactoriser)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "design",
        re.compile(
            r"\b(?:design|architect|plan|rfc|spec|proposal|concevoir)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "implement",
        re.compile(
            r"\b(?:implement|add|create|build|feature|new|write|ajouter|créer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "docs",
        re.compile(
            r"\b(?:doc(?:ument)?|readme|comment|explain|documenter)\b",
            re.IGNORECASE,
        ),
    ),
]


def classify_mission(title: str) -> str:
    """Classify a mission title into a work type.

    Returns one of: "debug", "implement", "design", "review",
    "refactor", "docs", "general".

    Only the first line is considered for multi-line titles.
    """
    if not title or not title.strip():
        return "general"

    # Use first line only, strip markdown list prefix and project tags
    line = title.split("\n")[0].strip()
    if line.startswith("- "):
        line = line[2:]
    line = re.sub(r"\[projec?t:[a-zA-Z0-9_-]+\]\s*", "", line)

    if not line.strip():
        return "general"

    for mission_type, pattern in _TYPE_PATTERNS:
        if pattern.search(line):
            return mission_type

    return "general"
