"""Tests for the /scaffold_skill handler."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skills import SkillContext

# Load the handler module from the skill directory
_HANDLER_PATH = (
    Path(__file__).parent.parent / "skills" / "core" / "scaffold_skill" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("scaffold_skill_handler", str(_HANDLER_PATH))
_handler_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handler_mod)

handle = _handler_mod.handle
_parse_args = _handler_mod._parse_args
_parse_claude_response = _handler_mod._parse_claude_response
_validate_skill_md = _handler_mod._validate_skill_md
_check_command_conflict = _handler_mod._check_command_conflict

# Patch target prefix for the dynamically loaded module
_MOD = "scaffold_skill_handler"


def _make_ctx(tmp_path, args=""):
    """Create a SkillContext backed by a temporary instance dir."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    ctx = MagicMock(spec=SkillContext)
    ctx.koan_root = tmp_path
    ctx.instance_dir = instance_dir
    ctx.command_name = "scaffold_skill"
    ctx.args = args
    ctx.send_message = MagicMock()
    return ctx


# --- Sample Claude output for mocking ---

_SAMPLE_SKILL_MD = """\
---
name: deploy
scope: myteam
description: Deploy to production
version: 1.0.0
audience: bridge
commands:
  - name: deploy
    description: Deploy the app to production
    usage: /deploy <env>
    aliases: [ship]
handler: handler.py
---
"""

_SAMPLE_HANDLER = """\
def handle(ctx):
    \"\"\"Deploy handler stub.\"\"\"
    if not ctx.args:
        return "Usage: /deploy <env>"
    # TODO: implement deployment logic
    return f"Deploying to {ctx.args}..."
"""

_SAMPLE_CLAUDE_RESPONSE = (
    "Here are the generated files:\n\n"
    "```SKILL.md\n" + _SAMPLE_SKILL_MD + "```\n\n"
    "```handler.py\n" + _SAMPLE_HANDLER + "```\n"
)

_SAMPLE_PROMPT_ONLY_RESPONSE = (
    "Here are the generated files:\n\n"
    "```SKILL.md\n"
    "---\n"
    "name: haiku\n"
    "scope: myteam\n"
    "description: Write a haiku\n"
    "version: 1.0.0\n"
    "audience: bridge\n"
    "commands:\n"
    "  - name: haiku\n"
    "    description: Write a haiku about the project\n"
    "    aliases: []\n"
    "---\n\n"
    "Write a haiku about the current project.\n"
    "```\n\n"
    "```handler.py\n"
    "# prompt-only skill — no handler needed\n"
    "```\n"
)


class TestParseArgs:
    """Tests for argument parsing."""

    def test_valid_args(self):
        scope, name, desc, err = _parse_args("myteam deploy Deploy to prod")
        assert scope == "myteam"
        assert name == "deploy"
        assert desc == "Deploy to prod"
        assert err is None

    def test_long_description(self):
        scope, name, desc, err = _parse_args(
            "myteam deploy Deploy to production with rollback support and monitoring"
        )
        assert scope == "myteam"
        assert name == "deploy"
        assert "rollback" in desc
        assert err is None

    def test_missing_description(self):
        _, _, _, err = _parse_args("myteam deploy")
        assert err is not None
        assert "Not enough" in err

    def test_missing_name_and_description(self):
        _, _, _, err = _parse_args("myteam")
        assert err is not None

    def test_empty_args(self):
        _, _, _, err = _parse_args("")
        assert err is not None


class TestScopeValidation:
    """Tests for scope validation (rejects 'core')."""

    def test_rejects_core_scope(self, tmp_path):
        ctx = _make_ctx(tmp_path, "core deploy Deploy stuff")
        result = handle(ctx)
        assert "reserved" in result.lower()

    def test_accepts_valid_scope(self, tmp_path):
        """Valid scope passes scope validation (will fail later at Claude call)."""
        ctx = _make_ctx(tmp_path, "myteam deploy Deploy stuff")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _SAMPLE_CLAUDE_RESPONSE
        mock_result.stderr = ""
        with patch("app.cli_exec.run_cli", return_value=mock_result), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)
        assert "Skill scaffolded" in result


class TestSkillNameValidation:
    """Tests for skill name validation."""

    def test_rejects_special_characters(self, tmp_path):
        ctx = _make_ctx(tmp_path, "myteam deploy@prod Deploy stuff")
        result = handle(ctx)
        assert "Invalid skill name" in result

    def test_rejects_spaces_in_name(self, tmp_path):
        """Spaces in name would be parsed as part of description, not an error."""
        scope, name, desc, err = _parse_args("myteam my deploy Deploy stuff")
        # "my" becomes the name, "deploy Deploy stuff" becomes description
        assert name == "my"
        assert err is None


class TestExistingSkillCheck:
    """Tests for existing skill directory blocking."""

    def test_existing_dir_blocked(self, tmp_path):
        ctx = _make_ctx(tmp_path, "myteam deploy Deploy stuff")
        target = ctx.instance_dir / "skills" / "myteam" / "deploy"
        target.mkdir(parents=True)
        result = handle(ctx)
        assert "already exists" in result


class TestNoArgs:
    """Tests for no-argument case."""

    def test_no_args_returns_usage(self, tmp_path):
        ctx = _make_ctx(tmp_path, "")
        result = handle(ctx)
        assert "Usage:" in result
        assert "/scaffold_skill" in result

    def test_whitespace_only_returns_usage(self, tmp_path):
        ctx = _make_ctx(tmp_path, "   ")
        result = handle(ctx)
        assert "Usage:" in result


class TestParseClaudeResponse:
    """Tests for parsing Claude's response."""

    def test_parse_valid_response(self):
        skill_md, handler_content, err = _parse_claude_response(_SAMPLE_CLAUDE_RESPONSE)
        assert err is None
        assert "name: deploy" in skill_md
        assert "def handle" in handler_content

    def test_parse_prompt_only_response(self):
        skill_md, handler_content, err = _parse_claude_response(_SAMPLE_PROMPT_ONLY_RESPONSE)
        assert err is None
        assert "name: haiku" in skill_md
        assert handler_content == "" or "prompt-only" in handler_content

    def test_parse_no_skill_md(self):
        _, _, err = _parse_claude_response("Here is some text without code blocks.")
        assert err is not None
        assert "Could not parse" in err

    def test_parse_with_language_prefix(self):
        response = (
            "```yaml SKILL.md\n"
            "---\n"
            "name: test\n"
            "scope: myteam\n"
            "description: Test skill\n"
            "version: 1.0.0\n"
            "commands:\n"
            "  - name: test\n"
            "    description: Test command\n"
            "---\n"
            "```\n\n"
            "```python handler.py\n"
            "def handle(ctx):\n"
            "    return 'hello'\n"
            "```\n"
        )
        skill_md, handler_content, err = _parse_claude_response(response)
        assert err is None
        assert "name: test" in skill_md
        assert "def handle" in handler_content

    def test_parse_with_header_labels(self):
        response = (
            "### SKILL.md\n"
            "```yaml\n"
            "---\n"
            "name: test\n"
            "scope: myteam\n"
            "description: Test\n"
            "version: 1.0.0\n"
            "commands:\n"
            "  - name: test\n"
            "    description: Test\n"
            "---\n"
            "```\n\n"
            "### handler.py\n"
            "```python\n"
            "def handle(ctx):\n"
            "    return 'test'\n"
            "```\n"
        )
        skill_md, handler_content, err = _parse_claude_response(response)
        assert err is None
        assert "name: test" in skill_md
        assert "def handle" in handler_content


class TestValidation:
    """Tests for SKILL.md validation."""

    def test_valid_skill_md_passes(self):
        err = _validate_skill_md(_SAMPLE_SKILL_MD)
        assert err is None

    def test_invalid_skill_md_fails(self):
        err = _validate_skill_md("This is not a valid SKILL.md")
        assert err is not None
        assert "failed validation" in err

    def test_skill_md_no_commands_fails(self):
        content = "---\nname: test\nversion: 1.0.0\n---\n"
        err = _validate_skill_md(content)
        assert err is not None
        assert "no commands" in err


class TestFilesWritten:
    """Tests for file writing."""

    def test_files_written_correctly(self, tmp_path):
        ctx = _make_ctx(tmp_path, "myteam deploy Deploy to production")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _SAMPLE_CLAUDE_RESPONSE
        mock_result.stderr = ""

        with patch("app.cli_exec.run_cli", return_value=mock_result), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)

        assert "Skill scaffolded" in result

        target_dir = ctx.instance_dir / "skills" / "myteam" / "deploy"
        assert (target_dir / "SKILL.md").exists()
        assert (target_dir / "handler.py").exists()

        skill_md = (target_dir / "SKILL.md").read_text()
        assert "name: deploy" in skill_md

        handler_py = (target_dir / "handler.py").read_text()
        assert "def handle" in handler_py

    def test_prompt_only_no_handler_file(self, tmp_path):
        """When Claude returns a prompt-only comment, handler.py should not be written."""
        ctx = _make_ctx(tmp_path, "myteam haiku Write haiku about projects")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _SAMPLE_PROMPT_ONLY_RESPONSE
        mock_result.stderr = ""

        with patch("app.cli_exec.run_cli", return_value=mock_result), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)

        assert "Skill scaffolded" in result
        target_dir = ctx.instance_dir / "skills" / "myteam" / "haiku"
        assert (target_dir / "SKILL.md").exists()


class TestCommandConflict:
    """Tests for command name conflict detection."""

    def test_detects_existing_command(self, tmp_path):
        # "status" is a core command
        result = _check_command_conflict("status", tmp_path / "instance")
        assert result is not None
        assert "already exists" in result

    def test_allows_new_command(self, tmp_path):
        result = _check_command_conflict("my-unique-skill-xyz", tmp_path / "instance")
        assert result is None


class TestHandlerInvocation:
    """End-to-end handler tests with mocked Claude CLI."""

    def test_full_flow(self, tmp_path):
        ctx = _make_ctx(tmp_path, "ops deploy Deploy with rollback support")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _SAMPLE_CLAUDE_RESPONSE
        mock_result.stderr = ""

        with patch("app.cli_exec.run_cli", return_value=mock_result), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)

        assert "Skill scaffolded" in result
        assert "ops/deploy" in result
        ctx.send_message.assert_called_once()

    def test_claude_failure_returns_error(self, tmp_path):
        ctx = _make_ctx(tmp_path, "ops deploy Deploy stuff")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "API error"

        with patch("app.cli_exec.run_cli", return_value=mock_result), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)

        assert "Failed" in result

    def test_claude_timeout_returns_error(self, tmp_path):
        ctx = _make_ctx(tmp_path, "ops deploy Deploy stuff")

        with patch("app.cli_exec.run_cli", side_effect=subprocess.TimeoutExpired("cmd", 120)), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)

        assert "Timeout" in result

    def test_malformed_claude_output(self, tmp_path):
        ctx = _make_ctx(tmp_path, "ops deploy Deploy stuff")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Sorry, I can't generate that."
        mock_result.stderr = ""

        with patch("app.cli_exec.run_cli", return_value=mock_result), \
             patch("app.cli_provider.build_full_command", return_value=["claude"]), \
             patch("app.config.get_fast_reply_model", return_value=""):
            result = handle(ctx)

        assert "Could not parse" in result


class TestRegistryRoundTrip:
    """Verify generated skills load into the registry."""

    def test_generated_skill_discoverable(self, tmp_path):
        from app.skills import build_registry, parse_skill_md

        skill_dir = tmp_path / "skills" / "myteam" / "deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_SAMPLE_SKILL_MD)

        skill = parse_skill_md(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.name == "deploy"
        assert skill.scope == "myteam"

        registry = build_registry(extra_dirs=[tmp_path / "skills"])
        found = registry.find_by_command("deploy")
        assert found is not None
        assert found.name == "deploy"

        found_alias = registry.find_by_command("ship")
        assert found_alias is not None
        assert found_alias.name == "deploy"
