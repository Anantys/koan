"""Tests for skill_dispatch.py — skill mission detection and CLI command building."""

import os
import pytest

from app.skill_dispatch import (
    is_skill_mission,
    parse_skill_mission,
    build_skill_command,
    dispatch_skill_mission,
    validate_skill_args,
)


# ---------------------------------------------------------------------------
# is_skill_mission
# ---------------------------------------------------------------------------

class TestIsSkillMission:
    def test_plan_command(self):
        assert is_skill_mission("/plan Add dark mode") is True

    def test_rebase_command(self):
        assert is_skill_mission("/rebase https://github.com/sukria/koan/pull/42") is True

    def test_scoped_command(self):
        assert is_skill_mission("/core.plan Add dark mode") is True

    def test_empty_string(self):
        assert is_skill_mission("") is False

    def test_regular_mission(self):
        assert is_skill_mission("Fix the login bug") is False

    def test_run_command_old_format(self):
        assert is_skill_mission("Plan: stuff — run: `cd /koan && python3 -m ...`") is False

    def test_slash_only(self):
        assert is_skill_mission("/") is False

    def test_whitespace_before_slash(self):
        assert is_skill_mission("  /plan test") is True

    def test_not_at_start(self):
        assert is_skill_mission("Fix /plan bug") is False


# ---------------------------------------------------------------------------
# parse_skill_mission
# ---------------------------------------------------------------------------

class TestParseSkillMission:
    def test_simple_plan(self):
        pid, cmd, args = parse_skill_mission("/plan Add dark mode")
        assert pid == ""
        assert cmd == "plan"
        assert args == "Add dark mode"

    def test_rebase_url(self):
        pid, cmd, args = parse_skill_mission("/rebase https://github.com/sukria/koan/pull/42")
        assert pid == ""
        assert cmd == "rebase"
        assert args == "https://github.com/sukria/koan/pull/42"

    def test_ai_no_args(self):
        pid, cmd, args = parse_skill_mission("/ai")
        assert pid == ""
        assert cmd == "ai"
        assert args == ""

    def test_ai_with_project(self):
        pid, cmd, args = parse_skill_mission("/ai koan")
        assert pid == ""
        assert cmd == "ai"
        assert args == "koan"

    def test_scoped_core(self):
        """core.plan should resolve to just 'plan'."""
        pid, cmd, args = parse_skill_mission("/core.plan Add dark mode")
        assert pid == ""
        assert cmd == "plan"
        assert args == "Add dark mode"

    def test_scoped_external(self):
        """External scoped skills keep full scope."""
        pid, cmd, args = parse_skill_mission("/anantys.review Check code")
        assert pid == ""
        assert cmd == "anantys.review"
        assert args == "Check code"

    def test_claudemd(self):
        pid, cmd, args = parse_skill_mission("/claudemd koan")
        assert pid == ""
        assert cmd == "claudemd"
        assert args == "koan"

    def test_claude_md_dot_alias(self):
        """The old /claude.md form is parsed via dot-scope logic (core.md -> md)."""
        pid, cmd, args = parse_skill_mission("/claude.md koan")
        assert pid == ""
        # dot in command triggers scope logic: scope=claude, skill=md
        assert cmd == "claude.md"
        assert args == "koan"

    def test_no_slash(self):
        pid, cmd, args = parse_skill_mission("Fix the bug")
        assert pid == ""
        assert cmd == ""
        assert args == "Fix the bug"

    def test_check_with_url(self):
        pid, cmd, args = parse_skill_mission("/check https://github.com/sukria/koan/issues/42")
        assert pid == ""
        assert cmd == "check"
        assert args == "https://github.com/sukria/koan/issues/42"

    def test_recreate_with_url(self):
        pid, cmd, args = parse_skill_mission("/recreate https://github.com/sukria/koan/pull/100")
        assert pid == ""
        assert cmd == "recreate"
        assert args == "https://github.com/sukria/koan/pull/100"


# ---------------------------------------------------------------------------
# build_skill_command
# ---------------------------------------------------------------------------

class TestBuildSkillCommand:
    KOAN_ROOT = "/home/user/koan"
    INSTANCE = "/home/user/koan/instance"
    PROJECT = "myproject"
    PROJECT_PATH = "/home/user/workspace/myproject"

    def _build(self, command, args):
        return build_skill_command(
            command=command,
            args=args,
            project_name=self.PROJECT,
            project_path=self.PROJECT_PATH,
            koan_root=self.KOAN_ROOT,
            instance_dir=self.INSTANCE,
        )

    def test_plan_with_idea(self):
        cmd = self._build("plan", "Add dark mode to the dashboard")
        assert cmd is not None
        assert "-m" in cmd
        assert "app.plan_runner" in cmd
        assert "--project-path" in cmd
        assert self.PROJECT_PATH in cmd
        assert "--idea" in cmd
        assert "Add dark mode to the dashboard" in cmd

    def test_plan_with_issue_url(self):
        url = "https://github.com/sukria/koan/issues/42"
        cmd = self._build("plan", url)
        assert cmd is not None
        assert "--issue-url" in cmd
        assert url in cmd

    def test_plan_with_issue_url_and_context(self):
        args = "https://github.com/sukria/koan/issues/42 Focus on phase 2"
        cmd = self._build("plan", args)
        assert cmd is not None
        assert "--issue-url" in cmd
        assert "https://github.com/sukria/koan/issues/42" in cmd
        assert "--context" in cmd
        ctx_idx = cmd.index("--context")
        assert cmd[ctx_idx + 1] == "Focus on phase 2"

    def test_plan_with_issue_url_no_context(self):
        """Issue URL with no trailing text should not include --context."""
        url = "https://github.com/sukria/koan/issues/42"
        cmd = self._build("plan", url)
        assert "--context" not in cmd

    def test_plan_with_pr_url(self):
        """PR URLs should be handled as --issue-url, not --idea."""
        url = "https://github.com/metacpan/metacpan-grep-front-end/pull/75"
        cmd = self._build("plan", url)
        assert cmd is not None
        assert "--issue-url" in cmd
        assert url in cmd
        assert "--idea" not in cmd

    def test_plan_with_pr_url_and_fragment(self):
        """PR URL with #issuecomment fragment should match."""
        url = "https://github.com/owner/repo/pull/75#issuecomment-123"
        cmd = self._build("plan", url)
        assert cmd is not None
        assert "--issue-url" in cmd
        # Fragment is included in the URL passed to plan_runner
        assert "https://github.com/owner/repo/pull/75" in " ".join(cmd)

    def test_plan_with_pr_url_and_context(self):
        """PR URL with trailing context should pass --context."""
        args = "https://github.com/owner/repo/pull/75 focus on security"
        cmd = self._build("plan", args)
        assert cmd is not None
        assert "--issue-url" in cmd
        assert "--context" in cmd
        ctx_idx = cmd.index("--context")
        assert cmd[ctx_idx + 1] == "focus on security"

    def test_rebase(self):
        url = "https://github.com/sukria/koan/pull/42"
        cmd = self._build("rebase", url)
        assert cmd is not None
        assert "app.rebase_pr" in cmd
        assert url in cmd
        assert "--project-path" in cmd

    def test_rebase_no_url(self):
        cmd = self._build("rebase", "just some text")
        assert cmd is None

    def test_recreate(self):
        url = "https://github.com/sukria/koan/pull/100"
        cmd = self._build("recreate", url)
        assert cmd is not None
        assert "app.recreate_pr" in cmd
        assert url in cmd

    def test_recreate_no_url(self):
        cmd = self._build("recreate", "no url here")
        assert cmd is None

    def test_ai(self):
        cmd = self._build("ai", "koan")
        assert cmd is not None
        assert "app.ai_runner" in cmd
        assert "--project-path" in cmd
        assert "--project-name" in cmd
        assert "--instance-dir" in cmd
        assert self.PROJECT_PATH in cmd
        assert self.PROJECT in cmd
        assert self.INSTANCE in cmd

    def test_check_pr(self):
        url = "https://github.com/sukria/koan/pull/85"
        cmd = self._build("check", url)
        assert cmd is not None
        assert "app.check_runner" in cmd
        assert url in cmd
        assert "--instance-dir" in cmd
        assert "--koan-root" in cmd

    def test_check_issue(self):
        url = "https://github.com/sukria/koan/issues/42"
        cmd = self._build("check", url)
        assert cmd is not None
        assert "app.check_runner" in cmd
        assert url in cmd

    def test_check_no_url(self):
        cmd = self._build("check", "no url here")
        assert cmd is None

    def test_claudemd(self):
        cmd = self._build("claudemd", "koan")
        assert cmd is not None
        assert "app.claudemd_refresh" in cmd
        assert self.PROJECT_PATH in cmd
        assert "--project-name" in cmd

    def test_claude_alias(self):
        cmd = self._build("claude", "koan")
        assert cmd is not None
        assert "app.claudemd_refresh" in cmd

    def test_claude_dot_md_alias(self):
        cmd = self._build("claude.md", "koan")
        assert cmd is not None
        assert "app.claudemd_refresh" in cmd

    def test_claude_underscore_md_alias(self):
        cmd = self._build("claude_md", "koan")
        assert cmd is not None
        assert "app.claudemd_refresh" in cmd

    def test_unknown_skill(self):
        cmd = self._build("unknown_skill", "args")
        assert cmd is None

    def test_implement_with_issue_url(self):
        url = "https://github.com/sukria/koan/issues/42"
        cmd = self._build("implement", url)
        assert cmd is not None
        assert "skills.core.implement.implement_runner" in cmd
        assert "--issue-url" in cmd
        assert url in cmd
        assert "--project-path" in cmd

    def test_implement_with_context(self):
        url = "https://github.com/sukria/koan/issues/42"
        cmd = self._build("implement", f"{url} Phase 1 to 3")
        assert cmd is not None
        assert "--issue-url" in cmd
        assert url in cmd
        assert "--context" in cmd
        assert "Phase 1 to 3" in cmd

    def test_implement_no_url(self):
        cmd = self._build("implement", "just some text")
        assert cmd is None

    def test_implement_url_only_no_context(self):
        url = "https://github.com/sukria/koan/issues/99"
        cmd = self._build("implement", url)
        assert "--context" not in cmd

    def test_fix_with_issue_url(self):
        url = "https://github.com/Anantys/investmindr/issues/42"
        cmd = self._build("fix", url)
        assert cmd is not None
        assert "skills.core.fix.fix_runner" in cmd
        assert "--issue-url" in cmd
        assert url in cmd
        assert "--project-path" in cmd

    def test_fix_with_context(self):
        url = "https://github.com/Anantys/investmindr/issues/42"
        cmd = self._build("fix", f"{url} backend only")
        assert cmd is not None
        assert "--issue-url" in cmd
        assert url in cmd
        assert "--context" in cmd
        assert "backend only" in cmd

    def test_fix_no_url(self):
        cmd = self._build("fix", "just fix the login bug")
        assert cmd is None

    def test_fix_url_only_no_context(self):
        url = "https://github.com/Anantys/investmindr/issues/99"
        cmd = self._build("fix", url)
        assert "--context" not in cmd

    def test_python_path(self):
        """Commands should use sys.executable (works in venv and Docker)."""
        import sys
        cmd = self._build("plan", "test idea")
        assert cmd[0] == sys.executable


# ---------------------------------------------------------------------------
# dispatch_skill_mission (high-level integration)
# ---------------------------------------------------------------------------

class TestDispatchSkillMission:
    KOAN_ROOT = "/home/user/koan"
    INSTANCE = "/home/user/koan/instance"

    def _dispatch(self, mission_text, project_name="koan", project_path="/home/user/workspace/koan"):
        return dispatch_skill_mission(
            mission_text=mission_text,
            project_name=project_name,
            project_path=project_path,
            koan_root=self.KOAN_ROOT,
            instance_dir=self.INSTANCE,
        )

    def test_plan_dispatch(self):
        cmd = self._dispatch("/plan Add dark mode")
        assert cmd is not None
        assert "app.plan_runner" in cmd

    def test_rebase_dispatch(self):
        cmd = self._dispatch("/rebase https://github.com/sukria/koan/pull/42")
        assert cmd is not None
        assert "app.rebase_pr" in cmd

    def test_recreate_dispatch(self):
        cmd = self._dispatch("/recreate https://github.com/sukria/koan/pull/42")
        assert cmd is not None
        assert "app.recreate_pr" in cmd

    def test_ai_dispatch(self):
        cmd = self._dispatch("/ai koan")
        assert cmd is not None
        assert "app.ai_runner" in cmd

    def test_check_dispatch(self):
        cmd = self._dispatch("/check https://github.com/sukria/koan/issues/42")
        assert cmd is not None
        assert "app.check_runner" in cmd

    def test_claudemd_dispatch(self):
        cmd = self._dispatch("/claudemd koan")
        assert cmd is not None
        assert "app.claudemd_refresh" in cmd

    def test_implement_dispatch(self):
        cmd = self._dispatch("/implement https://github.com/sukria/koan/issues/42")
        assert cmd is not None
        assert "skills.core.implement.implement_runner" in cmd
        assert "--issue-url" in cmd

    def test_implement_dispatch_with_context(self):
        cmd = self._dispatch("/implement https://github.com/sukria/koan/issues/42 Phase 1 to 3")
        assert cmd is not None
        assert "skills.core.implement.implement_runner" in cmd
        assert "--context" in cmd
        assert "Phase 1 to 3" in cmd

    def test_regular_mission_returns_none(self):
        cmd = self._dispatch("Fix the login bug")
        assert cmd is None

    def test_old_run_format_returns_none(self):
        cmd = self._dispatch("Plan: stuff — run: `cd /koan && python3 -m app.plan_runner ...`")
        assert cmd is None

    def test_scoped_core_dispatch(self):
        cmd = self._dispatch("/core.plan Fix the bug")
        assert cmd is not None
        assert "app.plan_runner" in cmd

    def test_unknown_skill_returns_none(self):
        cmd = self._dispatch("/nonexistent do things")
        assert cmd is None

    def test_empty_returns_none(self):
        cmd = self._dispatch("")
        assert cmd is None


# ---------------------------------------------------------------------------
# translate_cli_skill_mission
# ---------------------------------------------------------------------------

import textwrap
from pathlib import Path


def _make_cli_skill(tmp_path: Path, scope: str, name: str, cli_skill_value: str) -> Path:
    """Create a minimal SKILL.md with cli_skill set and return the skills root dir."""
    skill_dir = tmp_path / "skills" / scope / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        scope: {scope}
        description: Bridge to {cli_skill_value}
        audience: agent
        cli_skill: {cli_skill_value}
        commands:
          - name: {name}
            description: Invoke /{cli_skill_value}
        ---
    """))
    return tmp_path / "skills"


class TestTranslateCliSkillMission:
    """Tests for translate_cli_skill_mission()."""

    def _translate(self, mission_text, skills_root=None, instance_dir=None, tmp_path=None):
        import app.skill_dispatch as _sd_mod
        from app.skill_dispatch import translate_cli_skill_mission

        koan_root = tmp_path or Path("/tmp/koan")
        inst = instance_dir or (tmp_path / "instance" if tmp_path else Path("/tmp/koan/instance"))

        # Clear registry cache so each test builds a fresh registry.
        _sd_mod._cached_registry = None
        _sd_mod._cached_extra_dirs = None

        if skills_root:
            # Patch build_registry to load from our tmp skills dir
            import app.skills as _skills_mod
            orig = _skills_mod.get_default_skills_dir
            _skills_mod.get_default_skills_dir = lambda: skills_root
            try:
                return translate_cli_skill_mission(mission_text, koan_root, inst)
            finally:
                _sd_mod._cached_registry = None
                _skills_mod.get_default_skills_dir = orig

        return translate_cli_skill_mission(mission_text, koan_root, inst)

    def test_translates_scoped_cli_skill(self, tmp_path):
        """A scoped /group.myskill mission with cli_skill set is translated."""
        skills_root = _make_cli_skill(tmp_path, "group", "myskill", "my-tool")
        result = self._translate(
            "[project:foo] /group.myskill do something",
            skills_root=skills_root,
            tmp_path=tmp_path,
        )
        assert result == "/my-tool do something"

    def test_translates_no_project_prefix(self, tmp_path):
        """Works without [project:X] prefix."""
        skills_root = _make_cli_skill(tmp_path, "ops", "deploy", "deploy-tool")
        result = self._translate(
            "/ops.deploy staging",
            skills_root=skills_root,
            tmp_path=tmp_path,
        )
        assert result == "/deploy-tool staging"

    def test_no_args_still_translates(self, tmp_path):
        """Skills with no args produce just the slash command."""
        skills_root = _make_cli_skill(tmp_path, "grp", "check", "run-check")
        result = self._translate(
            "/grp.check",
            skills_root=skills_root,
            tmp_path=tmp_path,
        )
        assert result == "/run-check"

    def test_returns_none_for_regular_mission(self, tmp_path):
        """A non-slash mission returns None."""
        result = self._translate("Fix the login bug", tmp_path=tmp_path)
        assert result is None

    def test_returns_none_for_unscoped_command(self, tmp_path):
        """Unscoped commands (/plan) are not handled by translate_cli_skill_mission."""
        result = self._translate("/plan Add dark mode", tmp_path=tmp_path)
        assert result is None

    def test_returns_none_for_core_scope(self, tmp_path):
        """Core scope is reserved for _SKILL_RUNNERS — not translated."""
        skills_root = _make_cli_skill(tmp_path, "core", "plan", "some-tool")
        result = self._translate(
            "/core.plan Add dark mode",
            skills_root=skills_root,
            tmp_path=tmp_path,
        )
        assert result is None

    def test_returns_none_when_skill_not_found(self, tmp_path):
        """Returns None if the skill doesn't exist in the registry."""
        result = self._translate("/unknown.skill do something", tmp_path=tmp_path)
        assert result is None

    def test_returns_none_when_no_cli_skill_field(self, tmp_path):
        """Returns None if the skill exists but has no cli_skill field."""
        skill_dir = tmp_path / "skills" / "grp" / "normal"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: normal
            scope: grp
            audience: bridge
            commands:
              - name: normal
                description: Normal skill
            ---
        """))
        skills_root = tmp_path / "skills"

        import app.skills as _skills_mod
        orig = _skills_mod.get_default_skills_dir
        _skills_mod.get_default_skills_dir = lambda: skills_root
        try:
            from app.skill_dispatch import translate_cli_skill_mission
            result = translate_cli_skill_mission(
                "/grp.normal do something",
                tmp_path,
                tmp_path / "instance",
            )
        finally:
            _skills_mod.get_default_skills_dir = orig

        assert result is None


# ---------------------------------------------------------------------------
# Handler integration tests — verify handlers produce clean format
# ---------------------------------------------------------------------------

class TestHandlerCleanFormat:
    """Verify that updated handlers produce /skill format missions."""

    def _make_ctx(self, args="", instance_dir=None, koan_root="/koan"):
        from unittest.mock import MagicMock
        from pathlib import Path

        ctx = MagicMock()
        ctx.args = args
        ctx.koan_root = Path(koan_root)
        ctx.instance_dir = instance_dir or Path("/tmp/test-instance")
        return ctx

    def test_plan_handler_clean_format(self, tmp_path, monkeypatch):
        """Plan handler should produce /plan format, not run: format."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )

        from skills.core.plan.handler import handle
        ctx = self._make_ctx(args="Add dark mode", instance_dir=tmp_path)
        result = handle(ctx)

        assert "queued" in result.lower() or "Plan queued" in result
        content = missions_file.read_text()
        assert "/plan Add dark mode" in content
        assert "run:" not in content
        assert "python3 -m" not in content

    def test_rebase_handler_clean_format(self, tmp_path, monkeypatch):
        """Rebase handler should produce /rebase format."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        monkeypatch.setattr(
            "app.utils.resolve_project_path",
            lambda repo, owner=None: "/workspace/koan",
        )
        monkeypatch.setattr(
            "app.github_url_parser.parse_pr_url",
            lambda url: ("sukria", "koan", "42"),
        )

        from skills.core.rebase.handler import handle
        ctx = self._make_ctx(
            args="https://github.com/sukria/koan/pull/42",
            instance_dir=tmp_path,
        )
        result = handle(ctx)

        assert "queued" in result.lower()
        content = missions_file.read_text()
        assert "/rebase https://github.com/sukria/koan/pull/42" in content
        assert "run:" not in content

    def test_ai_handler_clean_format(self, tmp_path, monkeypatch):
        """AI handler should produce /ai format."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", str(tmp_path))],
        )

        from skills.core.ai.handler import handle
        ctx = self._make_ctx(args="koan", instance_dir=tmp_path)
        result = handle(ctx)

        assert "queued" in result.lower()
        content = missions_file.read_text()
        assert "/ai koan" in content
        assert "run:" not in content

    def test_check_handler_clean_format(self, tmp_path, monkeypatch):
        """Check handler should produce /check format."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )

        from skills.core.check.handler import handle
        ctx = self._make_ctx(
            args="https://github.com/sukria/koan/pull/85",
            instance_dir=tmp_path,
        )
        result = handle(ctx)

        assert "queued" in result.lower()
        content = missions_file.read_text()
        assert "/check https://github.com/sukria/koan/pull/85" in content
        assert "run:" not in content

    def test_claudemd_handler_clean_format(self, tmp_path, monkeypatch):
        """Claudemd handler should produce /claudemd format."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )

        from skills.core.claudemd.handler import handle
        ctx = self._make_ctx(args="koan", instance_dir=tmp_path)
        result = handle(ctx)

        assert "queued" in result.lower()
        content = missions_file.read_text()
        assert "/claudemd koan" in content
        assert "run:" not in content

    def test_recreate_handler_clean_format(self, tmp_path, monkeypatch):
        """Recreate handler should produce /recreate format."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        monkeypatch.setattr(
            "app.utils.resolve_project_path",
            lambda repo, owner=None: "/workspace/koan",
        )
        monkeypatch.setattr(
            "app.github_url_parser.parse_pr_url",
            lambda url: ("sukria", "koan", "42"),
        )

        from skills.core.recreate.handler import handle
        ctx = self._make_ctx(
            args="https://github.com/sukria/koan/pull/42",
            instance_dir=tmp_path,
        )
        result = handle(ctx)

        assert "queued" in result.lower()
        content = missions_file.read_text()
        assert "/recreate https://github.com/sukria/koan/pull/42" in content
        assert "run:" not in content


# ---------------------------------------------------------------------------
# is_skill_mission — project-id prefix handling
# ---------------------------------------------------------------------------

class TestIsSkillMissionWithPrefix:
    def test_project_tag_prefix(self):
        assert is_skill_mission("[project:koan] /plan Add dark mode") is True

    def test_projet_tag_prefix(self):
        assert is_skill_mission("[projet:koan] /plan Add dark mode") is True

    def test_raw_project_word_prefix(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        assert is_skill_mission("koan /plan Add dark mode") is True

    def test_project_tag_no_slash(self):
        """Tag prefix but no /command — not a skill mission."""
        assert is_skill_mission("[project:koan] Fix the bug") is False

    def test_raw_word_no_slash(self):
        """Word prefix but second word doesn't start with /."""
        assert is_skill_mission("koan Fix the bug") is False

    def test_project_tag_scoped_command(self):
        assert is_skill_mission("[project:koan] /core.plan Fix bug") is True

    def test_raw_prefix_ai(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("myproject", "/workspace/myproject")],
        )
        assert is_skill_mission("myproject /ai") is True

    def test_raw_word_not_project_rejected(self):
        """Common English word before /command should NOT match as project."""
        assert is_skill_mission("the /keyword at the beginning") is False

    def test_raw_word_unknown_project_rejected(self, monkeypatch):
        """Unknown project name before /command should not match."""
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        assert is_skill_mission("unknown /plan test") is False


# ---------------------------------------------------------------------------
# parse_skill_mission — project-id prefix handling
# ---------------------------------------------------------------------------

class TestParseSkillMissionWithPrefix:
    def test_project_tag_prefix(self):
        pid, cmd, args = parse_skill_mission("[project:koan] /plan Add dark mode")
        assert pid == "koan"
        assert cmd == "plan"
        assert args == "Add dark mode"

    def test_projet_tag_prefix(self):
        pid, cmd, args = parse_skill_mission("[projet:myapp] /rebase https://github.com/x/y/pull/1")
        assert pid == "myapp"
        assert cmd == "rebase"
        assert args == "https://github.com/x/y/pull/1"

    def test_raw_project_word_prefix(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        pid, cmd, args = parse_skill_mission("koan /plan Add dark mode")
        assert pid == "koan"
        assert cmd == "plan"
        assert args == "Add dark mode"

    def test_raw_word_prefix_no_args(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        pid, cmd, args = parse_skill_mission("koan /ai")
        assert pid == "koan"
        assert cmd == "ai"
        assert args == ""

    def test_project_tag_scoped_command(self):
        pid, cmd, args = parse_skill_mission("[project:koan] /core.plan Fix bug")
        assert pid == "koan"
        assert cmd == "plan"
        assert args == "Fix bug"

    def test_project_tag_no_command(self):
        pid, cmd, args = parse_skill_mission("[project:koan] Fix the login bug")
        assert pid == "koan"
        assert cmd == ""
        assert args == "Fix the login bug"

    def test_raw_word_no_command(self):
        """Two regular words — first word is not a project prefix."""
        pid, cmd, args = parse_skill_mission("Fix the bug")
        assert pid == ""
        assert cmd == ""
        assert args == "Fix the bug"

    def test_raw_word_not_project_no_prefix(self):
        """Non-project word before /command — whole text returned as-is."""
        pid, cmd, args = parse_skill_mission("the /keyword at the beginning")
        assert pid == ""
        assert cmd == ""
        assert args == "the /keyword at the beginning"

    def test_raw_word_unknown_project_no_prefix(self, monkeypatch):
        """Unknown project name — whole text returned as-is."""
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        pid, cmd, args = parse_skill_mission("unknown /plan test")
        assert pid == ""
        assert cmd == ""
        assert args == "unknown /plan test"


# ---------------------------------------------------------------------------
# dispatch_skill_mission — project-id prefix handling
# ---------------------------------------------------------------------------

class TestDispatchSkillMissionWithPrefix:
    KOAN_ROOT = "/home/user/koan"
    INSTANCE = "/home/user/koan/instance"

    def _dispatch(self, mission_text, project_name="koan", project_path="/home/user/workspace/koan"):
        return dispatch_skill_mission(
            mission_text=mission_text,
            project_name=project_name,
            project_path=project_path,
            koan_root=self.KOAN_ROOT,
            instance_dir=self.INSTANCE,
        )

    def test_project_tag_prefix_dispatches(self):
        cmd = self._dispatch("[project:koan] /plan Add dark mode")
        assert cmd is not None
        assert "app.plan_runner" in cmd

    def test_raw_word_prefix_dispatches(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        cmd = self._dispatch("koan /plan Add dark mode")
        assert cmd is not None
        assert "app.plan_runner" in cmd

    def test_project_tag_prefix_returns_none_for_regular_mission(self):
        cmd = self._dispatch("[project:koan] Fix the login bug")
        assert cmd is None

    def test_project_id_used_as_fallback(self, monkeypatch):
        """When project_name is empty, parsed project_id is used."""
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        cmd = self._dispatch("koan /ai", project_name="", project_path="/workspace/koan")
        assert cmd is not None
        assert "app.ai_runner" in cmd
        # The project name in the command should come from the parsed prefix
        assert "koan" in cmd

    def test_explicit_project_name_takes_priority(self):
        """Caller's project_name is used even if prefix has a different project."""
        cmd = self._dispatch("[project:other] /ai", project_name="koan", project_path="/workspace/koan")
        assert cmd is not None
        assert "koan" in cmd


# ---------------------------------------------------------------------------
# is_known_project (now in utils.py)
# ---------------------------------------------------------------------------

class TestIsKnownProject:
    def test_known_project(self, monkeypatch):
        from app.utils import is_known_project
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan"), ("Clone", "/workspace/Clone")],
        )
        assert is_known_project("koan") is True
        assert is_known_project("Clone") is True

    def test_case_insensitive(self, monkeypatch):
        from app.utils import is_known_project
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("Clone", "/workspace/Clone")],
        )
        assert is_known_project("clone") is True
        assert is_known_project("CLONE") is True

    def test_unknown_project(self, monkeypatch):
        from app.utils import is_known_project
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        assert is_known_project("unknown") is False

    def test_common_english_words_rejected(self, monkeypatch):
        from app.utils import is_known_project
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: [("koan", "/workspace/koan")],
        )
        for word in ("the", "fix", "add", "when", "let", "we"):
            assert is_known_project(word) is False

    def test_error_returns_false(self, monkeypatch):
        from app.utils import is_known_project
        monkeypatch.setattr(
            "app.utils.get_known_projects",
            lambda: (_ for _ in ()).throw(RuntimeError("config error")),
        )
        assert is_known_project("koan") is False


# ---------------------------------------------------------------------------
# validate_skill_args
# ---------------------------------------------------------------------------

class TestValidateSkillArgs:
    """Tests for validate_skill_args() — argument validation with user-facing messages."""

    def test_rebase_valid_url(self):
        assert validate_skill_args("rebase", "https://github.com/sukria/koan/pull/42") is None

    def test_rebase_no_url(self):
        err = validate_skill_args("rebase", "just some text")
        assert err is not None
        assert "/rebase requires a PR URL" in err

    def test_rebase_empty_args(self):
        err = validate_skill_args("rebase", "")
        assert err is not None
        assert "/rebase requires a PR URL" in err

    def test_recreate_valid_url(self):
        assert validate_skill_args("recreate", "https://github.com/sukria/koan/pull/100") is None

    def test_recreate_no_url(self):
        err = validate_skill_args("recreate", "no url here")
        assert err is not None
        assert "/recreate requires a PR URL" in err

    def test_implement_valid_issue_url(self):
        assert validate_skill_args("implement", "https://github.com/sukria/koan/issues/42") is None

    def test_implement_no_url(self):
        err = validate_skill_args("implement", "fix the login bug")
        assert err is not None
        assert "/implement requires an issue URL" in err

    def test_implement_pr_url_not_issue(self):
        err = validate_skill_args("implement", "https://github.com/sukria/koan/pull/42")
        assert err is not None
        assert "/implement requires an issue URL" in err

    def test_check_valid_pr_url(self):
        assert validate_skill_args("check", "https://github.com/sukria/koan/pull/42") is None

    def test_check_valid_issue_url(self):
        assert validate_skill_args("check", "https://github.com/sukria/koan/issues/42") is None

    def test_check_no_url(self):
        err = validate_skill_args("check", "no url here")
        assert err is not None
        assert "/check requires a GitHub URL" in err

    def test_plan_always_valid(self):
        """Plan accepts free text — no arg validation error."""
        assert validate_skill_args("plan", "Add dark mode") is None
        assert validate_skill_args("plan", "") is None

    def test_ai_always_valid(self):
        """AI has no arg requirements."""
        assert validate_skill_args("ai", "") is None
        assert validate_skill_args("ai", "koan") is None

    def test_claudemd_always_valid(self):
        assert validate_skill_args("claudemd", "koan") is None

    def test_unknown_command_returns_none(self):
        """Unknown commands return None — caller handles that case."""
        assert validate_skill_args("nonexistent", "whatever") is None

    def test_rebase_url_with_surrounding_text(self):
        """URL embedded in text should still validate."""
        assert validate_skill_args(
            "rebase",
            "please rebase https://github.com/sukria/koan/pull/42 thanks",
        ) is None

    def test_implement_url_with_context(self):
        """Issue URL with trailing context should validate."""
        assert validate_skill_args(
            "implement",
            "https://github.com/sukria/koan/issues/42 Phase 1 to 3",
        ) is None

    def test_fix_valid_issue_url(self):
        assert validate_skill_args("fix", "https://github.com/Anantys/investmindr/issues/42") is None

    def test_fix_no_url(self):
        err = validate_skill_args("fix", "fix the login bug")
        assert err is not None
        assert "/fix requires an issue URL" in err

    def test_fix_pr_url_not_issue(self):
        err = validate_skill_args("fix", "https://github.com/sukria/koan/pull/42")
        assert err is not None
        assert "/fix requires an issue URL" in err

    def test_fix_url_with_context(self):
        assert validate_skill_args(
            "fix",
            "https://github.com/Anantys/investmindr/issues/42 backend only",
        ) is None


# ---------------------------------------------------------------------------
# Fallthrough guard: skill missions that fail dispatch should not go to Claude
# ---------------------------------------------------------------------------

class TestSkillMissionFallthroughGuard:
    """Verify that is_skill_mission correctly identifies missions that
    should NOT fall through to the Claude agent when dispatch returns None."""

    def test_unrecognized_slash_command_is_skill(self):
        """A /nonexistent command should still be detected as a skill mission."""
        assert is_skill_mission("/nonexistent do things") is True

    def test_regular_mission_not_skill(self):
        assert is_skill_mission("Fix the login bug") is False

    def test_mission_with_slash_in_middle_not_skill(self):
        """Slash in the middle of text is not a skill command."""
        assert is_skill_mission("Fix the /plan bug") is False

    def test_dispatch_returns_none_for_unknown_runner(self):
        """dispatch_skill_mission returns None for unrecognized commands."""
        cmd = dispatch_skill_mission(
            mission_text="/nonexistent do things",
            project_name="koan",
            project_path="/workspace/koan",
            koan_root="/koan",
            instance_dir="/koan/instance",
        )
        assert cmd is None

    def test_is_skill_mission_plus_dispatch_none_is_fallthrough_case(self):
        """This is the combination that triggers the fallthrough guard in run.py."""
        mission = "/nonexistent do things"
        assert is_skill_mission(mission) is True
        cmd = dispatch_skill_mission(
            mission_text=mission,
            project_name="koan",
            project_path="/workspace/koan",
            koan_root="/koan",
            instance_dir="/koan/instance",
        )
        assert cmd is None
        # In run.py, this case now fails the mission instead of
        # falling through to Claude agent

    def test_known_command_bad_args_gives_specific_error(self):
        """A known command with bad args should give a specific error, not 'unknown'."""
        mission = "/rebase just some text without a URL"
        assert is_skill_mission(mission) is True
        cmd = dispatch_skill_mission(
            mission_text=mission,
            project_name="koan",
            project_path="/workspace/koan",
            koan_root="/koan",
            instance_dir="/koan/instance",
        )
        assert cmd is None
        # validate_skill_args distinguishes this from unknown commands
        _, cmd_name, cmd_args = parse_skill_mission(mission)
        assert cmd_name == "rebase"
        err = validate_skill_args(cmd_name, cmd_args)
        assert err is not None
        assert "PR URL" in err

    def test_unknown_command_no_specific_error(self):
        """A truly unknown command has no arg-level error."""
        mission = "/nonexistent do things"
        _, cmd_name, cmd_args = parse_skill_mission(mission)
        assert cmd_name == "nonexistent"
        err = validate_skill_args(cmd_name, cmd_args)
        assert err is None  # No specific error — it's just unknown


# ---------------------------------------------------------------------------
# Thread-safety of _cached_registry / _cached_extra_dirs
# ---------------------------------------------------------------------------

class TestRegistryCacheThreadSafety:
    """Verify that concurrent calls to translate_cli_skill_mission don't race."""

    def test_concurrent_translate_calls(self, tmp_path, monkeypatch):
        """Multiple threads calling translate_cli_skill_mission should not
        cause double-builds or corrupt the cache."""
        import threading
        from pathlib import Path
        from unittest.mock import MagicMock
        import app.skill_dispatch as sd

        # Reset cache state
        monkeypatch.setattr(sd, "_cached_registry", None)
        monkeypatch.setattr(sd, "_cached_extra_dirs", None)

        build_count = {"n": 0}
        build_lock = threading.Lock()
        fake_registry = MagicMock()
        fake_registry.get.return_value = None  # no skill found

        def slow_build_registry(extra_dirs):
            with build_lock:
                build_count["n"] += 1
            # Simulate work to widen the race window
            import time
            time.sleep(0.01)
            return fake_registry

        monkeypatch.setattr("app.skills.build_registry", slow_build_registry)

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        (instance_dir / "skills").mkdir()

        errors = []

        def worker():
            try:
                sd.translate_cli_skill_mission(
                    "/custom.myskill do stuff",
                    koan_root=tmp_path,
                    instance_dir=instance_dir,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised exceptions: {errors}"
        # With the lock, build_registry should be called exactly once
        assert build_count["n"] == 1, (
            f"build_registry called {build_count['n']} times, expected 1"
        )
