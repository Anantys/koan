"""GitHub @mention intent classifier using Claude.

When natural_language mode is enabled, this module classifies free-form
@mention text into a recognized bot command using a lightweight Claude call.

Only used as a fallback when the rigid command parser fails to match.
"""

import json
import logging
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


def classify_intent(
    message: str,
    commands: List[Tuple[str, str]],
    project_path: str,
) -> Optional[dict]:
    """Classify a natural-language @mention into a bot command.

    Args:
        message: The raw comment text (after @mention, code blocks stripped).
        commands: List of (command_name, description) tuples for available
            github-enabled commands.
        project_path: Path to the project directory (for Claude CLI).

    Returns:
        Dict with "command" (str or None) and "context" (str) keys,
        or None if classification failed (CLI error, timeout, etc.).
    """
    if not message or not message.strip():
        return None

    if not commands:
        return None

    from app.cli_provider import run_command
    from app.prompts import load_prompt

    # Build the commands list for the prompt
    commands_text = "\n".join(
        f"- `{name}` — {desc}" for name, desc in commands
    )

    # Load and fill the prompt template
    prompt_template = load_prompt("github-intent")
    if not prompt_template:
        log.warning("GitHub intent: could not load github-intent.md prompt")
        return None

    prompt = prompt_template.replace("{COMMANDS}", commands_text)
    prompt = prompt.replace("{MESSAGE}", message.strip())

    try:
        output = run_command(
            prompt=prompt,
            project_path=project_path,
            allowed_tools=[],
            model_key="lightweight",
            max_turns=1,
            timeout=30,
        )
    except (RuntimeError, OSError) as e:
        log.warning("GitHub intent: Claude CLI failed: %s", e)
        return None

    return _parse_classification(output)


def _parse_classification(output: str) -> Optional[dict]:
    """Parse the JSON classification from Claude's output.

    Handles various output formats: raw JSON, JSON in code blocks,
    or JSON embedded in explanatory text.

    Returns:
        Dict with "command" and "context" keys, or None on parse failure.
    """
    if not output or not output.strip():
        return None

    text = output.strip()

    # Try to extract JSON from code block first
    import re
    code_block = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if code_block:
        text = code_block.group(1).strip()

    # Try to find a JSON object in the text
    brace_start = text.find('{')
    brace_end = text.rfind('}')
    if brace_start >= 0 and brace_end > brace_start:
        text = text[brace_start:brace_end + 1]

    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        log.debug("GitHub intent: failed to parse JSON: %s", text[:200])
        return None

    if not isinstance(result, dict):
        return None

    # Normalize the result
    command = result.get("command")
    context = str(result.get("context", "")).strip()

    # command must be a string or None
    if command is not None:
        command = str(command).strip().lstrip("/")
        if not command:
            command = None

    return {"command": command, "context": context}
