"""Tests for the /squash core skill -- handler, SKILL.md, runner, and registry."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Import handler
# ---------------------------------------------------------------------------

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "squash" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("squash_handler", str(HANDLER_PATH))
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
        command_name="squash",
        args="",
        send_message=MagicMock(),
    )


# ---------------------------------------------------------------------------
# handle() -- usage / routing
# ---------------------------------------------------------------------------

class TestHandleRouting:
    def test_no_args_returns_usage(self, handler, ctx):
        result = handler.handle(ctx)
        assert "Usage:" in result
        assert "/squash" in result

    def test_invalid_url_returns_error(self, handler, ctx):
        ctx.args = "not-a-url"
        result = handler.handle(ctx)
        assert "\u274c" in result
        assert "No valid" in result

    def test_non_pr_url_returns_error(self, handler, ctx):
        ctx.args = "https://github.com/sukria/koan/issues/42"
        result = handler.handle(ctx)
        assert "\u274c" in result

    def test_unknown_repo_returns_error(self, handler, ctx):
        ctx.args = "https://github.com/unknown/repo/pull/1"
        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            result = handler.handle(ctx)
            assert "\u274c" in result
            assert "repo" in result.lower()


# ---------------------------------------------------------------------------
# handle() -- mission queuing
# ---------------------------------------------------------------------------

class TestMissionQueuing:
    def test_valid_url_queues_mission(self, handler, ctx):
        ctx.args = "https://github.com/sukria/koan/pull/42"
        with patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission") as mock_insert:
            result = handler.handle(ctx)
            assert "queued" in result.lower()
            assert "#42" in result
            mock_insert.assert_called_once()
            mission_entry = mock_insert.call_args[0][1]
            assert "[project:koan]" in mission_entry
            assert "/squash https://github.com/sukria/koan/pull/42" in mission_entry

    def test_returns_ack_message(self, handler, ctx):
        ctx.args = "https://github.com/sukria/koan/pull/42"
        with patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission"):
            result = handler.handle(ctx)
            assert result == "Squash queued for PR #42 (sukria/koan)"

    def test_mission_uses_squash_not_rebase(self, handler, ctx):
        ctx.args = "https://github.com/sukria/koan/pull/42"
        with patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission") as mock_insert:
            handler.handle(ctx)
            entry = mock_insert.call_args[0][1]
            assert "/squash " in entry
            assert "/rebase " not in entry


# ---------------------------------------------------------------------------
# SKILL.md -- structure validation
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "squash" / "SKILL.md"
        )
        assert skill is not None
        assert skill.name == "squash"
        assert skill.scope == "core"
        assert skill.group == "pr"
        assert len(skill.commands) == 1
        assert skill.commands[0].name == "squash"

    def test_skill_has_alias(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "squash" / "SKILL.md"
        )
        assert "sq" in skill.commands[0].aliases

    def test_skill_registered_in_registry(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("squash")
        assert skill is not None
        assert skill.name == "squash"

    def test_alias_registered_in_registry(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("sq")
        assert skill is not None
        assert skill.name == "squash"

    def test_handler_exists(self):
        assert HANDLER_PATH.exists()

    def test_prompt_template_exists(self):
        prompt_path = (
            Path(__file__).parent.parent
            / "skills" / "core" / "squash" / "prompts" / "squash.md"
        )
        assert prompt_path.exists()

    def test_prompt_has_placeholders(self):
        prompt_path = (
            Path(__file__).parent.parent
            / "skills" / "core" / "squash" / "prompts" / "squash.md"
        )
        content = prompt_path.read_text()
        assert "{TITLE}" in content or "{{TITLE}}" in content
        assert "{DIFF}" in content or "{{DIFF}}" in content
        assert "{BASE}" in content or "{{BASE}}" in content


# ---------------------------------------------------------------------------
# skill_dispatch -- registration
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_squash_in_skill_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "squash" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["squash"] == "app.squash_pr"

    def test_squash_validates_pr_url(self):
        from app.skill_dispatch import validate_skill_args
        error = validate_skill_args("squash", "no url here")
        assert error is not None
        assert "PR URL" in error

    def test_squash_accepts_valid_url(self):
        from app.skill_dispatch import validate_skill_args
        error = validate_skill_args(
            "squash", "https://github.com/owner/repo/pull/42"
        )
        assert error is None

    def test_squash_builds_command(self):
        from app.skill_dispatch import build_skill_command
        cmd = build_skill_command(
            command="squash",
            args="https://github.com/owner/repo/pull/42",
            project_name="myproj",
            project_path="/path/to/proj",
            koan_root="/root",
            instance_dir="/instance",
        )
        assert cmd is not None
        assert "app.squash_pr" in " ".join(cmd)
        assert "https://github.com/owner/repo/pull/42" in cmd
        assert "--project-path" in cmd
        assert "/path/to/proj" in cmd


# ---------------------------------------------------------------------------
# squash_pr -- runner unit tests
# ---------------------------------------------------------------------------

class TestSquashRunner:
    def test_extract_between(self):
        from app.squash_pr import _extract_between
        text = "before===START===content here===END===after"
        assert _extract_between(text, "===START===", "===END===") == "content here"

    def test_extract_between_no_end(self):
        from app.squash_pr import _extract_between
        text = "before===START===content here"
        assert _extract_between(text, "===START===", "===END===") == "content here"

    def test_extract_between_no_start(self):
        from app.squash_pr import _extract_between
        text = "no markers here"
        assert _extract_between(text, "===START===", "===END===") == ""

    def test_parse_squash_output(self):
        from app.squash_pr import _parse_squash_output
        output = (
            "===COMMIT_MESSAGE===\n"
            "feat: add new feature\n\n"
            "This adds X and Y.\n"
            "===PR_TITLE===\n"
            "feat: add new feature\n"
            "===PR_DESCRIPTION===\n"
            "## What\nAdded a feature.\n"
            "===END==="
        )
        result = _parse_squash_output(output, {"title": "old"})
        assert "feat: add new feature" in result["commit_message"]
        assert result["pr_title"] == "feat: add new feature"
        assert "Added a feature" in result["pr_description"]

    def test_parse_squash_output_fallback(self):
        from app.squash_pr import _parse_squash_output
        result = _parse_squash_output("garbage output", {"title": "fallback"})
        assert result["commit_message"] == "fallback"
        assert result["pr_title"] == "fallback"

    def test_build_squash_comment(self):
        from app.squash_pr import _build_squash_comment
        comment = _build_squash_comment(
            pr_number="42",
            branch="feature-x",
            base="main",
            commit_count=5,
            actions_log=["Squashed 5 commits into 1", "Force-pushed"],
            squash_text={"commit_message": "feat: add feature x"},
        )
        assert "5 commits" in comment
        assert "feature-x" in comment
        assert "feat: add feature x" in comment
        assert "Koan" in comment

    def test_run_squash_merged_pr_skips(self):
        """Squash should skip if PR is already merged."""
        from app.squash_pr import run_squash

        mock_context = {
            "title": "test",
            "body": "",
            "branch": "feat",
            "base": "main",
            "state": "MERGED",
            "author": "me",
            "head_owner": "me",
            "url": "",
            "diff": "",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
            "has_pending_reviews": False,
        }

        with patch("app.squash_pr.fetch_pr_context", return_value=mock_context):
            ok, summary = run_squash(
                "owner", "repo", "1", "/tmp/proj",
                notify_fn=MagicMock(),
            )
            assert ok is True
            assert "merged" in summary.lower()

    def test_run_squash_single_commit_skips(self):
        """Squash should skip if PR already has 1 commit."""
        from app.squash_pr import run_squash

        mock_context = {
            "title": "test",
            "body": "",
            "branch": "feat",
            "base": "main",
            "state": "OPEN",
            "author": "me",
            "head_owner": "me",
            "url": "",
            "diff": "",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
            "has_pending_reviews": False,
        }

        with patch("app.squash_pr.fetch_pr_context", return_value=mock_context), \
             patch("app.squash_pr._get_current_branch", return_value="main"), \
             patch("app.squash_pr._checkout_pr_branch", return_value="origin"), \
             patch("app.squash_pr._run_git", return_value=""), \
             patch("app.squash_pr._count_commits_since_base", return_value=1), \
             patch("app.squash_pr._safe_checkout"), \
             patch("app.squash_pr._find_remote_for_repo", return_value="origin"):
            ok, summary = run_squash(
                "owner", "repo", "1", "/tmp/proj",
                notify_fn=MagicMock(),
            )
            assert ok is True
            assert "nothing to squash" in summary.lower()

    def test_main_cli_entry(self):
        """CLI entry point should parse URL and invoke run_squash."""
        from app.squash_pr import main

        with patch("app.squash_pr.run_squash", return_value=(True, "done")) as mock_run:
            code = main([
                "https://github.com/owner/repo/pull/42",
                "--project-path", "/tmp/proj",
            ])
            assert code == 0
            mock_run.assert_called_once()
            assert mock_run.call_args[0][0] == "owner"
            assert mock_run.call_args[0][1] == "repo"
            assert mock_run.call_args[0][2] == "42"

    def test_main_cli_invalid_url(self):
        from app.squash_pr import main
        code = main(["not-a-url", "--project-path", "/tmp"])
        assert code == 1


# ---------------------------------------------------------------------------
# GitHub @mention integration
# ---------------------------------------------------------------------------

class TestGitHubMention:
    """Verify squash is discoverable and usable via GitHub @mentions."""

    def test_skill_has_github_enabled(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "squash" / "SKILL.md"
        )
        assert skill.github_enabled is True

    def test_skill_has_github_context_aware(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "squash" / "SKILL.md"
        )
        assert skill.github_context_aware is True

    def test_handler_accepts_url_with_trailing_context(self, handler, ctx):
        """Simulates the GitHub mention path where URL + optional context are injected."""
        ctx.args = "https://github.com/sukria/koan/pull/42 keep the first commit message"
        with patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.insert_pending_mission") as mock_insert:
            result = handler.handle(ctx)
            assert "queued" in result.lower()
            assert "#42" in result
            mock_insert.assert_called_once()

    def test_build_mission_from_command_includes_squash(self):
        """build_mission_from_command produces correct mission for squash."""
        from app.skills import SkillRegistry
        from app.github_command_handler import build_mission_from_command

        registry = SkillRegistry(
            Path(__file__).parent.parent / "skills" / "core"
        )
        skill = registry.find_by_command("squash")
        assert skill is not None

        notif = {"subject": {"url": "https://api.github.com/repos/o/r/pulls/99"}}
        mission = build_mission_from_command(skill, "squash", "", notif, "koan")
        assert "/squash https://github.com/o/r/pull/99" in mission
        assert "[project:koan]" in mission

    def test_build_mission_with_context(self):
        """Extra context from @mention is appended to the mission."""
        from app.skills import SkillRegistry
        from app.github_command_handler import build_mission_from_command

        registry = SkillRegistry(
            Path(__file__).parent.parent / "skills" / "core"
        )
        skill = registry.find_by_command("squash")

        notif = {"subject": {"url": "https://api.github.com/repos/o/r/pulls/99"}}
        mission = build_mission_from_command(
            skill, "squash", "keep the first commit message", notif, "koan"
        )
        assert "keep the first commit message" in mission
        assert "/squash" in mission
