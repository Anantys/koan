"""Tests for the /deepplan core skill — mission-queuing handler and runner."""

import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Load handler module
# ---------------------------------------------------------------------------

HANDLER_PATH = (
    Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "handler.py"
)


def _load_handler():
    spec = importlib.util.spec_from_file_location("deepplan_handler", str(HANDLER_PATH))
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
    missions_path = instance_dir / "missions.md"
    missions_path.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="deepplan",
        args="",
        send_message=MagicMock(),
    )


# ---------------------------------------------------------------------------
# handle() — usage / routing
# ---------------------------------------------------------------------------

class TestHandlerNoArgs:
    def test_no_args_returns_usage(self, handler, ctx):
        result = handler.handle(ctx)
        assert "Usage:" in result
        assert "/deepplan <idea>" in result

    def test_whitespace_only_returns_usage(self, handler, ctx):
        ctx.args = "   "
        result = handler.handle(ctx)
        assert "Usage:" in result


class TestHandlerQueuesMission:
    def test_queues_mission_in_missions_md(self, handler, ctx):
        ctx.args = "Refactor the auth middleware"
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path/koan")]):
            result = handler.handle(ctx)
        assert "queued" in result.lower()
        missions = (ctx.instance_dir / "missions.md").read_text()
        assert "/deepplan Refactor the auth middleware" in missions

    def test_response_includes_idea_preview(self, handler, ctx):
        ctx.args = "Improve caching strategy"
        with patch("app.utils.get_known_projects", return_value=[("koan", "/p")]):
            result = handler.handle(ctx)
        assert "Improve caching strategy" in result

    def test_mission_uses_clean_format(self, handler, ctx):
        ctx.args = "Add rate limiting"
        with patch("app.utils.get_known_projects", return_value=[("koan", "/p")]):
            handler.handle(ctx)
        missions = (ctx.instance_dir / "missions.md").read_text()
        assert "/deepplan Add rate limiting" in missions
        assert "python3 -m" not in missions
        assert "run:" not in missions


class TestHandlerWithProjectPrefix:
    def test_project_name_prefix(self, handler, ctx):
        ctx.args = "koan Refactor auth"
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path/koan")]):
            result = handler.handle(ctx)
        assert "koan" in result
        missions = (ctx.instance_dir / "missions.md").read_text()
        assert "[project:koan]" in missions
        assert "/deepplan Refactor auth" in missions

    def test_project_tag_format(self, handler, ctx):
        ctx.args = "[project:koan] Add dark mode"
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path/koan")]):
            result = handler.handle(ctx)
        missions = (ctx.instance_dir / "missions.md").read_text()
        assert "[project:koan]" in missions
        assert "/deepplan Add dark mode" in missions

    def test_unknown_project_returns_error(self, handler, ctx):
        ctx.args = "unknown_proj Refactor auth"
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            # "unknown_proj" is not in known projects, treated as part of idea
            result = handler.handle(ctx)
        # Should still queue (unknown prefix treated as idea text)
        missions = (ctx.instance_dir / "missions.md").read_text()
        assert "/deepplan" in missions


# ---------------------------------------------------------------------------
# _parse_project_arg
# ---------------------------------------------------------------------------

class TestParseProjectArg:
    def test_no_project_prefix(self, handler):
        with patch("app.utils.get_known_projects", return_value=[]):
            project, idea = handler._parse_project_arg("Refactor auth")
        assert project is None
        assert idea == "Refactor auth"

    def test_project_tag_format(self, handler):
        project, idea = handler._parse_project_arg("[project:koan] Fix the bug")
        assert project == "koan"
        assert idea == "Fix the bug"

    def test_project_name_prefix(self, handler):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            project, idea = handler._parse_project_arg("koan Fix the login")
        assert project == "koan"
        assert idea == "Fix the login"

    def test_unknown_word_not_treated_as_project(self, handler):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            project, idea = handler._parse_project_arg("webapp Fix the login")
        assert project is None
        assert idea == "webapp Fix the login"

    def test_single_word_no_project(self, handler):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            project, idea = handler._parse_project_arg("refactor")
        assert project is None
        assert idea == "refactor"


# ---------------------------------------------------------------------------
# SKILL.md — structure validation
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "SKILL.md"
        )
        assert skill is not None
        assert skill.name == "deepplan"
        assert skill.scope == "core"
        assert len(skill.commands) >= 1
        assert skill.commands[0].name == "deepplan"

    def test_skill_has_group(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "SKILL.md"
        )
        assert skill.group in ("missions", "code", "pr", "status", "config", "ideas", "system")

    def test_skill_not_worker(self):
        """Handler queues missions — should not be a worker."""
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "SKILL.md"
        )
        assert skill.worker is False

    def test_skill_registered_in_registry(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("deepplan")
        assert skill is not None
        assert skill.name == "deepplan"

    def test_alias_registered(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("deeplan")
        assert skill is not None
        assert skill.name == "deepplan"

    def test_handler_file_exists(self):
        handler_path = (
            Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "handler.py"
        )
        assert handler_path.exists()


# ---------------------------------------------------------------------------
# Prompts — structure validation
# ---------------------------------------------------------------------------

PROMPTS_DIR = (
    Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "prompts"
)


class TestDeepplanPrompts:
    def test_explore_prompt_exists(self):
        assert (PROMPTS_DIR / "deepplan-explore.md").exists()

    def test_review_prompt_exists(self):
        assert (PROMPTS_DIR / "deepplan-review.md").exists()

    def test_explore_prompt_has_idea_placeholder(self):
        content = (PROMPTS_DIR / "deepplan-explore.md").read_text()
        assert "{IDEA}" in content

    def test_explore_prompt_has_required_sections(self):
        content = (PROMPTS_DIR / "deepplan-explore.md").read_text()
        assert "Alternatives Considered" in content
        assert "Open Questions" in content
        assert "Recommended Approach" in content

    def test_review_prompt_has_spec_placeholder(self):
        content = (PROMPTS_DIR / "deepplan-review.md").read_text()
        assert "{SPEC}" in content

    def test_review_prompt_output_format(self):
        content = (PROMPTS_DIR / "deepplan-review.md").read_text()
        assert "APPROVED" in content
        assert "ISSUES_FOUND" in content


# ---------------------------------------------------------------------------
# Runner — unit tests (no real Claude calls)
# ---------------------------------------------------------------------------

RUNNER_PATH = (
    Path(__file__).parent.parent / "skills" / "core" / "deepplan" / "deepplan_runner.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("deepplan_runner", str(RUNNER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def runner():
    return _load_runner()


class TestRunnerApprovedFirstTry:
    def test_approved_first_try(self, runner, tmp_path):
        """Runner posts issue when review approves on first try."""
        valid_spec = (
            "Design spec: improve caching strategy\n\n"
            "### Summary\n\nThis spec covers caching improvements.\n\n"
            "### Alternatives Considered\n\n- **Approach A (recommended)**: Redis. *Trade-off: ops overhead.*\n"
            "- **Approach B**: In-memory. *Trade-off: no persistence.*\n\n"
            "### Recommended Approach\n\nUse Redis with `koan/app/cache.py`.\n\n"
            "### Scope\n\nCaching layer only.\n\n"
            "### Out of Scope\n\nAuth changes.\n\n"
            "### Open Questions\n\nNone — ready for /plan."
        )

        with patch.object(runner, "_get_repo_info", return_value=("owner", "repo")), \
             patch.object(runner, "_explore_design", return_value=valid_spec), \
             patch.object(runner, "_review_spec", return_value=(True, "")), \
             patch.object(runner, "issue_create", return_value="https://github.com/o/r/issues/1"), \
             patch.object(runner, "_queue_plan_mission") as mock_queue, \
             patch("app.notify.send_telegram"):

            success, summary = runner.run_deepplan(
                project_path=str(tmp_path),
                idea="Improve caching strategy",
                skill_dir=RUNNER_PATH.parent,
            )

        assert success is True
        assert "issues/1" in summary
        mock_queue.assert_called_once_with(str(tmp_path), "https://github.com/o/r/issues/1")


class TestRunnerRetryOnIssuesFound:
    def test_retry_on_issues_found(self, runner, tmp_path):
        """Runner retries exploration when review finds issues."""
        spec_v1 = "Vague spec title\n\n### Summary\nVague."
        spec_v2 = "Better spec title\n\n### Summary\nBetter."
        spec_v3 = "Final spec title\n\n### Summary\nFinal."

        explore_results = [spec_v1, spec_v2, spec_v3]
        review_results = [(False, "Missing file paths"), (False, "Still vague"), (True, "")]

        with patch.object(runner, "_get_repo_info", return_value=("o", "r")), \
             patch.object(runner, "_explore_design", side_effect=explore_results) as mock_explore, \
             patch.object(runner, "_review_spec", side_effect=review_results), \
             patch.object(runner, "issue_create", return_value="https://github.com/o/r/issues/2"), \
             patch.object(runner, "_queue_plan_mission"), \
             patch("app.notify.send_telegram"):

            success, summary = runner.run_deepplan(
                project_path=str(tmp_path),
                idea="Improve caching",
                skill_dir=RUNNER_PATH.parent,
            )

        assert success is True
        # explore_design called: 1 initial + 2 retries = 3
        assert mock_explore.call_count == 3


class TestRunnerMaxIterations:
    def test_max_iterations_posts_best_effort(self, runner, tmp_path):
        """Runner posts best-effort spec when max review rounds exceeded."""
        spec = "Spec title\n\n### Summary\nSpec body."
        always_issues = (False, "Always failing")

        with patch.object(runner, "_get_repo_info", return_value=("o", "r")), \
             patch.object(runner, "_explore_design", return_value=spec) as mock_explore, \
             patch.object(runner, "_review_spec", return_value=always_issues), \
             patch.object(runner, "issue_create", return_value="https://github.com/o/r/issues/3") as mock_create, \
             patch.object(runner, "_queue_plan_mission"), \
             patch("app.notify.send_telegram"):

            success, summary = runner.run_deepplan(
                project_path=str(tmp_path),
                idea="Something vague",
                skill_dir=RUNNER_PATH.parent,
            )

        # Should still post issue (best-effort)
        assert success is True
        mock_create.assert_called_once()
        # explore_design: 1 initial + (_MAX_REVIEW_ROUNDS - 1) retries
        assert mock_explore.call_count == runner._MAX_REVIEW_ROUNDS


class TestRunnerNoGithubRepo:
    def test_no_github_repo_returns_failure(self, runner, tmp_path):
        """Runner returns failure when no GitHub repository found."""
        with patch.object(runner, "_get_repo_info", return_value=(None, None)), \
             patch("app.notify.send_telegram"):

            success, summary = runner.run_deepplan(
                project_path=str(tmp_path),
                idea="Improve caching",
                skill_dir=RUNNER_PATH.parent,
            )

        assert success is False
        assert "No GitHub repository" in summary


# ---------------------------------------------------------------------------
# skill_dispatch — deepplan registered
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_deepplan_in_skill_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "deepplan" in _SKILL_RUNNERS
        assert "skills.core.deepplan.deepplan_runner" in _SKILL_RUNNERS["deepplan"]

    def test_deeplan_alias_in_skill_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "deeplan" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["deeplan"] == _SKILL_RUNNERS["deepplan"]

    def test_build_deepplan_cmd(self, tmp_path):
        from app.skill_dispatch import build_skill_command
        cmd = build_skill_command(
            command="deepplan",
            args="Improve caching strategy",
            project_name="koan",
            project_path=str(tmp_path),
            koan_root=str(tmp_path),
            instance_dir=str(tmp_path),
        )
        assert cmd is not None
        assert "--project-path" in cmd
        assert "--idea" in cmd
        assert "Improve caching strategy" in cmd

    def test_build_deeplan_cmd(self, tmp_path):
        from app.skill_dispatch import build_skill_command
        cmd = build_skill_command(
            command="deeplan",
            args="Refactor auth",
            project_name="koan",
            project_path=str(tmp_path),
            koan_root=str(tmp_path),
            instance_dir=str(tmp_path),
        )
        assert cmd is not None
        assert "--idea" in cmd
