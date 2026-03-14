"""Tests for the /tech_debt skill — handler and runner."""

import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "tech_debt" / "handler.py"


def _load_handler():
    """Load the tech_debt handler module dynamically."""
    spec = importlib.util.spec_from_file_location("tech_debt_handler", str(HANDLER_PATH))
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
        command_name="tech_debt",
        args="",
        send_message=MagicMock(),
    )


class TestHandleRouting:
    def test_help_flag_returns_usage(self, handler, ctx):
        ctx.args = "--help"
        result = handler.handle(ctx)
        assert "Usage:" in result

    def test_help_short_flag_returns_usage(self, handler, ctx):
        ctx.args = "-h"
        result = handler.handle(ctx)
        assert "Usage:" in result


class TestHandleQueueMission:
    @patch("app.utils.get_known_projects", return_value=[("myproject", "/path/myproject")])
    @patch("app.utils.insert_pending_mission")
    def test_no_args_uses_first_project(self, mock_insert, mock_projects, handler, ctx):
        ctx.args = ""
        result = handler.handle(ctx)

        assert "Tech debt scan queued" in result
        assert "myproject" in result
        mock_insert.assert_called_once()
        mission_entry = mock_insert.call_args[0][1]
        assert "/tech_debt" in mission_entry
        assert "[project:myproject]" in mission_entry

    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_named_project(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan"
        result = handler.handle(ctx)

        assert "Tech debt scan queued" in result
        assert "koan" in result
        mock_insert.assert_called_once()
        mission_entry = mock_insert.call_args[0][1]
        assert "[project:koan]" in mission_entry

    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_no_queue_flag(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan --no-queue"
        result = handler.handle(ctx)

        assert "Tech debt scan queued" in result
        mission_entry = mock_insert.call_args[0][1]
        assert "--no-queue" in mission_entry

    @patch("app.utils.resolve_project_path", return_value=None)
    @patch("app.utils.get_known_projects", return_value=[("web", "/path/web")])
    def test_unknown_project(self, mock_projects, mock_resolve, handler, ctx):
        ctx.args = "nonexistent"
        result = handler.handle(ctx)

        assert "\u274c" in result
        assert "nonexistent" in result
        assert "web" in result

    @patch("app.utils.get_known_projects", return_value=[])
    def test_no_projects_configured(self, mock_projects, handler, ctx):
        ctx.args = ""
        result = handler.handle(ctx)

        assert "\u274c" in result
        assert "No projects" in result


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------

from skills.core.tech_debt.tech_debt_runner import (
    build_tech_debt_prompt,
    _extract_report_body,
    _extract_debt_score,
    _extract_missions,
    _save_report,
    _queue_missions,
    run_tech_debt,
    main,
)


class TestBuildPrompt:
    def test_prompt_contains_project_name(self):
        prompt = build_tech_debt_prompt(
            "myproject",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "tech_debt",
        )
        assert "myproject" in prompt

    def test_prompt_contains_instructions(self):
        prompt = build_tech_debt_prompt(
            "test",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "tech_debt",
        )
        assert "tech debt" in prompt.lower()
        assert "Debt Score" in prompt


class TestExtractReportBody:
    def test_extracts_from_tech_debt_header(self):
        raw = "Some preamble\n\nTech Debt Report — myproject\n\n## Summary\nGood project."
        result = _extract_report_body(raw)
        assert result.startswith("Tech Debt Report")
        assert "## Summary" in result

    def test_extracts_from_summary_header(self):
        raw = "Preamble text\n\n## Summary\nThe project has moderate debt."
        result = _extract_report_body(raw)
        assert result.startswith("## Summary")

    def test_fallback_to_full_output(self):
        raw = "Just some analysis text with no headers."
        result = _extract_report_body(raw)
        assert result == raw.strip()

    def test_empty_output(self):
        assert _extract_report_body("") == ""

    def test_whitespace_only(self):
        assert _extract_report_body("   \n  ") == ""


class TestExtractDebtScore:
    def test_valid_score(self):
        report = "**Debt Score**: 7/10"
        assert _extract_debt_score(report) == 7

    def test_score_1(self):
        assert _extract_debt_score("**Debt Score**: 1/10") == 1

    def test_score_10(self):
        assert _extract_debt_score("**Debt Score**: 10/10") == 10

    def test_score_zero_invalid(self):
        assert _extract_debt_score("**Debt Score**: 0/10") is None

    def test_score_11_invalid(self):
        assert _extract_debt_score("**Debt Score**: 11/10") is None

    def test_no_score(self):
        assert _extract_debt_score("No score here") is None

    def test_score_in_larger_report(self):
        report = (
            "## Summary\n\nSome text.\n\n"
            "**Debt Score**: 4/10\n\n"
            "## Findings\n..."
        )
        assert _extract_debt_score(report) == 4


class TestExtractMissions:
    def test_extracts_numbered_missions(self):
        report = (
            "## Findings\n\nSome stuff.\n\n"
            "## Suggested Missions\n\n"
            "1. Refactor the auth module\n"
            "2. Add tests for the parser\n"
            "3. Remove deprecated API calls\n"
        )
        missions = _extract_missions(report)
        assert len(missions) == 3
        assert "Refactor the auth module" in missions[0]
        assert "Add tests for the parser" in missions[1]

    def test_extracts_with_dash_suffix(self):
        report = (
            "## Suggested Missions\n\n"
            "1. Fix duplication — addresses finding #1\n"
            "2. Add types — addresses finding #3\n"
        )
        missions = _extract_missions(report)
        assert len(missions) == 2
        assert missions[0] == "Fix duplication"

    def test_max_five_missions(self):
        lines = "\n".join(f"{i}. Mission {i}" for i in range(1, 8))
        report = f"## Suggested Missions\n\n{lines}\n"
        missions = _extract_missions(report)
        assert len(missions) == 5

    def test_no_section_returns_empty(self):
        report = "## Summary\n\nJust a summary."
        assert _extract_missions(report) == []

    def test_empty_section(self):
        report = "## Suggested Missions\n\n## Next Section\n"
        assert _extract_missions(report) == []


class TestSaveReport:
    def test_creates_file_with_header(self, tmp_path):
        report_path = _save_report(tmp_path, "myproject", "## Summary\nGood.", 5)

        assert report_path.exists()
        content = report_path.read_text()
        assert "Last scan:" in content
        assert "Debt score: 5/10" in content
        assert "## Summary" in content

    def test_creates_directory_structure(self, tmp_path):
        _save_report(tmp_path, "newproject", "Report", None)

        learnings_dir = tmp_path / "memory" / "projects" / "newproject"
        assert learnings_dir.exists()

    def test_no_score_header_when_none(self, tmp_path):
        _save_report(tmp_path, "proj", "Report", None)

        content = (tmp_path / "memory" / "projects" / "proj" / "tech_debt.md").read_text()
        assert "Last scan:" in content
        assert "Debt score:" not in content

    def test_overwrites_existing_report(self, tmp_path):
        _save_report(tmp_path, "proj", "Old report", 3)
        _save_report(tmp_path, "proj", "New report", 7)

        content = (tmp_path / "memory" / "projects" / "proj" / "tech_debt.md").read_text()
        assert "New report" in content
        assert "Old report" not in content


class TestQueueMissions:
    @patch("app.utils.insert_pending_mission")
    def test_queues_up_to_max(self, mock_insert, tmp_path):
        missions = ["Fix A", "Fix B", "Fix C", "Fix D"]
        queued = _queue_missions(tmp_path, "proj", missions, max_missions=3)

        assert queued == 3
        assert mock_insert.call_count == 3

    @patch("app.utils.insert_pending_mission")
    def test_queue_entry_format(self, mock_insert, tmp_path):
        _queue_missions(tmp_path, "myproj", ["Refactor auth"])

        entry = mock_insert.call_args[0][1]
        assert entry == "- [project:myproj] Refactor auth"

    @patch("app.utils.insert_pending_mission")
    def test_empty_missions(self, mock_insert, tmp_path):
        queued = _queue_missions(tmp_path, "proj", [])
        assert queued == 0
        mock_insert.assert_not_called()


SAMPLE_REPORT = """\
Tech Debt Report — testproj

## Summary

The project has moderate tech debt.

**Debt Score**: 5/10

## Findings

### High Priority

1. Duplicated validation logic in auth.py and api.py

### Medium Priority

1. Complex function parse_input (120 lines)

### Low Priority

1. Missing type annotations in utils.py

## Suggested Missions

1. Extract shared validation into a common module
2. Split parse_input into smaller functions
3. Add type annotations to public interfaces
"""


class TestRunTechDebt:
    @patch("skills.core.tech_debt.tech_debt_runner.build_tech_debt_prompt", return_value="scan prompt")
    @patch("skills.core.tech_debt.tech_debt_runner._run_claude_scan", return_value=SAMPLE_REPORT)
    def test_full_pipeline_success(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        missions_path = instance_dir / "missions.md"
        missions_path.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        notify = MagicMock()

        with patch("skills.core.tech_debt.tech_debt_runner._queue_missions", return_value=3) as mock_queue:
            success, summary = run_tech_debt(
                project_path="/path/testproj",
                project_name="testproj",
                instance_dir=str(instance_dir),
                notify_fn=notify,
            )

        assert success
        assert "tech_debt.md" in summary
        assert "5/10" in summary
        assert "3 missions queued" in summary
        # Notification calls: scan start + success
        assert notify.call_count == 2

    @patch("skills.core.tech_debt.tech_debt_runner.build_tech_debt_prompt", return_value="scan prompt")
    @patch("skills.core.tech_debt.tech_debt_runner._run_claude_scan", return_value=SAMPLE_REPORT)
    def test_no_queue_flag(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        with patch("skills.core.tech_debt.tech_debt_runner._queue_missions") as mock_queue:
            success, summary = run_tech_debt(
                project_path="/path/testproj",
                project_name="testproj",
                instance_dir=str(instance_dir),
                notify_fn=notify,
                queue_missions=False,
            )

        assert success
        mock_queue.assert_not_called()

    @patch("skills.core.tech_debt.tech_debt_runner.build_tech_debt_prompt", return_value="scan prompt")
    @patch("skills.core.tech_debt.tech_debt_runner._run_claude_scan", side_effect=RuntimeError("quota"))
    def test_scan_failure(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        success, summary = run_tech_debt(
            project_path="/path/testproj",
            project_name="testproj",
            instance_dir=str(instance_dir),
            notify_fn=notify,
        )

        assert not success
        assert "failed" in summary.lower()

    @patch("skills.core.tech_debt.tech_debt_runner.build_tech_debt_prompt", return_value="scan prompt")
    @patch("skills.core.tech_debt.tech_debt_runner._run_claude_scan", return_value="")
    def test_empty_output(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        success, summary = run_tech_debt(
            project_path="/path/testproj",
            project_name="testproj",
            instance_dir=str(instance_dir),
            notify_fn=notify,
        )

        assert not success
        assert "no output" in summary.lower()

    @patch("skills.core.tech_debt.tech_debt_runner.build_tech_debt_prompt", return_value="scan prompt")
    @patch("skills.core.tech_debt.tech_debt_runner._run_claude_scan", return_value="Just some analysis with no score.")
    def test_report_without_score(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        with patch("skills.core.tech_debt.tech_debt_runner._queue_missions", return_value=0):
            success, summary = run_tech_debt(
                project_path="/path/testproj",
                project_name="testproj",
                instance_dir=str(instance_dir),
                notify_fn=notify,
            )

        assert success
        assert "score" not in summary

    @patch("skills.core.tech_debt.tech_debt_runner.build_tech_debt_prompt", return_value="scan prompt")
    @patch("skills.core.tech_debt.tech_debt_runner._run_claude_scan", return_value=SAMPLE_REPORT)
    def test_saves_report_to_learnings(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        with patch("skills.core.tech_debt.tech_debt_runner._queue_missions", return_value=0):
            run_tech_debt(
                project_path="/path/testproj",
                project_name="testproj",
                instance_dir=str(instance_dir),
                notify_fn=notify,
            )

        report_path = instance_dir / "memory" / "projects" / "testproj" / "tech_debt.md"
        assert report_path.exists()
        content = report_path.read_text()
        assert "Tech Debt Report" in content


class TestCLI:
    @patch("skills.core.tech_debt.tech_debt_runner.run_tech_debt", return_value=(True, "Done"))
    def test_main_success(self, mock_run, tmp_path):
        exit_code = main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        assert exit_code == 0
        mock_run.assert_called_once()

    @patch("skills.core.tech_debt.tech_debt_runner.run_tech_debt", return_value=(False, "Failed"))
    def test_main_failure(self, mock_run, tmp_path):
        exit_code = main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        assert exit_code == 1

    @patch("skills.core.tech_debt.tech_debt_runner.run_tech_debt", return_value=(True, "Done"))
    def test_main_no_queue_flag(self, mock_run, tmp_path):
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
            "--no-queue",
        ])
        _, kwargs = mock_run.call_args
        assert kwargs.get("queue_missions") is False

    @patch("skills.core.tech_debt.tech_debt_runner.run_tech_debt", return_value=(True, "Done"))
    def test_main_sets_skill_dir(self, mock_run, tmp_path):
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        _, kwargs = mock_run.call_args
        skill_dir = kwargs.get("skill_dir")
        assert skill_dir is not None
        assert skill_dir.name == "tech_debt"


# ---------------------------------------------------------------------------
# skill_dispatch integration tests
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_tech_debt_in_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "tech_debt" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["tech_debt"] == "skills.core.tech_debt.tech_debt_runner"

    def test_build_skill_command(self):
        from app.skill_dispatch import build_skill_command

        cmd = build_skill_command(
            command="tech_debt",
            args="",
            project_name="myproj",
            project_path="/path/myproj",
            koan_root="/koan",
            instance_dir="/koan/instance",
        )

        assert cmd is not None
        assert "--project-path" in cmd
        assert "/path/myproj" in cmd
        assert "--project-name" in cmd
        assert "myproj" in cmd
        assert "--instance-dir" in cmd

    def test_parse_skill_mission(self):
        from app.skill_dispatch import parse_skill_mission

        project, command, args = parse_skill_mission("/tech_debt")
        assert command == "tech_debt"
        assert args == ""

    def test_parse_with_project_tag(self):
        from app.skill_dispatch import parse_skill_mission

        project, command, args = parse_skill_mission("[project:koan] /tech_debt --no-queue")
        assert project == "koan"
        assert command == "tech_debt"
        assert args == "--no-queue"
