"""Kōan — Claude Code plugin directory generator.

Converts Kōan skills tagged with audience=agent/command/hybrid into a
Claude Code plugin directory that can be loaded via ``--plugin-dir``.

Generated layout::

    <tmpdir>/
    ├── .claude-plugin/
    │   └── plugin.json        # Auto-generated manifest
    ├── skills/                 # Skills with audience=agent or hybrid
    │   └── <name>/
    │       └── SKILL.md
    └── commands/               # Skills with audience=command
        └── <name>.md

Lifecycle:
    - ``generate_plugin_dir()`` creates a temp directory per session
    - ``cleanup_plugin_dir()`` removes it after the session ends
"""

import json
import tempfile
from pathlib import Path
from typing import List, Optional

from app.skills import Skill, SkillRegistry

# Plugin manifest version (follows Claude Code plugin spec)
_PLUGIN_VERSION = "1.0.0"
_PLUGIN_NAME = "koan-skills"
_PLUGIN_DESCRIPTION = "Kōan agent skills — auto-generated plugin"


def _render_command_md(skill: Skill) -> str:
    """Render a Kōan skill as a Claude Code command .md file.

    Claude Code commands use YAML frontmatter with ``description``,
    ``allowed-tools``, and the prompt body below.
    """
    lines = ["---"]
    lines.append(f"description: {skill.description}")
    lines.append("allowed-tools: [Read, Glob, Grep, Bash, Edit, Write]")
    lines.append("---")
    lines.append("")

    # Use the skill's prompt body if available
    if skill.prompt_body:
        lines.append(skill.prompt_body)
    else:
        lines.append(f"# {skill.name}")
        lines.append("")
        lines.append(skill.description)

    return "\n".join(lines)


def _render_skill_md(skill: Skill) -> str:
    """Render a Kōan skill as a Claude Code plugin skill SKILL.md.

    Claude Code plugin skills use YAML frontmatter with ``name`` and
    ``description``. The body provides contextual knowledge that Claude
    uses for auto-triggering.
    """
    lines = ["---"]
    lines.append(f"name: {skill.name}")
    lines.append(f"description: {skill.description}")
    lines.append("---")
    lines.append("")

    if skill.prompt_body:
        lines.append(skill.prompt_body)
    else:
        lines.append(f"# {skill.name}")
        lines.append("")
        lines.append(skill.description)

    return "\n".join(lines)


def _generate_manifest() -> str:
    """Generate the plugin.json manifest."""
    manifest = {
        "name": _PLUGIN_NAME,
        "version": _PLUGIN_VERSION,
        "description": _PLUGIN_DESCRIPTION,
    }
    return json.dumps(manifest, indent=2) + "\n"


def _select_skills(
    registry: SkillRegistry,
    include_audiences: Optional[List[str]] = None,
) -> List[Skill]:
    """Select skills eligible for plugin generation.

    Args:
        registry: Skill registry to select from.
        include_audiences: Audience types to include. Defaults to
            agent, command, and hybrid.

    Returns:
        List of skills to include in the plugin.
    """
    if include_audiences is None:
        include_audiences = ["agent", "command", "hybrid"]
    return registry.list_by_audience(*include_audiences)


def generate_plugin_dir(
    registry: SkillRegistry,
    include_audiences: Optional[List[str]] = None,
    base_dir: Optional[Path] = None,
) -> Path:
    """Generate a Claude Code plugin directory from Kōan skills.

    Creates a temporary directory with the plugin structure that Claude
    Code can load via ``--plugin-dir``.

    Args:
        registry: Skill registry to pull skills from.
        include_audiences: Audience types to include. Defaults to
            ["agent", "command", "hybrid"].
        base_dir: Parent directory for the temp dir. Defaults to
            system temp.

    Returns:
        Path to the generated plugin directory.
    """
    skills = _select_skills(registry, include_audiences)

    # Create temp directory
    kwargs = {}
    if base_dir is not None:
        kwargs["dir"] = str(base_dir)
    plugin_dir = Path(tempfile.mkdtemp(prefix="koan-plugins-", **kwargs))

    # Create plugin manifest
    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text(_generate_manifest())

    # Create skills/ and commands/ directories
    skills_dir = plugin_dir / "skills"
    commands_dir = plugin_dir / "commands"

    for skill in skills:
        if skill.audience in ("agent", "hybrid"):
            # Agent/hybrid skills go to skills/<name>/SKILL.md
            skill_out = skills_dir / skill.name
            skill_out.mkdir(parents=True, exist_ok=True)
            (skill_out / "SKILL.md").write_text(_render_skill_md(skill))

        if skill.audience in ("command", "hybrid"):
            # Command/hybrid skills go to commands/<name>.md
            commands_dir.mkdir(parents=True, exist_ok=True)
            (commands_dir / f"{skill.name}.md").write_text(
                _render_command_md(skill)
            )

    return plugin_dir


def cleanup_plugin_dir(plugin_dir: Path) -> bool:
    """Remove a generated plugin directory.

    Args:
        plugin_dir: Path returned by ``generate_plugin_dir()``.

    Returns:
        True if cleanup succeeded, False on error.
    """
    import shutil

    try:
        if plugin_dir.exists() and plugin_dir.is_dir():
            shutil.rmtree(plugin_dir)
            return True
    except OSError:
        pass
    return False
