"""Tests for app/plugin_generator.py — Claude Code plugin directory generation."""

import json
import textwrap
from pathlib import Path

import pytest

from app.plugin_generator import (
    _generate_manifest,
    _render_command_md,
    _render_skill_md,
    _select_skills,
    cleanup_plugin_dir,
    generate_plugin_dir,
)
from app.skills import Skill, SkillCommand, SkillRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill(
    name="test",
    scope="core",
    description="A test skill",
    audience="agent",
    prompt_body="",
    worker=False,
) -> Skill:
    """Create a Skill instance for testing."""
    return Skill(
        name=name,
        scope=scope,
        description=description,
        audience=audience,
        prompt_body=prompt_body,
        worker=worker,
        commands=[SkillCommand(name=name, description=description)],
    )


def _make_registry(tmp_path, skills_data):
    """Create a registry from a list of (name, audience, prompt_body) tuples."""
    for name, audience, prompt_body in skills_data:
        skill_dir = tmp_path / "core" / name
        skill_dir.mkdir(parents=True)
        frontmatter = textwrap.dedent(f"""\
            ---
            name: {name}
            scope: core
            description: {name} skill
            audience: {audience}
            commands:
              - name: {name}
                description: {name} command
            ---
        """)
        if prompt_body:
            frontmatter += f"\n{prompt_body}\n"
        (skill_dir / "SKILL.md").write_text(frontmatter)
    return SkillRegistry(tmp_path)


# ---------------------------------------------------------------------------
# _render_command_md
# ---------------------------------------------------------------------------

class TestRenderCommandMd:
    def test_with_prompt_body(self):
        skill = _make_skill(prompt_body="Review the code carefully.")
        result = _render_command_md(skill)
        assert "---" in result
        assert "description: A test skill" in result
        assert "Review the code carefully." in result

    def test_without_prompt_body(self):
        skill = _make_skill(prompt_body="")
        result = _render_command_md(skill)
        assert "# test" in result
        assert "A test skill" in result

    def test_includes_allowed_tools(self):
        skill = _make_skill()
        result = _render_command_md(skill)
        assert "allowed-tools:" in result

    def test_frontmatter_structure(self):
        skill = _make_skill(description="Do something useful")
        result = _render_command_md(skill)
        lines = result.split("\n")
        assert lines[0] == "---"
        assert "description: Do something useful" in lines[1]
        # Find closing ---
        assert "---" in lines[3]


# ---------------------------------------------------------------------------
# _render_skill_md
# ---------------------------------------------------------------------------

class TestRenderSkillMd:
    def test_with_prompt_body(self):
        skill = _make_skill(prompt_body="Quality rules for code.")
        result = _render_skill_md(skill)
        assert "name: test" in result
        assert "description: A test skill" in result
        assert "Quality rules for code." in result

    def test_without_prompt_body(self):
        skill = _make_skill()
        result = _render_skill_md(skill)
        assert "# test" in result
        assert "A test skill" in result

    def test_frontmatter_format(self):
        skill = _make_skill(name="review", description="Code review")
        result = _render_skill_md(skill)
        assert result.startswith("---\n")
        assert "name: review" in result
        assert "description: Code review" in result


# ---------------------------------------------------------------------------
# _generate_manifest
# ---------------------------------------------------------------------------

class TestGenerateManifest:
    def test_valid_json(self):
        manifest = _generate_manifest()
        data = json.loads(manifest)
        assert "name" in data
        assert "version" in data
        assert "description" in data

    def test_manifest_name(self):
        data = json.loads(_generate_manifest())
        assert data["name"] == "koan-skills"

    def test_manifest_version(self):
        data = json.loads(_generate_manifest())
        assert data["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# _select_skills
# ---------------------------------------------------------------------------

class TestSelectSkills:
    def test_selects_agent_skills(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("status", "bridge", ""),
            ("quality", "agent", "Check quality"),
            ("review", "hybrid", "Review code"),
        ])
        skills = _select_skills(registry)
        names = {s.name for s in skills}
        assert "quality" in names
        assert "review" in names
        assert "status" not in names

    def test_selects_command_skills(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("deploy", "command", "Deploy app"),
            ("status", "bridge", ""),
        ])
        skills = _select_skills(registry)
        names = {s.name for s in skills}
        assert "deploy" in names
        assert "status" not in names

    def test_custom_audiences(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("ctl", "bridge", ""),
            ("lint", "agent", "Lint"),
        ])
        # Only include bridge
        skills = _select_skills(registry, include_audiences=["bridge"])
        names = {s.name for s in skills}
        assert "ctl" in names
        assert "lint" not in names

    def test_empty_registry(self, tmp_path):
        registry = SkillRegistry(tmp_path)
        skills = _select_skills(registry)
        assert skills == []

    def test_no_matching_audience(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("ctl", "bridge", ""),
        ])
        skills = _select_skills(registry)
        assert skills == []


# ---------------------------------------------------------------------------
# generate_plugin_dir
# ---------------------------------------------------------------------------

class TestGeneratePluginDir:
    def test_creates_directory(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("review", "hybrid", "Review code"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            assert plugin_dir.exists()
            assert plugin_dir.is_dir()
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_manifest_created(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("review", "agent", ""),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            manifest = plugin_dir / ".claude-plugin" / "plugin.json"
            assert manifest.exists()
            data = json.loads(manifest.read_text())
            assert data["name"] == "koan-skills"
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_agent_skill_in_skills_dir(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("quality", "agent", "Check code quality"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            skill_md = plugin_dir / "skills" / "quality" / "SKILL.md"
            assert skill_md.exists()
            content = skill_md.read_text()
            assert "name: quality" in content
            assert "Check code quality" in content
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_command_skill_in_commands_dir(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("deploy", "command", "Deploy to production"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            cmd_md = plugin_dir / "commands" / "deploy.md"
            assert cmd_md.exists()
            content = cmd_md.read_text()
            assert "Deploy to production" in content
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_hybrid_skill_in_both_dirs(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("review", "hybrid", "Review code"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            # Should be in skills/
            skill_md = plugin_dir / "skills" / "review" / "SKILL.md"
            assert skill_md.exists()
            # Should also be in commands/
            cmd_md = plugin_dir / "commands" / "review.md"
            assert cmd_md.exists()
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_bridge_skill_excluded(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("status", "bridge", ""),
            ("review", "agent", "Review"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            # Bridge skill should not appear
            assert not (plugin_dir / "skills" / "status").exists()
            assert not (plugin_dir / "commands" / "status.md").exists()
            # Agent skill should appear
            assert (plugin_dir / "skills" / "review" / "SKILL.md").exists()
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_empty_registry_creates_manifest_only(self, tmp_path):
        registry = SkillRegistry(tmp_path)
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            assert (plugin_dir / ".claude-plugin" / "plugin.json").exists()
            # No skills or commands dirs created
            assert not (plugin_dir / "skills").exists()
            assert not (plugin_dir / "commands").exists()
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_multiple_skills(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("quality", "agent", "Code quality"),
            ("security", "agent", "Security checks"),
            ("refactor", "command", "Refactor code"),
            ("review", "hybrid", "Review code"),
            ("status", "bridge", "Show status"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            # Agent skills in skills/
            assert (plugin_dir / "skills" / "quality" / "SKILL.md").exists()
            assert (plugin_dir / "skills" / "security" / "SKILL.md").exists()
            # Hybrid in both
            assert (plugin_dir / "skills" / "review" / "SKILL.md").exists()
            assert (plugin_dir / "commands" / "review.md").exists()
            # Command in commands/
            assert (plugin_dir / "commands" / "refactor.md").exists()
            # Bridge excluded
            assert not (plugin_dir / "skills" / "status").exists()
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_custom_base_dir(self, tmp_path):
        custom_base = tmp_path / "custom"
        custom_base.mkdir()
        registry = _make_registry(tmp_path, [
            ("test", "agent", ""),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=custom_base)
        try:
            assert str(plugin_dir).startswith(str(custom_base))
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_dir_name_prefix(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("test", "agent", ""),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        try:
            assert "koan-plugins-" in plugin_dir.name
        finally:
            cleanup_plugin_dir(plugin_dir)

    def test_custom_include_audiences(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("ctl", "bridge", ""),
            ("lint", "agent", "Lint"),
        ])
        # Include bridge only
        plugin_dir = generate_plugin_dir(
            registry,
            include_audiences=["bridge"],
            base_dir=tmp_path,
        )
        try:
            # Bridge skills go to skills/ when explicitly included
            # (they have audience=bridge which is not in agent/command/hybrid)
            # Actually bridge goes nowhere — it's not agent or command
            # So only the manifest should exist
            assert (plugin_dir / ".claude-plugin" / "plugin.json").exists()
        finally:
            cleanup_plugin_dir(plugin_dir)


# ---------------------------------------------------------------------------
# cleanup_plugin_dir
# ---------------------------------------------------------------------------

class TestCleanupPluginDir:
    def test_removes_directory(self, tmp_path):
        target = tmp_path / "to-remove"
        target.mkdir()
        (target / "file.txt").write_text("data")
        assert cleanup_plugin_dir(target) is True
        assert not target.exists()

    def test_nonexistent_returns_false(self, tmp_path):
        assert cleanup_plugin_dir(tmp_path / "nonexistent") is False

    def test_removes_nested_contents(self, tmp_path):
        target = tmp_path / "nested"
        (target / "a" / "b").mkdir(parents=True)
        (target / "a" / "b" / "c.txt").write_text("data")
        assert cleanup_plugin_dir(target) is True
        assert not target.exists()


# ---------------------------------------------------------------------------
# Integration: generate + cleanup lifecycle
# ---------------------------------------------------------------------------

class TestPluginLifecycle:
    def test_generate_and_cleanup(self, tmp_path):
        registry = _make_registry(tmp_path, [
            ("review", "hybrid", "Review code"),
            ("quality", "agent", "Code quality"),
        ])
        plugin_dir = generate_plugin_dir(registry, base_dir=tmp_path)
        assert plugin_dir.exists()

        # Verify contents
        assert (plugin_dir / ".claude-plugin" / "plugin.json").exists()
        assert (plugin_dir / "skills" / "review" / "SKILL.md").exists()
        assert (plugin_dir / "skills" / "quality" / "SKILL.md").exists()
        assert (plugin_dir / "commands" / "review.md").exists()

        # Cleanup
        assert cleanup_plugin_dir(plugin_dir) is True
        assert not plugin_dir.exists()

    def test_default_registry_integration(self):
        """Generate plugin dir from the real core skills registry."""
        from app.skills import build_registry

        registry = build_registry()
        plugin_dir = generate_plugin_dir(registry)
        try:
            assert plugin_dir.exists()
            manifest = plugin_dir / ".claude-plugin" / "plugin.json"
            assert manifest.exists()

            # Hybrid skills (like pr, review, plan) should be present
            hybrid_skills = registry.list_by_audience("hybrid")
            for skill in hybrid_skills:
                assert (plugin_dir / "skills" / skill.name / "SKILL.md").exists()
                assert (plugin_dir / "commands" / f"{skill.name}.md").exists()
        finally:
            cleanup_plugin_dir(plugin_dir)
