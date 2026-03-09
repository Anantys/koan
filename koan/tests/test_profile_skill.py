"""Tests for the /profile core skill — handler and skill dispatch."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Import handler
# ---------------------------------------------------------------------------

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "profile" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("profile_handler", str(HANDLER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def ctx(tmp_path):
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    missions_md = instance_dir / "missions.md"
    missions_md.write_text("## Pending\n\n## In Progress\n\n## Done\n")
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="profile",
        args="",
        send_message=MagicMock(),
    )


# ---------------------------------------------------------------------------
# handle() — usage / routing
# ---------------------------------------------------------------------------

class TestHandleRouting:
    def test_no_args_returns_usage(self, handler, ctx):
        result = handler.handle(ctx)
        assert "Usage:" in result
        assert "/profile" in result

    def test_usage_shows_examples(self, handler, ctx):
        result = handler.handle(ctx)
        assert "project-name" in result or "pr-url" in result


# ---------------------------------------------------------------------------
# handle() — project name queuing
# ---------------------------------------------------------------------------

class TestProjectNameQueuing:
    def test_project_name_queues_mission(self, handler, ctx):
        ctx.args = "koan"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            result = handler.handle(ctx)
            assert "Profile queued" in result
            assert "koan" in result
            mock_insert.assert_called_once()

    def test_project_mission_entry_format(self, handler, ctx):
        ctx.args = "koan"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            handler.handle(ctx)
            entry = mock_insert.call_args[0][1]
            assert "[project:koan]" in entry
            assert "/profile" in entry

    def test_unknown_project_returns_error(self, handler, ctx):
        ctx.args = "nonexistent"
        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            result = handler.handle(ctx)
            assert "\u274c" in result
            assert "nonexistent" in result

    def test_unknown_project_lists_known(self, handler, ctx):
        ctx.args = "nonexistent"
        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan"), ("myapp", "/home/myapp")]):
            result = handler.handle(ctx)
            assert "koan" in result


# ---------------------------------------------------------------------------
# handle() — PR URL queuing
# ---------------------------------------------------------------------------

class TestPrUrlQueuing:
    def test_pr_url_queues_mission(self, handler, ctx):
        ctx.args = "https://github.com/sukria/koan/pull/42"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            result = handler.handle(ctx)
            assert "Profile queued" in result
            assert "PR" in result or "#42" in result
            mock_insert.assert_called_once()

    def test_pr_mission_contains_url(self, handler, ctx):
        ctx.args = "https://github.com/sukria/koan/pull/42"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            handler.handle(ctx)
            entry = mock_insert.call_args[0][1]
            assert "github.com/sukria/koan/pull/42" in entry

    def test_pr_url_in_text_extracted(self, handler, ctx):
        ctx.args = "please profile https://github.com/sukria/koan/pull/99 thanks"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            result = handler.handle(ctx)
            assert "Profile queued" in result


# ---------------------------------------------------------------------------
# SKILL.md — structure validation
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill_path = Path(__file__).parent.parent / "skills" / "core" / "profile" / "SKILL.md"
        skill = parse_skill_md(skill_path)
        assert skill is not None
        assert skill.name == "profile"
        assert skill.scope == "core"

    def test_skill_not_worker(self):
        from app.skills import parse_skill_md
        skill_path = Path(__file__).parent.parent / "skills" / "core" / "profile" / "SKILL.md"
        skill = parse_skill_md(skill_path)
        assert skill.worker is False

    def test_skill_has_aliases(self):
        from app.skills import parse_skill_md
        skill_path = Path(__file__).parent.parent / "skills" / "core" / "profile" / "SKILL.md"
        skill = parse_skill_md(skill_path)
        aliases = skill.commands[0].aliases
        assert "perf" in aliases
        assert "benchmark" in aliases

    def test_skill_registered_in_registry(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("profile")
        assert skill is not None
        assert skill.name == "profile"

    def test_alias_perf_registered(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("perf")
        assert skill is not None
        assert skill.name == "profile"

    def test_alias_benchmark_registered(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("benchmark")
        assert skill is not None
        assert skill.name == "profile"

    def test_skill_handler_exists(self):
        assert HANDLER_PATH.exists()

    def test_skill_github_enabled(self):
        from app.skills import parse_skill_md
        skill_path = Path(__file__).parent.parent / "skills" / "core" / "profile" / "SKILL.md"
        skill = parse_skill_md(skill_path)
        assert skill.github_enabled is True


# ---------------------------------------------------------------------------
# skill_dispatch — profile command building
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_profile_in_skill_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "profile" in _SKILL_RUNNERS

    def test_build_profile_cmd_basic(self):
        from app.skill_dispatch import build_skill_command
        cmd = build_skill_command(
            command="profile",
            args="",
            project_name="koan",
            project_path="/home/koan",
            koan_root="/koan-root",
            instance_dir="/instance",
        )
        assert cmd is not None
        assert "--project-path" in cmd
        assert "/home/koan" in cmd
        assert "--instance-dir" in cmd

    def test_build_profile_cmd_with_pr_url(self):
        from app.skill_dispatch import build_skill_command
        cmd = build_skill_command(
            command="profile",
            args="https://github.com/sukria/koan/pull/42",
            project_name="koan",
            project_path="/home/koan",
            koan_root="/koan-root",
            instance_dir="/instance",
        )
        assert cmd is not None
        assert "--pr-url" in cmd
        assert "https://github.com/sukria/koan/pull/42" in cmd

    def test_dispatch_profile_mission(self):
        from app.skill_dispatch import dispatch_skill_mission
        with patch("app.skill_dispatch.is_known_project", return_value=True):
            cmd = dispatch_skill_mission(
                mission_text="[project:koan] /profile",
                project_name="koan",
                project_path="/home/koan",
                koan_root="/koan-root",
                instance_dir="/instance",
            )
        assert cmd is not None
        assert "profile_runner" in " ".join(cmd) or "profile.profile_runner" in " ".join(cmd)
