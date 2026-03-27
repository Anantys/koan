"""Tests for app/skills.py — SKILL.md parsing, registry, and skill execution."""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.skills import (
    DEFAULT_AUDIENCE,
    Skill,
    SkillCommand,
    SkillContext,
    SkillError,
    SkillRegistry,
    VALID_AUDIENCES,
    _parse_bool_flag,
    _parse_inline_list,
    _parse_yaml_lite,
    build_registry,
    execute_skill,
    get_default_skills_dir,
    parse_skill_md,
)


# ---------------------------------------------------------------------------
# _parse_inline_list
# ---------------------------------------------------------------------------

class TestParseInlineList:
    def test_empty_brackets(self):
        assert _parse_inline_list("[]") == []

    def test_single_item(self):
        assert _parse_inline_list("[foo]") == ["foo"]

    def test_multiple_items(self):
        assert _parse_inline_list("[a, b, c]") == ["a", "b", "c"]

    def test_quoted_items(self):
        assert _parse_inline_list('["a", "b"]') == ["a", "b"]

    def test_no_brackets(self):
        assert _parse_inline_list("a, b") == ["a", "b"]


# ---------------------------------------------------------------------------
# _parse_yaml_lite
# ---------------------------------------------------------------------------

class TestParseYamlLite:
    def test_simple_key_value(self):
        result = _parse_yaml_lite("name: test\ndescription: A test skill")
        assert result["name"] == "test"
        assert result["description"] == "A test skill"

    def test_inline_list(self):
        result = _parse_yaml_lite("aliases: [a, b, c]")
        assert result["aliases"] == ["a", "b", "c"]

    def test_commands_block(self):
        yaml = textwrap.dedent("""\
            name: status
            commands:
              - name: status
                description: Quick status
                aliases: [st]
              - name: ping
                description: Check liveness
        """)
        result = _parse_yaml_lite(yaml)
        assert result["name"] == "status"
        assert len(result["commands"]) == 2
        assert result["commands"][0]["name"] == "status"
        assert result["commands"][0]["description"] == "Quick status"
        assert result["commands"][0]["aliases"] == ["st"]
        assert result["commands"][1]["name"] == "ping"

    def test_commands_with_usage(self):
        yaml = textwrap.dedent("""\
            name: cancel
            commands:
              - name: cancel
                description: Cancel a pending mission
                usage: /cancel <n>, /cancel <keyword>
        """)
        result = _parse_yaml_lite(yaml)
        assert len(result["commands"]) == 1
        assert result["commands"][0]["usage"] == "/cancel <n>, /cancel <keyword>"

    def test_empty_string(self):
        assert _parse_yaml_lite("") == {}

    def test_comments_ignored(self):
        result = _parse_yaml_lite("# comment\nname: test")
        assert result["name"] == "test"


# ---------------------------------------------------------------------------
# _parse_bool_flag
# ---------------------------------------------------------------------------

class TestParseBoolFlag:
    def test_true_lowercase(self):
        assert _parse_bool_flag({"flag": "true"}, "flag") is True

    def test_true_uppercase(self):
        assert _parse_bool_flag({"flag": "True"}, "flag") is True

    def test_true_mixed_case(self):
        assert _parse_bool_flag({"flag": "TRUE"}, "flag") is True

    def test_yes_lowercase(self):
        assert _parse_bool_flag({"flag": "yes"}, "flag") is True

    def test_yes_uppercase(self):
        assert _parse_bool_flag({"flag": "YES"}, "flag") is True

    def test_one_string(self):
        assert _parse_bool_flag({"flag": "1"}, "flag") is True

    def test_false_string(self):
        assert _parse_bool_flag({"flag": "false"}, "flag") is False

    def test_no_string(self):
        assert _parse_bool_flag({"flag": "no"}, "flag") is False

    def test_zero_string(self):
        assert _parse_bool_flag({"flag": "0"}, "flag") is False

    def test_empty_string(self):
        assert _parse_bool_flag({"flag": ""}, "flag") is False

    def test_missing_key(self):
        assert _parse_bool_flag({}, "flag") is False

    def test_arbitrary_string(self):
        assert _parse_bool_flag({"flag": "maybe"}, "flag") is False


# ---------------------------------------------------------------------------
# parse_skill_md
# ---------------------------------------------------------------------------

class TestParseSkillMd:
    def test_valid_skill(self, tmp_path):
        skill_dir = tmp_path / "koan" / "status"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: status
            scope: koan
            description: Show status
            version: 1.0.0
            commands:
              - name: status
                description: Quick status
                aliases: [st]
              - name: ping
                description: Check liveness
            ---

            This is the prompt body.
        """))

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.name == "status"
        assert skill.scope == "koan"
        assert skill.description == "Show status"
        assert skill.version == "1.0.0"
        assert len(skill.commands) == 2
        assert skill.commands[0].name == "status"
        assert skill.commands[0].aliases == ["st"]
        assert skill.commands[1].name == "ping"
        assert skill.prompt_body == "This is the prompt body."
        assert skill.qualified_name == "koan.status"

    def test_group_field_parsed(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: mission
            scope: core
            group: missions
            description: Create a mission
            commands:
              - name: mission
                description: Create a mission
            ---
        """))
        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.group == "missions"

    def test_group_field_defaults_empty(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\nscope: core\n---\nbody")
        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.group == ""

    def test_no_frontmatter(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("Just some text without frontmatter")
        assert parse_skill_md(skill_md) is None

    def test_no_name(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\ndescription: test\n---\nbody")
        assert parse_skill_md(skill_md) is None

    def test_nonexistent_file(self, tmp_path):
        assert parse_skill_md(tmp_path / "nonexistent.md") is None

    def test_handler_path_resolved(self, tmp_path):
        skill_dir = tmp_path / "koan" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "handler.py").write_text("def handle(ctx): return 'ok'")
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("---\nname: test\nhandler: handler.py\n---\nbody")

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.has_handler()
        assert skill.handler_path == skill_dir / "handler.py"

    def test_handler_missing(self, tmp_path):
        skill_dir = tmp_path / "koan" / "test"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("---\nname: test\nhandler: handler.py\n---\nbody")

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert not skill.has_handler()

    def test_usage_field_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "cancel"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: cancel
            scope: core
            description: Cancel a pending mission
            commands:
              - name: cancel
                description: Cancel a pending mission
                usage: /cancel <n>, /cancel <keyword>
            handler: handler.py
            ---
        """))

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.commands[0].usage == "/cancel <n>, /cancel <keyword>"

    def test_usage_absent_defaults_empty(self, tmp_path):
        skill_dir = tmp_path / "core" / "status"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: status
            scope: core
            commands:
              - name: status
                description: Quick status
            ---
        """))

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.commands[0].usage == ""

    def test_scope_inferred_from_parent(self, tmp_path):
        skill_dir = tmp_path / "myproject" / "myskill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("---\nname: myskill\n---\nbody")

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.scope == "myproject"

    def test_cli_skill_field_parsed(self, tmp_path):
        """cli_skill field is parsed from frontmatter and stored on the Skill."""
        skill_dir = tmp_path / "group" / "myskill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: myskill
            scope: group
            description: Bridge to my-tool
            audience: agent
            cli_skill: my-tool
            commands:
              - name: myskill
                description: Invoke /my-tool
            ---
        """))

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.cli_skill == "my-tool"
        assert skill.audience == "agent"

    def test_cli_skill_absent_defaults_none(self, tmp_path):
        """Skills without cli_skill field have cli_skill=None."""
        skill_dir = tmp_path / "core" / "status"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: status
            scope: core
            commands:
              - name: status
                description: Quick status
            ---
        """))

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.cli_skill is None

    def test_cli_skill_empty_value_treated_as_none(self, tmp_path):
        """An empty cli_skill value is treated as None (not set)."""
        skill_dir = tmp_path / "group" / "empty"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: empty
            scope: group
            cli_skill:
            commands:
              - name: empty
                description: Empty cli_skill
            ---
        """))

        skill = parse_skill_md(skill_md)
        assert skill is not None
        assert skill.cli_skill is None


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------

class TestSkillRegistry:
    def _make_skill_tree(self, tmp_path):
        """Create a skills directory with 2 scopes and 3 skills."""
        # koan/status
        status_dir = tmp_path / "koan" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: status
            scope: koan
            description: Show status
            commands:
              - name: status
                description: Quick status
                aliases: [st]
              - name: ping
                description: Check liveness
            ---
        """))

        # koan/verbose
        verbose_dir = tmp_path / "koan" / "verbose"
        verbose_dir.mkdir(parents=True)
        (verbose_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: verbose
            scope: koan
            description: Toggle verbose mode
            commands:
              - name: verbose
                description: Enable verbose
              - name: silent
                description: Disable verbose
            ---
        """))

        # myproject/deploy
        deploy_dir = tmp_path / "myproject" / "deploy"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: deploy
            scope: myproject
            description: Deploy to staging
            commands:
              - name: deploy
                description: Deploy
            ---
        """))

        return tmp_path

    def test_discover_skills(self, tmp_path):
        skills_dir = self._make_skill_tree(tmp_path)
        registry = SkillRegistry(skills_dir)

        assert len(registry) == 3
        assert "koan.status" in registry
        assert "koan.verbose" in registry
        assert "myproject.deploy" in registry

    def test_get_skill(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        skill = registry.get("koan", "status")
        assert skill is not None
        assert skill.name == "status"

    def test_get_nonexistent(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        assert registry.get("koan", "nonexistent") is None

    def test_find_by_command(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        skill = registry.find_by_command("ping")
        assert skill is not None
        assert skill.name == "status"

    def test_find_by_alias(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        skill = registry.find_by_command("st")
        assert skill is not None
        assert skill.name == "status"

    def test_find_unknown_command(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        assert registry.find_by_command("unknown") is None

    def test_suggest_command_close_match(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        # "statu" is close to "status"
        assert registry.suggest_command("statu") == "status"

    def test_suggest_command_no_match(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        assert registry.suggest_command("xyzzy") is None

    def test_suggest_command_with_extra_commands(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        # "hel" is close to "help" (not in registry, but in extra_commands)
        assert registry.suggest_command("hel", extra_commands=["help", "stop"]) == "help"

    def test_suggest_command_prefers_registry_over_extra(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        # "statu" matches "status" from registry
        result = registry.suggest_command("statu", extra_commands=["stop"])
        assert result == "status"

    def test_suggest_command_matches_alias(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        # "s" is too short for cutoff, but "deplo" should match "deploy"
        assert registry.suggest_command("deplo") == "deploy"

    def test_list_all(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        skills = registry.list_all()
        assert len(skills) == 3

    def test_list_by_scope(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        koan_skills = registry.list_by_scope("koan")
        assert len(koan_skills) == 2
        names = {s.name for s in koan_skills}
        assert names == {"status", "verbose"}

    def test_scopes(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        assert registry.scopes() == ["koan", "myproject"]

    def test_empty_dir(self, tmp_path):
        registry = SkillRegistry(tmp_path)
        assert len(registry) == 0

    def test_none_dir(self):
        registry = SkillRegistry(None)
        assert len(registry) == 0

    def test_get_by_qualified_name(self, tmp_path):
        registry = SkillRegistry(self._make_skill_tree(tmp_path))
        skill = registry.get_by_qualified_name("koan.verbose")
        assert skill is not None
        assert skill.name == "verbose"

    def test_list_by_group(self, tmp_path):
        """Skills with group field are found by list_by_group."""
        # Create core-scoped skills with groups
        missions_dir = tmp_path / "core" / "mission"
        missions_dir.mkdir(parents=True)
        (missions_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: mission
            scope: core
            group: missions
            description: Create a mission
            commands:
              - name: mission
                description: Create a mission
            ---
        """))
        cancel_dir = tmp_path / "core" / "cancel"
        cancel_dir.mkdir(parents=True)
        (cancel_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: cancel
            scope: core
            group: missions
            description: Cancel a mission
            commands:
              - name: cancel
                description: Cancel a mission
            ---
        """))
        review_dir = tmp_path / "core" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: review
            scope: core
            group: code
            description: Review code
            commands:
              - name: review
                description: Review code
            ---
        """))
        registry = SkillRegistry(tmp_path)
        missions = registry.list_by_group("missions")
        assert len(missions) == 2
        names = {s.name for s in missions}
        assert names == {"mission", "cancel"}

        code = registry.list_by_group("code")
        assert len(code) == 1
        assert code[0].name == "review"

    def test_groups(self, tmp_path):
        """groups() returns sorted distinct group names from core skills."""
        for name, group in [("a", "code"), ("b", "missions"), ("c", "code")]:
            d = tmp_path / "core" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(textwrap.dedent(f"""\
                ---
                name: {name}
                scope: core
                group: {group}
                commands:
                  - name: {name}
                    description: test
                ---
            """))
        registry = SkillRegistry(tmp_path)
        assert registry.groups() == ["code", "missions"]

    def test_list_by_group_excludes_non_core(self, tmp_path):
        """list_by_group only returns core-scoped skills."""
        d = tmp_path / "custom" / "deploy"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: deploy
            scope: custom
            group: missions
            commands:
              - name: deploy
                description: Deploy
            ---
        """))
        registry = SkillRegistry(tmp_path)
        assert registry.list_by_group("missions") == []


# ---------------------------------------------------------------------------
# Skill execution
# ---------------------------------------------------------------------------

class TestExecuteSkill:
    def test_handler_based_skill(self, tmp_path):
        handler_dir = tmp_path / "koan" / "test"
        handler_dir.mkdir(parents=True)
        (handler_dir / "handler.py").write_text(
            "def handle(ctx): return f'Hello {ctx.args}'"
        )

        skill = Skill(
            name="test",
            scope="koan",
            handler_path=handler_dir / "handler.py",
            skill_dir=handler_dir,
        )

        ctx = SkillContext(
            koan_root=tmp_path,
            instance_dir=tmp_path,
            args="world",
        )

        result = execute_skill(skill, ctx)
        assert result == "Hello world"

    def test_prompt_based_skill(self, tmp_path):
        skill = Skill(
            name="test",
            scope="koan",
            prompt_body="This is the prompt for Claude",
        )

        ctx = SkillContext(
            koan_root=tmp_path,
            instance_dir=tmp_path,
        )

        result = execute_skill(skill, ctx)
        assert result == "This is the prompt for Claude"

    def test_handler_error_returns_message(self, tmp_path):
        handler_dir = tmp_path / "koan" / "broken"
        handler_dir.mkdir(parents=True)
        (handler_dir / "handler.py").write_text(
            "def handle(ctx): raise ValueError('boom')"
        )

        skill = Skill(
            name="broken",
            scope="koan",
            handler_path=handler_dir / "handler.py",
            skill_dir=handler_dir,
        )

        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path)
        result = execute_skill(skill, ctx)
        assert isinstance(result, SkillError)
        assert result.skill_name == "koan.broken"
        assert isinstance(result.exception, ValueError)
        assert "boom" in result.message

    def test_no_handler_no_prompt(self, tmp_path):
        skill = Skill(name="empty", scope="koan")
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path)
        assert execute_skill(skill, ctx) is None

    def test_handler_missing_handle_function(self, tmp_path):
        handler_dir = tmp_path / "koan" / "nohandle"
        handler_dir.mkdir(parents=True)
        (handler_dir / "handler.py").write_text("x = 42")

        skill = Skill(
            name="nohandle",
            scope="koan",
            handler_path=handler_dir / "handler.py",
            skill_dir=handler_dir,
        )

        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path)
        assert execute_skill(skill, ctx) is None


# ---------------------------------------------------------------------------
# Default skills directory
# ---------------------------------------------------------------------------

class TestDefaultSkillsDir:
    def test_default_dir_exists(self):
        skills_dir = get_default_skills_dir()
        assert skills_dir.exists()
        assert skills_dir.is_dir()

    def test_core_scope_exists(self):
        skills_dir = get_default_skills_dir()
        assert (skills_dir / "core").is_dir()


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------

class TestBuildRegistry:
    def test_loads_default_skills(self):
        registry = build_registry()
        assert len(registry) > 0
        assert "core.status" in registry

    def test_with_extra_dirs(self, tmp_path):
        # Create extra skill in a custom dir
        extra_dir = tmp_path / "custom" / "myskill"
        extra_dir.mkdir(parents=True)
        (extra_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: myskill
            scope: custom
            description: A custom skill
            commands:
              - name: myskill
                description: Do something
            ---
        """))

        registry = build_registry(extra_dirs=[tmp_path])
        assert "custom.myskill" in registry

    def test_extra_nonexistent_dir(self, tmp_path):
        # Should not crash on nonexistent dirs
        registry = build_registry(extra_dirs=[tmp_path / "nonexistent"])
        assert len(registry) > 0  # Still has defaults


# ---------------------------------------------------------------------------
# SkillContext
# ---------------------------------------------------------------------------

class TestSkillContext:
    def test_defaults(self, tmp_path):
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path)
        assert ctx.command_name == ""
        assert ctx.args == ""
        assert ctx.send_message is None
        assert ctx.handle_chat is None

    def test_with_send_message(self, tmp_path):
        mock_send = MagicMock()
        ctx = SkillContext(
            koan_root=tmp_path,
            instance_dir=tmp_path,
            send_message=mock_send,
        )
        ctx.send_message("test")
        mock_send.assert_called_once_with("test")

    def test_with_handle_chat(self, tmp_path):
        mock_chat = MagicMock()
        ctx = SkillContext(
            koan_root=tmp_path,
            instance_dir=tmp_path,
            handle_chat=mock_chat,
        )
        ctx.handle_chat("hello world")
        mock_chat.assert_called_once_with("hello world")


# ---------------------------------------------------------------------------
# Skill dataclass
# ---------------------------------------------------------------------------

class TestSkill:
    def test_qualified_name(self):
        skill = Skill(name="status", scope="koan")
        assert skill.qualified_name == "koan.status"

    def test_has_handler_no_path(self):
        skill = Skill(name="test", scope="koan")
        assert not skill.has_handler()

    def test_has_handler_nonexistent_path(self, tmp_path):
        skill = Skill(name="test", scope="koan", handler_path=tmp_path / "nope.py")
        assert not skill.has_handler()

    def test_has_handler_exists(self, tmp_path):
        handler = tmp_path / "handler.py"
        handler.write_text("def handle(ctx): pass")
        skill = Skill(name="test", scope="koan", handler_path=handler)
        assert skill.has_handler()

    def test_worker_default_false(self):
        skill = Skill(name="test", scope="koan")
        assert not skill.worker

    def test_worker_explicit_true(self):
        skill = Skill(name="test", scope="koan", worker=True)
        assert skill.worker


# ---------------------------------------------------------------------------
# Worker field parsing
# ---------------------------------------------------------------------------

class TestWorkerField:
    """Tests for the 'worker: true' field in SKILL.md."""

    def test_worker_true_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "blocking"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: blocking
            scope: core
            description: A blocking skill
            worker: true
            commands:
              - name: blocking
                description: Does blocking work
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.worker is True

    def test_worker_false_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "fast"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: fast
            scope: core
            description: A fast skill
            worker: false
            commands:
              - name: fast
                description: Does fast work
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.worker is False

    def test_worker_absent_defaults_false(self, tmp_path):
        skill_dir = tmp_path / "core" / "normal"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: normal
            scope: core
            description: A normal skill
            commands:
              - name: normal
                description: Does normal work
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.worker is False

    def test_sparring_skill_is_worker(self):
        """Sparring core skill should have worker=true."""
        registry = build_registry()
        skill = registry.get("core", "sparring")
        assert skill is not None
        assert skill.worker is True

    def test_pr_skill_is_worker(self):
        """PR core skill should have worker=true."""
        registry = build_registry()
        skill = registry.get("core", "pr")
        assert skill is not None
        assert skill.worker is True

    def test_status_skill_not_worker(self):
        """Status core skill should NOT be a worker (reads files only)."""
        registry = build_registry()
        skill = registry.get("core", "status")
        assert skill is not None
        assert skill.worker is False


# ---------------------------------------------------------------------------
# GitHub integration fields
# ---------------------------------------------------------------------------

class TestGitHubFields:
    """Tests for GitHub integration fields in SKILL.md."""

    def test_github_enabled_true(self, tmp_path):
        skill_dir = tmp_path / "core" / "rebase"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: rebase
            scope: core
            description: Rebase a PR
            github_enabled: true
            commands:
              - name: rebase
                description: Rebase a PR
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.github_enabled is True
        assert skill.github_context_aware is False

    def test_github_context_aware_true(self, tmp_path):
        skill_dir = tmp_path / "core" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: review
            scope: core
            description: Review code
            github_context_aware: true
            commands:
              - name: review
                description: Review code
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.github_enabled is False
        assert skill.github_context_aware is True

    def test_both_github_flags_true(self, tmp_path):
        skill_dir = tmp_path / "core" / "implement"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: implement
            scope: core
            description: Implement a feature
            github_enabled: true
            github_context_aware: true
            commands:
              - name: implement
                description: Implement a feature
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.github_enabled is True
        assert skill.github_context_aware is True

    def test_github_flags_absent_default_false(self, tmp_path):
        skill_dir = tmp_path / "core" / "status"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: status
            scope: core
            description: Show status
            commands:
              - name: status
                description: Show status
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.github_enabled is False
        assert skill.github_context_aware is False

    def test_rebase_skill_github_enabled(self):
        """Rebase core skill should have github_enabled=true."""
        registry = build_registry()
        skill = registry.get("core", "rebase")
        assert skill is not None
        assert skill.github_enabled is True

    def test_recreate_skill_github_enabled(self):
        """Recreate core skill should have github_enabled=true."""
        registry = build_registry()
        skill = registry.get("core", "recreate")
        assert skill is not None
        assert skill.github_enabled is True

    def test_plan_skill_github_enabled(self):
        """Plan core skill should have github_enabled=true."""
        registry = build_registry()
        skill = registry.get("core", "plan")
        assert skill is not None
        assert skill.github_enabled is True
        assert skill.github_context_aware is True


# ---------------------------------------------------------------------------
# Audience field
# ---------------------------------------------------------------------------

class TestAudienceField:
    """Tests for the 'audience' field in SKILL.md."""

    def test_audience_bridge_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "ctl"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: ctl
            scope: core
            description: A bridge-only skill
            audience: bridge
            commands:
              - name: ctl
                description: Control something
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == "bridge"

    def test_audience_hybrid_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: review
            scope: core
            description: A hybrid skill
            audience: hybrid
            commands:
              - name: review
                description: Review code
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == "hybrid"

    def test_audience_agent_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "lint"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: lint
            scope: core
            description: An agent-only skill
            audience: agent
            commands:
              - name: lint
                description: Lint code
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == "agent"

    def test_audience_command_parsed(self, tmp_path):
        skill_dir = tmp_path / "core" / "slash"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: slash
            scope: core
            description: A command skill
            audience: command
            commands:
              - name: slash
                description: Slash command
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == "command"

    def test_audience_absent_defaults_to_bridge(self, tmp_path):
        skill_dir = tmp_path / "core" / "simple"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: simple
            scope: core
            description: No audience field
            commands:
              - name: simple
                description: Simple skill
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == DEFAULT_AUDIENCE
        assert skill.audience == "bridge"

    def test_audience_invalid_falls_back_to_default(self, tmp_path):
        skill_dir = tmp_path / "core" / "bad"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: bad
            scope: core
            description: Invalid audience value
            audience: foobar
            commands:
              - name: bad
                description: Bad audience
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == DEFAULT_AUDIENCE

    def test_audience_case_insensitive(self, tmp_path):
        skill_dir = tmp_path / "core" / "upper"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: upper
            scope: core
            description: Uppercase audience
            audience: HYBRID
            commands:
              - name: upper
                description: Uppercase test
            ---
        """))
        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.audience == "hybrid"

    def test_skill_dataclass_default_audience(self):
        skill = Skill(name="test", scope="core")
        assert skill.audience == DEFAULT_AUDIENCE

    def test_valid_audiences_constant(self):
        assert "bridge" in VALID_AUDIENCES
        assert "agent" in VALID_AUDIENCES
        assert "command" in VALID_AUDIENCES
        assert "hybrid" in VALID_AUDIENCES
        assert len(VALID_AUDIENCES) == 4

    def test_status_skill_is_bridge(self):
        """Status core skill should be audience: bridge."""
        registry = build_registry()
        skill = registry.get("core", "status")
        assert skill is not None
        assert skill.audience == "bridge"

    def test_pr_skill_is_hybrid(self):
        """PR core skill should be audience: hybrid."""
        registry = build_registry()
        skill = registry.get("core", "pr")
        assert skill is not None
        assert skill.audience == "hybrid"

    def test_rebase_skill_is_hybrid(self):
        """Rebase core skill should be audience: hybrid."""
        registry = build_registry()
        skill = registry.get("core", "rebase")
        assert skill is not None
        assert skill.audience == "hybrid"

    def test_list_by_audience_single(self, tmp_path):
        """list_by_audience with one audience type."""
        self._make_mixed_registry(tmp_path)
        registry = SkillRegistry(tmp_path)
        hybrids = registry.list_by_audience("hybrid")
        assert len(hybrids) == 1
        assert hybrids[0].name == "review"

    def test_list_by_audience_multiple(self, tmp_path):
        """list_by_audience with multiple audience types."""
        self._make_mixed_registry(tmp_path)
        registry = SkillRegistry(tmp_path)
        result = registry.list_by_audience("bridge", "hybrid")
        assert len(result) == 2
        names = {s.name for s in result}
        assert names == {"ctl", "review"}

    def test_list_by_audience_empty(self, tmp_path):
        """list_by_audience returns empty for unmatched audience."""
        self._make_mixed_registry(tmp_path)
        registry = SkillRegistry(tmp_path)
        assert registry.list_by_audience("command") == []

    def _make_mixed_registry(self, tmp_path):
        """Helper: create two skills with different audiences."""
        # bridge skill
        d1 = tmp_path / "core" / "ctl"
        d1.mkdir(parents=True)
        (d1 / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: ctl
            scope: core
            description: Bridge skill
            audience: bridge
            commands:
              - name: ctl
                description: Control
            ---
        """))
        # hybrid skill
        d2 = tmp_path / "core" / "review"
        d2.mkdir(parents=True)
        (d2 / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: review
            scope: core
            description: Hybrid skill
            audience: hybrid
            commands:
              - name: review
                description: Review
            ---
        """))


# ---------------------------------------------------------------------------
# Scoped command resolution
# ---------------------------------------------------------------------------

class TestResolveScopedCommand:
    def _make_registry(self, tmp_path):
        """Create a registry with skills in multiple scopes."""
        # core/status
        status_dir = tmp_path / "core" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: status
            scope: core
            description: Show status
            commands:
              - name: status
                description: Quick status
              - name: ping
                description: Check liveness
            ---
        """))

        # myproject/review
        review_dir = tmp_path / "myproject" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: review
            scope: myproject
            description: Code review
            commands:
              - name: review
                description: Run code review
            ---
        """))

        return SkillRegistry(tmp_path)

    def test_resolve_scope_skill(self, tmp_path):
        registry = self._make_registry(tmp_path)
        result = registry.resolve_scoped_command("myproject.review")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "review"
        assert cmd == "review"
        assert args == ""

    def test_resolve_scope_skill_with_args(self, tmp_path):
        registry = self._make_registry(tmp_path)
        result = registry.resolve_scoped_command("myproject.review some args here")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "review"
        assert cmd == "review"
        assert args == "some args here"

    def test_resolve_scope_skill_subcommand(self, tmp_path):
        registry = self._make_registry(tmp_path)
        result = registry.resolve_scoped_command("core.status.ping")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "status"
        assert cmd == "ping"

    def test_resolve_nonexistent_scope(self, tmp_path):
        registry = self._make_registry(tmp_path)
        assert registry.resolve_scoped_command("unknown.review") is None

    def test_resolve_nonexistent_skill(self, tmp_path):
        registry = self._make_registry(tmp_path)
        assert registry.resolve_scoped_command("core.unknown") is None

    def test_resolve_single_segment_returns_none(self, tmp_path):
        registry = self._make_registry(tmp_path)
        assert registry.resolve_scoped_command("status") is None

    def test_resolve_by_command_name_when_skill_name_differs(self, tmp_path):
        """Scoped lookup should work by command name, not just skill name.

        When a custom skill has name 'refactor' but a command named 'wp_refactor',
        /wp.wp_refactor should still resolve via command name fallback.
        """
        # Create a custom skill where command name ≠ skill name
        custom_dir = tmp_path / "wp" / "refactor"
        custom_dir.mkdir(parents=True)
        (custom_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: refactor
            scope: wp
            description: WP refactoring
            commands:
              - name: wp_refactor
                description: Refactor WP code
            ---
        """))
        registry = SkillRegistry(tmp_path)
        # /wp.wp_refactor should find the skill via command name
        result = registry.resolve_scoped_command("wp.wp_refactor")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "refactor"
        assert skill.scope == "wp"
        assert cmd == "wp_refactor"

    def test_resolve_by_command_alias_in_scope(self, tmp_path):
        """Scoped lookup should also match command aliases within a scope."""
        custom_dir = tmp_path / "wp" / "checker"
        custom_dir.mkdir(parents=True)
        (custom_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: checker
            scope: wp
            description: WP checker
            commands:
              - name: check
                description: Run checks
                aliases: [chk, verify]
            ---
        """))
        registry = SkillRegistry(tmp_path)
        # /wp.chk should resolve via alias
        result = registry.resolve_scoped_command("wp.chk")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "checker"
        assert cmd == "chk"

    def test_resolve_by_command_name_with_args(self, tmp_path):
        """Command name fallback should preserve args."""
        custom_dir = tmp_path / "wp" / "refactor"
        custom_dir.mkdir(parents=True)
        (custom_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: refactor
            scope: wp
            description: WP refactoring
            commands:
              - name: wp_refactor
                description: Refactor WP code
            ---
        """))
        registry = SkillRegistry(tmp_path)
        result = registry.resolve_scoped_command("wp.wp_refactor some args here")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "refactor"
        assert cmd == "wp_refactor"
        assert args == "some args here"

    def test_skill_name_lookup_still_preferred(self, tmp_path):
        """Skill name match should be preferred over command name match."""
        # Skill with name matching the segment directly
        s1_dir = tmp_path / "wp" / "deploy"
        s1_dir.mkdir(parents=True)
        (s1_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: deploy
            scope: wp
            description: Deploy tool
            commands:
              - name: deploy
                description: Deploy
            ---
        """))
        registry = SkillRegistry(tmp_path)
        # /wp.deploy should resolve via skill name (preferred path)
        result = registry.resolve_scoped_command("wp.deploy")
        assert result is not None
        skill, cmd, args = result
        assert skill.name == "deploy"


# ---------------------------------------------------------------------------
# PR skill handler
# ---------------------------------------------------------------------------

class TestPrSkillHandler:
    """Tests for the /pr core skill handler."""

    def _load_handler(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pr_handler",
            str(Path(__file__).parent.parent / "skills" / "core" / "pr" / "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_no_args_returns_usage(self, tmp_path):
        mod = self._load_handler()
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="")
        result = mod.handle(ctx)
        assert "Usage" in result
        assert "/pr" in result

    def test_invalid_url_returns_error(self, tmp_path):
        mod = self._load_handler()
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="not-a-url")
        result = mod.handle(ctx)
        assert "No valid GitHub PR URL" in result

    def test_pr_skill_registered(self):
        """PR skill should be discoverable in the default registry."""
        registry = build_registry()
        assert "core.pr" in registry
        skill = registry.get("core", "pr")
        assert skill.worker is True

    def test_pr_command_findable(self):
        """The 'pr' command should be resolvable via find_by_command."""
        registry = build_registry()
        skill = registry.find_by_command("pr")
        assert skill is not None
        assert skill.name == "pr"


# ---------------------------------------------------------------------------
# Default registry includes all core skills
# ---------------------------------------------------------------------------

class TestCoreSkillsComplete:
    """Verify all expected core skills are registered."""

    def test_all_core_skills_present(self):
        registry = build_registry()
        expected = {"status", "journal", "sparring", "reflect",
                    "verbose", "chat", "mission", "language", "pr",
                    "list", "idea"}
        actual = {s.name for s in registry.list_by_scope("core")}
        assert expected.issubset(actual), f"Missing: {expected - actual}"

    def test_all_core_skills_have_handlers(self):
        """Every core skill should have a handler.py."""
        registry = build_registry()
        for skill in registry.list_by_scope("core"):
            assert skill.has_handler(), f"Skill {skill.name} missing handler.py"

    def test_chat_skill_is_worker(self):
        """Chat skill should be worker=true (handle_chat blocks on Claude call)."""
        registry = build_registry()
        skill = registry.get("core", "chat")
        assert skill is not None
        assert skill.worker is True

    def test_journal_alias_resolves(self):
        """'/journal' should resolve via alias to the journal skill."""
        registry = build_registry()
        skill = registry.find_by_command("journal")
        assert skill is not None
        assert skill.name == "journal"

    def test_log_resolves(self):
        """'/log' should resolve to the journal skill (primary command)."""
        registry = build_registry()
        skill = registry.find_by_command("log")
        assert skill is not None
        assert skill.name == "journal"

    def test_think_alias_resolves_to_reflect(self):
        """'/think' should resolve via alias to the reflect skill."""
        registry = build_registry()
        skill = registry.find_by_command("think")
        assert skill is not None
        assert skill.name == "reflect"

    def test_core_skills_with_args_have_usage(self):
        """Core skills that take arguments should have usage set."""
        registry = build_registry()
        commands_with_usage = {
            "chat", "idea", "log", "mission", "pr",
            "reflect", "cancel", "plan", "language", "priority",
        }
        for cmd_name in commands_with_usage:
            skill = registry.find_by_command(cmd_name)
            assert skill is not None, f"Command '{cmd_name}' not found"
            cmd = next(c for c in skill.commands if c.name == cmd_name)
            assert cmd.usage, f"Command '/{cmd_name}' should have usage set"


# ---------------------------------------------------------------------------
# Chat handler with handle_chat callback
# ---------------------------------------------------------------------------

class TestChatSkillHandler:
    """Tests for the chat skill handler using handle_chat callback."""

    def _load_handler(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chat_handler",
            str(Path(__file__).parent.parent / "skills" / "core" / "chat" / "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_empty_args_returns_usage(self, tmp_path):
        mod = self._load_handler()
        ctx = SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args="")
        result = mod.handle(ctx)
        assert "Usage" in result
        assert "/chat" in result

    def test_with_args_calls_handle_chat(self, tmp_path):
        mod = self._load_handler()
        mock_chat = MagicMock()
        ctx = SkillContext(
            koan_root=tmp_path, instance_dir=tmp_path,
            args="fix the login bug",
            handle_chat=mock_chat,
        )
        result = mod.handle(ctx)
        mock_chat.assert_called_once_with("fix the login bug")
        assert result == ""

    def test_no_handle_chat_callback(self, tmp_path):
        """Without handle_chat callback, returns error message."""
        mod = self._load_handler()
        ctx = SkillContext(
            koan_root=tmp_path, instance_dir=tmp_path,
            args="hello world",
        )
        result = mod.handle(ctx)
        assert "not available" in result


# ---------------------------------------------------------------------------
# Enforcement: every core skill must declare a help group
# ---------------------------------------------------------------------------

class TestCoreSkillGroupEnforcement:
    """Ensure all core skills have a 'group:' field so they appear in /help."""

    def test_all_core_skills_have_group(self):
        """Every SKILL.md under koan/skills/core/ must declare a non-empty group."""
        skills_dir = get_default_skills_dir()
        core_dir = skills_dir / "core"
        assert core_dir.is_dir(), f"Core skills dir not found: {core_dir}"

        missing = []
        for skill_md in sorted(core_dir.rglob("SKILL.md")):
            skill = parse_skill_md(skill_md)
            if skill is None:
                continue
            if not skill.group:
                missing.append(str(skill_md.relative_to(core_dir)))

        assert not missing, (
            f"Core skills missing 'group:' field (they won't appear in /help): "
            f"{', '.join(missing)}"
        )

    def test_core_skill_groups_are_known(self):
        """Every group used by core skills must exist in _GROUP_META."""
        from app.command_handlers import _GROUP_META

        skills_dir = get_default_skills_dir()
        core_dir = skills_dir / "core"
        unknown = []
        for skill_md in sorted(core_dir.rglob("SKILL.md")):
            skill = parse_skill_md(skill_md)
            if skill is None or not skill.group:
                continue
            if skill.group not in _GROUP_META:
                unknown.append(f"{skill.name} → {skill.group}")

        assert not unknown, (
            f"Core skills use unknown help groups (add to _GROUP_META): "
            f"{', '.join(unknown)}"
        )

    def test_registry_warns_on_missing_group(self, caplog):
        """Registry logs a warning when registering a core skill without group."""
        skill = Skill(name="orphan", scope="core", group="")
        registry = SkillRegistry()

        with caplog.at_level("WARNING", logger="app.skills"):
            registry._register(skill)

        assert registry.get("core", "orphan") is not None
        assert "no 'group:'" in caplog.text


class TestHyphenValidation:
    """Ensure skills with hyphens in command names or aliases are rejected."""

    def test_command_name_with_hyphen_skipped(self, caplog):
        """A command whose name contains a hyphen is skipped, but the skill is still registered."""
        skill = Skill(
            name="bad_skill", scope="custom",
            commands=[
                SkillCommand(name="bad-cmd", description="nope"),
                SkillCommand(name="good_cmd", description="ok"),
            ],
        )
        registry = SkillRegistry()

        with caplog.at_level("ERROR", logger="app.skills"):
            registry._register(skill)

        # The skill itself is registered
        assert registry.get("custom", "bad_skill") is not None
        # The bad command is not in the command map
        assert registry.find_by_command("bad-cmd") is None
        # The good command IS registered
        assert registry.find_by_command("good_cmd") is not None
        assert "contains a hyphen" in caplog.text
        assert "bad-cmd" in caplog.text

    def test_command_name_with_hyphen_only_command(self, caplog):
        """A skill whose only command has a hyphen is registered but has no commands mapped."""
        skill = Skill(
            name="bad_skill_only", scope="custom",
            commands=[SkillCommand(name="bad-cmd", description="nope")],
        )
        registry = SkillRegistry()

        with caplog.at_level("ERROR", logger="app.skills"):
            registry._register(skill)

        # Skill registered, but no commands accessible
        assert registry.get("custom", "bad_skill_only") is not None
        assert registry.find_by_command("bad-cmd") is None

    def test_alias_with_hyphen_skipped(self, caplog):
        """An alias containing a hyphen is skipped, but the command and skill remain."""
        skill = Skill(
            name="bad_skill2", scope="custom",
            commands=[SkillCommand(name="good_cmd", aliases=["bad-alias", "good_alias"])],
        )
        registry = SkillRegistry()

        with caplog.at_level("ERROR", logger="app.skills"):
            registry._register(skill)

        # Skill and command are registered
        assert registry.get("custom", "bad_skill2") is not None
        assert registry.find_by_command("good_cmd") is not None
        # Good alias works, bad alias doesn't
        assert registry.find_by_command("good_alias") is not None
        assert registry.find_by_command("bad-alias") is None
        assert "contain a hyphen" in caplog.text
        assert "bad-alias" in caplog.text

    def test_underscore_names_accepted(self):
        """Skills with underscores in names register normally."""
        skill = Skill(
            name="good_skill", scope="custom", group="test",
            commands=[SkillCommand(name="good_cmd", aliases=["gc"])],
        )
        registry = SkillRegistry()
        registry._register(skill)

        assert registry.get("custom", "good_skill") is not None
        assert registry.find_by_command("good_cmd") is not None
        assert registry.find_by_command("gc") is not None

    def test_no_existing_core_skills_have_hyphens(self):
        """Verify no shipped core skills use hyphens (enforces convention)."""
        skills_dir = get_default_skills_dir()
        core_dir = skills_dir / "core"
        assert core_dir.is_dir()

        violations = []
        for skill_md in sorted(core_dir.rglob("SKILL.md")):
            skill = parse_skill_md(skill_md)
            if skill is None:
                continue
            for cmd in skill.commands:
                if "-" in cmd.name:
                    violations.append(f"{skill.name}: command '{cmd.name}'")
                for alias in cmd.aliases:
                    if "-" in alias:
                        violations.append(f"{skill.name}: alias '{alias}'")

        assert not violations, (
            f"Core skills with hyphens in commands/aliases: {', '.join(violations)}"
        )


class TestAliasCollisionDetection:
    """Verify that SkillRegistry warns when two skills register the same command/alias."""

    def test_command_collision_warns(self, tmp_path, caplog):
        """Two skills with the same command name should log a warning."""
        skill_a = tmp_path / "core" / "skill_a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_a
            scope: core
            description: First skill
            group: status
            commands:
              - name: deploy
                description: Deploy A
            ---
        """))

        skill_b = tmp_path / "core" / "skill_b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_b
            scope: core
            description: Second skill
            group: status
            commands:
              - name: deploy
                description: Deploy B
            ---
        """))

        with caplog.at_level("WARNING"):
            registry = SkillRegistry(tmp_path)

        assert "collides" in caplog.text
        assert "deploy" in caplog.text
        assert "core.skill_a" in caplog.text
        assert "core.skill_b" in caplog.text

        # The later skill wins (overwrites)
        found = registry.find_by_command("deploy")
        assert found is not None
        assert found.name == "skill_b"

    def test_alias_collision_warns(self, tmp_path, caplog):
        """Two skills with overlapping aliases should log a warning."""
        skill_a = tmp_path / "core" / "skill_a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_a
            scope: core
            description: First skill
            group: status
            commands:
              - name: alpha
                description: Alpha cmd
                aliases: [a]
            ---
        """))

        skill_b = tmp_path / "core" / "skill_b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_b
            scope: core
            description: Second skill
            group: status
            commands:
              - name: beta
                description: Beta cmd
                aliases: [a]
            ---
        """))

        with caplog.at_level("WARNING"):
            SkillRegistry(tmp_path)

        assert "collides" in caplog.text
        assert "alias" in caplog.text
        assert "'a'" in caplog.text

    def test_alias_collides_with_command_warns(self, tmp_path, caplog):
        """An alias that matches another skill's command name should warn."""
        skill_a = tmp_path / "core" / "skill_a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_a
            scope: core
            description: First skill
            group: status
            commands:
              - name: deploy
                description: Deploy
            ---
        """))

        skill_b = tmp_path / "core" / "skill_b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_b
            scope: core
            description: Second skill
            group: status
            commands:
              - name: ship
                description: Ship it
                aliases: [deploy]
            ---
        """))

        with caplog.at_level("WARNING"):
            SkillRegistry(tmp_path)

        assert "collides" in caplog.text
        assert "deploy" in caplog.text

    def test_same_skill_multiple_commands_no_warning(self, tmp_path, caplog):
        """A skill registering its own commands should never trigger a collision."""
        skill_a = tmp_path / "core" / "skill_a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_a
            scope: core
            description: Multi-command skill
            group: status
            commands:
              - name: start
                description: Start
              - name: stop
                description: Stop
            ---
        """))

        with caplog.at_level("WARNING"):
            SkillRegistry(tmp_path)

        assert "collides" not in caplog.text

    def test_no_collision_across_different_commands(self, tmp_path, caplog):
        """Skills with distinct commands should not warn."""
        skill_a = tmp_path / "core" / "skill_a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_a
            scope: core
            description: First
            group: status
            commands:
              - name: alpha
                description: Alpha
            ---
        """))

        skill_b = tmp_path / "core" / "skill_b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: skill_b
            scope: core
            description: Second
            group: status
            commands:
              - name: beta
                description: Beta
            ---
        """))

        with caplog.at_level("WARNING"):
            SkillRegistry(tmp_path)

        assert "collides" not in caplog.text

    def test_no_collision_on_real_core_skills(self, caplog):
        """Verify no alias/command collisions exist in shipped core skills."""
        from app.skills import get_default_skills_dir

        with caplog.at_level("WARNING", logger="app.skills"):
            SkillRegistry(get_default_skills_dir())

        collisions = [
            rec.message for rec in caplog.records if "collides" in rec.message
        ]
        assert not collisions, (
            f"Core skills have command/alias collisions:\n"
            + "\n".join(collisions)
        )
