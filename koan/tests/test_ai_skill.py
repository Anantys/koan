"""Tests for the /ai core skill — mission-queuing AI exploration."""

import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext, build_registry


# ---------------------------------------------------------------------------
# Import handler module
# ---------------------------------------------------------------------------

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "ai" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("ai_handler", str(HANDLER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def ctx(tmp_path):
    """Create a basic SkillContext for tests."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    # Create minimal missions.md
    missions_md = instance_dir / "missions.md"
    missions_md.write_text("## Pending\n\n## In Progress\n\n## Done\n")
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="ai",
        args="",
        send_message=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Registry / routing
# ---------------------------------------------------------------------------

class TestAiSkillRegistry:
    def test_ai_skill_not_worker(self):
        """AI skill should NOT have worker=true (it queues, not runs)."""
        registry = build_registry()
        skill = registry.get("core", "ai")
        assert skill is not None
        assert skill.worker is False

    def test_ai_found_by_command(self):
        """Registry should find ai skill by /ai."""
        registry = build_registry()
        skill = registry.find_by_command("ai")
        assert skill is not None
        assert skill.name == "ai"

    def test_ia_alias_routes_to_ai(self):
        """Registry should find ai skill by /ia alias."""
        registry = build_registry()
        skill = registry.find_by_command("ia")
        assert skill is not None
        assert skill.name == "ai"

    def test_magic_routes_to_magic_not_ai(self):
        """Registry should find magic skill by /magic (separate skill, not alias)."""
        registry = build_registry()
        skill = registry.find_by_command("magic")
        assert skill is not None
        assert skill.name == "magic"


# ---------------------------------------------------------------------------
# SKILL.md — structure validation
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "ai" / "SKILL.md"
        )
        assert skill is not None
        assert skill.name == "ai"
        assert skill.scope == "core"
        assert skill.worker is False

    def test_skill_has_ia_alias(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "ai" / "SKILL.md"
        )
        assert "ia" in skill.commands[0].aliases

    def test_skill_does_not_have_magic_alias(self):
        """Magic is a separate skill now, not an alias of /ai."""
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "ai" / "SKILL.md"
        )
        assert "magic" not in skill.commands[0].aliases

    def test_skill_handler_exists(self):
        assert HANDLER_PATH.exists()

    def test_prompt_file_exists(self):
        prompt_path = (
            Path(__file__).parent.parent
            / "skills" / "core" / "ai" / "prompts" / "ai-explore.md"
        )
        assert prompt_path.exists()


# ---------------------------------------------------------------------------
# Magic SKILL.md — separate instant skill
# ---------------------------------------------------------------------------

class TestMagicSkillMd:
    def test_magic_skill_md_exists(self):
        magic_md = (
            Path(__file__).parent.parent
            / "skills" / "core" / "magic" / "SKILL.md"
        )
        assert magic_md.exists()

    def test_magic_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "magic" / "SKILL.md"
        )
        assert skill is not None
        assert skill.name == "magic"
        assert skill.worker is True

    def test_magic_handler_exists(self):
        handler_path = (
            Path(__file__).parent.parent
            / "skills" / "core" / "magic" / "handler.py"
        )
        assert handler_path.exists()


# ---------------------------------------------------------------------------
# _get_projects
# ---------------------------------------------------------------------------

class TestGetProjects:
    @patch("app.utils.get_known_projects")
    def test_returns_projects_from_yaml(self, mock_get, handler, ctx, tmp_path):
        mock_get.return_value = [("foo", str(tmp_path)), ("bar", "/nonexistent")]
        projects = handler.get_projects()
        assert len(projects) == 1
        assert projects[0][0] == "foo"

    @patch("app.utils.get_known_projects", side_effect=Exception("no yaml"))
    def test_returns_empty_on_exception(self, mock_get, handler, ctx, monkeypatch):
        """When get_known_projects raises, returns empty list."""
        projects = handler.get_projects()
        assert projects == []


# ---------------------------------------------------------------------------
# _resolve_project
# ---------------------------------------------------------------------------

class TestResolveProject:
    def test_no_target_picks_random(self, handler):
        projects = [("koan", "/koan"), ("web", "/web")]
        name, path = handler._resolve_project(projects, "")
        assert name in ("koan", "web")

    def test_target_matches_exact(self, handler):
        projects = [("koan", "/koan"), ("web", "/web")]
        name, path = handler._resolve_project(projects, "koan")
        assert name == "koan"
        assert path == "/koan"

    def test_target_case_insensitive(self, handler):
        projects = [("Koan", "/koan")]
        name, path = handler._resolve_project(projects, "koan")
        assert name == "Koan"

    def test_target_not_found(self, handler):
        projects = [("koan", "/koan")]
        name, path = handler._resolve_project(projects, "unknown")
        assert name is None
        assert path is None


# ---------------------------------------------------------------------------
# handle() — main entry point
# ---------------------------------------------------------------------------

class TestHandle:
    @patch("app.utils.get_known_projects", return_value=[])
    def test_no_projects_returns_message(self, mock_get, handler, ctx):
        result = handler.handle(ctx)
        assert "No projects" in result

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_queues_mission_for_random_project(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        mock_get.return_value = [("test", str(tmp_path))]
        result = handler.handle(ctx)
        assert "queued" in result.lower()
        assert "test" in result
        mock_insert.assert_called_once()

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_queues_mission_for_specific_project(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        mock_get.return_value = [("koan", str(tmp_path)), ("web", str(tmp_path))]
        ctx.args = "koan"
        result = handler.handle(ctx)
        assert "queued" in result.lower()
        assert "koan" in result
        mock_insert.assert_called_once()
        mission_entry = mock_insert.call_args[0][1]
        assert "[project:koan]" in mission_entry

    @patch("app.utils.get_known_projects")
    def test_unknown_project_returns_error(self, mock_get, handler, ctx, tmp_path):
        mock_get.return_value = [("koan", str(tmp_path))]
        ctx.args = "unknown"
        result = handler.handle(ctx)
        assert "Unknown project" in result
        assert "koan" in result

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_mission_entry_has_project_tag(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        mock_get.return_value = [("myproject", str(tmp_path))]
        handler.handle(ctx)
        entry = mock_insert.call_args[0][1]
        assert "[project:myproject]" in entry

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_mission_entry_uses_clean_skill_format(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        """Mission entry uses clean /ai format (no run: command)."""
        mock_get.return_value = [("test", str(tmp_path))]
        handler.handle(ctx)
        entry = mock_insert.call_args[0][1]
        assert "/ai test" in entry
        assert "run:" not in entry
        assert "python3 -m" not in entry

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_missions_path_uses_instance_dir(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        mock_get.return_value = [("test", str(tmp_path))]
        handler.handle(ctx)
        missions_path = mock_insert.call_args[0][0]
        assert missions_path == ctx.instance_dir / "missions.md"

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_ia_alias_works(self, mock_insert, mock_get, handler, ctx, tmp_path):
        """The /ia alias should work the same as /ai."""
        mock_get.return_value = [("test", str(tmp_path))]
        ctx.command_name = "ia"
        result = handler.handle(ctx)
        assert "queued" in result.lower()
        mock_insert.assert_called_once()

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_does_not_call_claude_directly(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        """The handler should queue, not call Claude subprocess."""
        mock_get.return_value = [("test", str(tmp_path))]
        with patch("subprocess.run") as mock_subprocess:
            handler.handle(ctx)
            # No subprocess calls at all — handler is a pure queuer
            mock_subprocess.assert_not_called()

    @patch("app.utils.get_known_projects")
    @patch("app.utils.insert_pending_mission")
    def test_does_not_inline_prompt(
        self, mock_insert, mock_get, handler, ctx, tmp_path
    ):
        """Mission entry should NOT contain the full prompt text inline."""
        mock_get.return_value = [("test", str(tmp_path))]
        handler.handle(ctx)
        entry = mock_insert.call_args[0][1]
        # Should not contain the long prompt template markers
        assert "Dive deep into the codebase" not in entry
        assert "3-5 concrete" not in entry


# ---------------------------------------------------------------------------
# Dispatch behavior via command_handlers
# ---------------------------------------------------------------------------

class TestDispatch:
    @patch("app.command_handlers._run_in_worker_cb")
    @patch("app.command_handlers.send_telegram")
    def test_ai_not_dispatched_via_worker(self, mock_send, mock_worker):
        """handle_command('/ai') should NOT use worker thread (not a worker skill)."""
        from app.command_handlers import handle_command
        handle_command("/ai")
        mock_worker.assert_not_called()
        mock_send.assert_called()

    @patch("app.command_handlers._run_in_worker_cb")
    @patch("app.command_handlers.send_telegram")
    def test_ia_not_dispatched_via_worker(self, mock_send, mock_worker):
        """handle_command('/ia') should NOT use worker thread."""
        from app.command_handlers import handle_command
        handle_command("/ia")
        mock_worker.assert_not_called()
        mock_send.assert_called()


# ---------------------------------------------------------------------------
# /help integration
# ---------------------------------------------------------------------------

class TestHelpIntegration:
    @patch("app.command_handlers.send_telegram")
    def test_help_mentions_ai(self, mock_send):
        from app.command_handlers import _handle_help
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/ai" in msg or "ai" in msg.lower()
