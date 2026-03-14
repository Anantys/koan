"""Skill handler for /scaffold_skill — generate SKILL.md + handler.py from a description."""

import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from app.bridge_log import log

# Skill name validation: alphanumeric + underscores, must start with a letter/digit.
# Hyphens are NOT allowed — they break Telegram command parsing.
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_]*$")

# Max description length for single-turn generation
_MAX_DESCRIPTION_LENGTH = 500


def handle(ctx) -> Optional[str]:
    """Generate a new skill from a natural language description."""
    args = ctx.args.strip() if ctx.args else ""
    if not args:
        return (
            "Usage: /scaffold_skill <scope> <name> <description>\n\n"
            "Example: /scaffold_skill myteam deploy Deploy to production with rollback support"
        )

    scope, name, description, err = _parse_args(args)
    if err:
        return err

    # Validate scope (reject "core" and invalid names)
    from app.skill_manager import validate_scope

    scope_err = validate_scope(scope)
    if scope_err:
        return scope_err

    # Validate skill name
    if not _SKILL_NAME_RE.match(name):
        return f"Invalid skill name '{name}'. Use letters, numbers, and underscores (no hyphens)."

    # Check for command name conflicts with existing skills
    conflict = _check_command_conflict(name, ctx.instance_dir)
    if conflict:
        return conflict

    # Check target directory doesn't already exist
    target_dir = ctx.instance_dir / "skills" / scope / name
    if target_dir.exists():
        return f"Skill directory already exists: instance/skills/{scope}/{name}/"

    # Truncate very long descriptions
    if len(description) > _MAX_DESCRIPTION_LENGTH:
        description = description[:_MAX_DESCRIPTION_LENGTH] + "..."

    if ctx.send_message:
        ctx.send_message(f"Scaffolding skill '{name}' in scope '{scope}'...")

    # Load the skills README for reference
    skills_readme = _load_skills_readme()

    # Gather example skills for the prompt
    example_skills = _gather_example_skills()

    # Load and fill the scaffold prompt
    from app.prompts import load_skill_prompt

    prompt = load_skill_prompt(
        Path(__file__).parent,
        "scaffold",
        SCOPE=scope,
        SKILL_NAME=name,
        DESCRIPTION=description,
        SKILLS_README=skills_readme,
        EXAMPLE_SKILLS=example_skills,
    )

    # Invoke Claude CLI
    skill_md_content, handler_content, err = _invoke_claude(prompt)
    if err:
        return err

    # Validate the generated SKILL.md
    validation_err = _validate_skill_md(skill_md_content)
    if validation_err:
        return validation_err

    # Write files to disk
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "SKILL.md").write_text(skill_md_content)
    if handler_content:
        (target_dir / "handler.py").write_text(handler_content)

    # Build response
    has_handler = " + handler.py" if handler_content else ""
    return (
        f"Skill scaffolded: instance/skills/{scope}/{name}/\n\n"
        f"Files: SKILL.md{has_handler}\n\n"
        f"Restart the bridge to load the new skill."
    )


def _parse_args(args: str) -> Tuple[str, str, str, Optional[str]]:
    """Parse scope, name, and description from args.

    Returns (scope, name, description, error).
    """
    parts = args.split(None, 2)
    if len(parts) < 3:
        return "", "", "", (
            "Not enough arguments.\n\n"
            "Usage: /scaffold_skill <scope> <name> <description>"
        )
    return parts[0], parts[1], parts[2], None


def _check_command_conflict(name: str, instance_dir: Path) -> Optional[str]:
    """Check if the skill name conflicts with existing commands."""
    from app.skills import build_registry

    extra_dirs = []
    instance_skills = instance_dir / "skills"
    if instance_skills.is_dir():
        extra_dirs.append(instance_skills)

    registry = build_registry(extra_dirs=extra_dirs)
    existing = registry.find_by_command(name)
    if existing:
        return (
            f"Command '{name}' already exists in skill "
            f"'{existing.qualified_name}'. Choose a different name."
        )
    return None


def _load_skills_readme() -> str:
    """Load the skills README for prompt context."""
    readme_path = Path(__file__).parent.parent.parent / "README.md"
    try:
        return readme_path.read_text()
    except OSError:
        return "(Skills README not available)"


def _gather_example_skills() -> str:
    """Gather 2-3 example skills of varying complexity for the prompt."""
    skills_dir = Path(__file__).parent.parent

    examples = []

    # Example 1: Simple prompt-only skill (idea)
    idea_path = skills_dir / "idea" / "SKILL.md"
    if idea_path.exists():
        examples.append(f"### Example: prompt-only skill (idea)\n```\n{idea_path.read_text().strip()}\n```")

    # Example 2: Simple handler skill (chat)
    chat_skill = skills_dir / "chat" / "SKILL.md"
    chat_handler = skills_dir / "chat" / "handler.py"
    if chat_skill.exists() and chat_handler.exists():
        examples.append(
            f"### Example: simple handler skill (chat)\n"
            f"SKILL.md:\n```\n{chat_skill.read_text().strip()}\n```\n\n"
            f"handler.py:\n```python\n{chat_handler.read_text().strip()}\n```"
        )

    # Example 3: Worker handler skill (magic)
    magic_skill = skills_dir / "magic" / "SKILL.md"
    magic_handler = skills_dir / "magic" / "handler.py"
    if magic_skill.exists() and magic_handler.exists():
        examples.append(
            f"### Example: worker handler skill (magic)\n"
            f"SKILL.md:\n```\n{magic_skill.read_text().strip()}\n```\n\n"
            f"handler.py:\n```python\n{magic_handler.read_text().strip()}\n```"
        )

    return "\n\n".join(examples) if examples else "(No examples available)"


def _invoke_claude(prompt: str) -> Tuple[str, str, Optional[str]]:
    """Invoke Claude CLI to generate skill files.

    Returns (skill_md_content, handler_content, error).
    handler_content may be empty if Claude generates a prompt-only skill.
    """
    from app.cli_provider import build_full_command
    from app.cli_exec import run_cli
    from app.config import get_fast_reply_model

    fast_model = get_fast_reply_model()
    cmd = build_full_command(
        prompt=prompt,
        max_turns=1,
        model=fast_model or "",
    )

    try:
        result = run_cli(
            cmd,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0 or not result.stdout.strip():
            if result.stderr:
                log("error", f"Scaffold Claude stderr: {result.stderr[:500]}")
            return "", "", "Failed to generate skill files. Try again with a more specific description."

        return _parse_claude_response(result.stdout.strip())
    except subprocess.TimeoutExpired:
        return "", "", "Timeout generating skill. Try again."
    except Exception as e:
        log("error", f"Scaffold error: {e}")
        return "", "", f"Error generating skill: {e}"


def _parse_claude_response(response: str) -> Tuple[str, str, Optional[str]]:
    """Parse Claude's response to extract SKILL.md and handler.py content.

    Expects fenced code blocks labeled with filenames.
    Returns (skill_md_content, handler_content, error).
    """
    # Match code blocks with filename labels
    # Patterns: ```SKILL.md, ```yaml SKILL.md, ```markdown SKILL.md, etc.
    skill_md = _extract_code_block(response, "SKILL.md")
    if not skill_md:
        # Try without filename — look for frontmatter pattern
        skill_md = _extract_frontmatter_block(response)

    if not skill_md:
        return "", "", (
            "Could not parse generated SKILL.md from Claude's response. "
            "Try again with a clearer description."
        )

    handler = _extract_code_block(response, "handler.py")

    return skill_md, handler or "", None


def _extract_code_block(text: str, filename: str) -> str:
    """Extract content of a fenced code block labeled with a filename."""
    # Match patterns like:
    # ```SKILL.md or ```yaml SKILL.md or **SKILL.md** followed by ```
    # Also: ### SKILL.md\n```
    patterns = [
        # ``` followed by optional language then filename
        re.compile(
            r"```(?:\w+\s+)?" + re.escape(filename) + r"\s*\n(.*?)```",
            re.DOTALL,
        ),
        # filename as header/bold then code block
        re.compile(
            r"(?:#{1,4}\s+|[*_]{1,2})" + re.escape(filename) + r"[*_]{0,2}\s*\n+```\w*\s*\n(.*?)```",
            re.DOTALL,
        ),
        # Just the filename on a line before a code block
        re.compile(
            re.escape(filename) + r"\s*:?\s*\n+```\w*\s*\n(.*?)```",
            re.DOTALL,
        ),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip() + "\n"
    return ""


def _extract_frontmatter_block(text: str) -> str:
    """Extract a YAML frontmatter block (---...---) from text as fallback."""
    match = re.search(r"```\w*\s*\n(---\n.*?\n---(?:\n.*?)?)\n*```", text, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return ""


def _validate_skill_md(content: str) -> Optional[str]:
    """Validate generated SKILL.md content via the existing parser.

    Returns error message or None if valid.
    """
    import tempfile

    from app.skills import parse_skill_md

    # Write to temp file for parse_skill_md
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="scaffold-validate-", delete=False
    ) as f:
        f.write(content)
        tmp_path = Path(f.name)

    try:
        skill = parse_skill_md(tmp_path)
        if skill is None:
            return (
                "Generated SKILL.md failed validation. "
                "Try again with a more specific description."
            )
        if not skill.commands:
            return (
                "Generated SKILL.md has no commands defined. "
                "Try again with a clearer description."
            )
        return None
    finally:
        tmp_path.unlink(missing_ok=True)
