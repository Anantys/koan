"""Kōan — LLM-driven mission decomposition.

Classifies missions as atomic (single agent pass) or composite (needs splitting),
then generates ordered sub-task lists for composite missions.

Usage:
    from app.decompose import decompose_mission

    subtasks = decompose_mission(mission_text, project_path)
    if subtasks is None:
        # Atomic — run as-is
        ...
    else:
        # Composite — subtasks is a list of sub-mission strings
        ...

Architecture mirrors pr_review_learning.py:
- Load prompt from system-prompts/decompose-mission.md
- Build CLI command with lightweight model (haiku)
- Parse JSON from stdout
- Return None for atomic or error, list[str] for composite
"""

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Maximum number of sub-tasks to accept from the classifier
_MAX_SUBTASKS = 6


def decompose_mission(
    mission_text: str,
    project_path: str = "",
) -> Optional[List[str]]:
    """Classify a mission and return sub-tasks if composite.

    Makes a lightweight Claude call to classify the mission as atomic or
    composite. If composite, returns an ordered list of sub-task strings
    (up to _MAX_SUBTASKS). Returns None if atomic or on any error.

    Args:
        mission_text: Full mission text (with tags stripped or included).
        project_path: Path to the project repo (used as cwd for CLI).

    Returns:
        None if the mission is atomic (or on error).
        List of sub-task strings if composite (at least 1 item).
    """
    if not mission_text.strip():
        return None

    try:
        from app.cli_provider import build_full_command
        from app.config import get_model_config
        from app.prompts import load_prompt
    except ImportError as e:
        logger.error("Import error: %s", e)
        return None

    try:
        prompt = load_prompt("decompose-mission", MISSION_TEXT=mission_text.strip())
    except (FileNotFoundError, OSError) as e:
        logger.error("Prompt load error: %s", e)
        return None

    models = get_model_config()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=[],
        model=models.get("lightweight", "haiku"),
        fallback=models.get("fallback", "sonnet"),
        max_turns=1,
    )

    cwd = project_path if project_path else None

    try:
        from app.cli_exec import run_cli_with_retry
        result = run_cli_with_retry(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd,
        )
        if result.returncode != 0:
            logger.error("CLI call failed: %s", result.stderr[:200])
            return None
        output = result.stdout.strip()
    except Exception as e:
        logger.error("CLI error: %s", e)
        return None

    return _parse_decompose_output(output)


def _parse_decompose_output(output: str) -> Optional[List[str]]:
    """Parse JSON output from the decompose classifier.

    Handles both clean JSON and JSON embedded in surrounding text.
    Returns None for atomic classification or parse errors.
    """
    if not output:
        return None

    # Try to extract JSON object from output (Claude may wrap it in text).
    # Use a pattern that handles one level of nested braces (for the subtasks array),
    # avoiding greedy matching across unrelated brace groups.
    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', output)
    if not json_match:
        logger.warning("No JSON found in output: %s", output[:200])
        return None

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        logger.warning("JSON parse error: %s — output: %s", e, output[:200])
        return None

    classification = data.get("type", "atomic")
    if classification != "composite":
        return None

    subtasks = data.get("subtasks", [])
    if not isinstance(subtasks, list) or not subtasks:
        logger.warning("Composite with empty subtasks — treating as atomic")
        return None

    # Filter and cap sub-tasks
    valid = [str(t).strip() for t in subtasks if str(t).strip()]
    if not valid:
        return None

    if len(valid) > _MAX_SUBTASKS:
        logger.info("Capping sub-tasks from %d to %d", len(valid), _MAX_SUBTASKS)
        valid = valid[:_MAX_SUBTASKS]

    return valid


def should_decompose(mission_text: str) -> bool:
    """Check if a mission is tagged for decomposition.

    Returns True if the mission has a [decompose] tag.
    """
    return bool(re.search(r'\[decompose\]', mission_text, re.IGNORECASE))


def is_already_decomposed(mission_text: str) -> bool:
    """Check if a mission has already been decomposed or is a sub-task.

    Returns True if the mission has [decomposed:*] or [group:*] tags,
    which means it should not be decomposed again.
    """
    return bool(re.search(r'\[decomposed:[^\]]+\]|\[group:[^\]]+\]', mission_text, re.IGNORECASE))
