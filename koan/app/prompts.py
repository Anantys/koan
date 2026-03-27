"""Kōan — System prompt loader.

Loads prompt templates from koan/system-prompts/ and substitutes placeholders.
"""

import subprocess
from pathlib import Path
from typing import Optional

PROMPT_DIR = Path(__file__).parent.parent / "system-prompts"


def get_prompt_path(name: str) -> Path:
    """Return the full path to a system prompt file.

    Args:
        name: Prompt file name without .md extension (e.g. "chat", "pick-mission")

    Returns:
        Path to the prompt file (e.g. koan/system-prompts/chat.md)
    """
    return PROMPT_DIR / f"{name}.md"


def _read_prompt_with_git_fallback(path: Path) -> str:
    """Read a prompt file, falling back to git if the file is missing on disk.

    When Kōan works on its own repo and a rebase or crash leaves the tree on a
    PR branch, prompt files added after that branch was created may be absent.
    This helper tries ``upstream/main`` then ``origin/main`` via ``git show``.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise FileNotFoundError(path)
        root = Path(result.stdout.strip())
        rel_path = path.relative_to(root)
    except (subprocess.TimeoutExpired, ValueError):
        raise FileNotFoundError(path)

    for remote in ("upstream/main", "origin/main"):
        try:
            result = subprocess.run(
                ["git", "show", f"{remote}:{rel_path}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout
        except subprocess.TimeoutExpired:
            continue

    raise FileNotFoundError(path)


def _substitute(template: str, kwargs: dict) -> str:
    """Replace {KEY} placeholders in a template string."""
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a system prompt template and substitute placeholders.

    Args:
        name: Prompt file name without .md extension (e.g. "chat", "format-message")
        **kwargs: Placeholder values to substitute. Keys map to {KEY} in the template.

    Returns:
        The prompt string with placeholders replaced.
    """
    template = _read_prompt_with_git_fallback(get_prompt_path(name))
    return _substitute(template, kwargs)


def load_skill_prompt(skill_dir: Path, name: str, **kwargs: str) -> str:
    """Load a prompt from a skill's prompts/ directory.

    Looks for ``skill_dir/prompts/<name>.md`` first, then falls back to
    the global ``system-prompts/`` directory for safe incremental migration.

    Args:
        skill_dir: Path to the skill directory (e.g. ``skills/core/plan``).
        name: Prompt file name without .md extension.
        **kwargs: Placeholder values to substitute. Keys map to {KEY} in the template.

    Returns:
        The prompt string with placeholders replaced.
    """
    skill_prompt = skill_dir / "prompts" / f"{name}.md"
    try:
        template = _read_prompt_with_git_fallback(skill_prompt)
    except FileNotFoundError:
        # Skill prompt not found even via git — fall back to system-prompts/
        template = _read_prompt_with_git_fallback(get_prompt_path(name))
    return _substitute(template, kwargs)


def load_prompt_or_skill(
    skill_dir: Optional[Path], name: str, **kwargs: str
) -> str:
    """Load a prompt, preferring the skill directory when available.

    Consolidates the repeated pattern::

        if skill_dir is not None:
            prompt = load_skill_prompt(skill_dir, name, **kw)
        else:
            prompt = load_prompt(name, **kw)

    Args:
        skill_dir: Path to the skill directory, or None for system prompts.
        name: Prompt file name without .md extension.
        **kwargs: Placeholder values to substitute.

    Returns:
        The prompt string with placeholders replaced.
    """
    if skill_dir is not None:
        return load_skill_prompt(skill_dir, name, **kwargs)
    return load_prompt(name, **kwargs)
