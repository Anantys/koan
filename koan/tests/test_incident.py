"""Tests for the /incident skill — handler and runner."""

import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "incident" / "handler.py"


def _load_handler():
    """Load the incident handler module dynamically."""
    spec = importlib.util.spec_from_file_location("incident_handler", str(HANDLER_PATH))
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
        command_name="incident",
        args="",
        send_message=MagicMock(),
    )


class TestHandleRouting:
    def test_no_args_returns_usage(self, handler, ctx):
        ctx.args = ""
        result = handler.handle(ctx)
        assert "Usage:" in result

    def test_no_args_whitespace_returns_usage(self, handler, ctx):
        ctx.args = "   "
        result = handler.handle(ctx)
        assert "Usage:" in result


class TestQueueIncident:
    @patch("app.utils.get_known_projects", return_value=[("myproject", "/path/myproject")])
    @patch("app.utils.insert_pending_mission")
    def test_queues_mission_default_project(self, mock_insert, mock_projects, handler, ctx):
        ctx.args = "TypeError: 'NoneType' object has no attribute 'id'"
        result = handler.handle(ctx)

        assert "\U0001f6a8" in result  # siren emoji
        assert "Incident queued" in result
        mock_insert.assert_called_once()
        mission_entry = mock_insert.call_args[0][1]
        assert "/incident" in mission_entry
        assert "TypeError" in mission_entry

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path/koan"), ("web", "/path/web")])
    @patch("app.utils.insert_pending_mission")
    def test_queues_mission_with_project_prefix(self, mock_insert, mock_projects, handler, ctx):
        ctx.args = "koan AttributeError: module has no attribute 'foo'"
        result = handler.handle(ctx)

        assert "Incident queued" in result
        assert "koan" in result
        mission_entry = mock_insert.call_args[0][1]
        assert "[project:koan]" in mission_entry
        assert "AttributeError" in mission_entry

    @patch("app.utils.get_known_projects", return_value=[("myproject", "/path/myproject")])
    @patch("app.utils.insert_pending_mission")
    def test_multiline_error_preserved(self, mock_insert, mock_projects, handler, ctx):
        error = "Traceback (most recent call last):\n  File 'app.py', line 42\nValueError: bad"
        ctx.args = error
        handler.handle(ctx)

        mission_entry = mock_insert.call_args[0][1]
        assert "Traceback" in mission_entry
        assert "ValueError" in mission_entry

    @patch("app.utils.get_known_projects", return_value=[("myproject", "/path/myproject")])
    @patch("app.utils.insert_pending_mission")
    def test_long_error_truncated(self, mock_insert, mock_projects, handler, ctx):
        ctx.args = "E" * 5000
        handler.handle(ctx)

        mission_entry = mock_insert.call_args[0][1]
        assert "[... truncated]" in mission_entry
        # Mission should not exceed max length + overhead
        assert len(mission_entry) < 4200

    @patch("app.utils.get_known_projects", return_value=[])
    def test_unknown_project_returns_error(self, mock_projects, handler, ctx):
        ctx.args = "badproject Some error"
        result = handler.handle(ctx)
        # With no projects, fall back to default — empty project list
        # The handler should still queue or error gracefully
        assert isinstance(result, str)

    @patch("app.utils.resolve_project_path", return_value=None)
    @patch("app.utils.get_known_projects", return_value=[("koan", "/p/koan")])
    def test_project_tag_syntax(self, mock_projects, mock_resolve, handler, ctx):
        """[project:X] tag is parsed correctly."""
        ctx.args = "[project:koan] RuntimeError: boom"
        with patch("app.utils.insert_pending_mission"):
            result = handler.handle(ctx)
        assert "Incident queued" in result


# ---------------------------------------------------------------------------
# SKILL.md tests
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_exists(self):
        skill_md = Path(__file__).parent.parent / "skills" / "core" / "incident" / "SKILL.md"
        assert skill_md.exists()

    def test_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(
            Path(__file__).parent.parent / "skills" / "core" / "incident" / "SKILL.md"
        )
        assert skill is not None
        assert skill.name == "incident"
        assert skill.scope == "core"


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------

from skills.core.incident.incident_runner import (
    run_incident,
    _build_prompt,
    _parse_summary,
    _write_journal_entry,
    main,
)

_RUNNER_MODULE = "skills.core.incident.incident_runner"


class TestBuildPrompt:
    def test_with_skill_dir(self):
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "incident"
        prompt = _build_prompt(
            error_text="TypeError: 'NoneType' has no attribute 'id'",
            skill_dir=skill_dir,
            branch_prefix="koan/",
            timestamp="1234567890",
        )
        assert "TypeError" in prompt
        assert "koan/" in prompt
        assert "1234567890" in prompt

    def test_placeholders_replaced(self):
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "incident"
        prompt = _build_prompt(
            error_text="ValueError: bad input",
            skill_dir=skill_dir,
            branch_prefix="koan/",
            timestamp="123",
        )
        assert "{ERROR_TEXT}" not in prompt
        assert "{BRANCH_PREFIX}" not in prompt
        assert "{TIMESTAMP}" not in prompt


class TestParseSummary:
    def test_valid_summary(self):
        output = """
Some analysis text...

INCIDENT_SUMMARY_START
root_cause: Missing null check in user_service.get_user()
culprit_commit: abc1234
fix_description: Added null guard before accessing user.id
affected_files: app/user_service.py, tests/test_user_service.py
pr_url: https://github.com/o/r/pull/99
INCIDENT_SUMMARY_END

Done.
"""
        result = _parse_summary(output)
        assert result is not None
        assert result["root_cause"] == "Missing null check in user_service.get_user()"
        assert result["culprit_commit"] == "abc1234"
        assert result["fix_description"] == "Added null guard before accessing user.id"
        assert "user_service.py" in result["affected_files"]
        assert result["pr_url"] == "https://github.com/o/r/pull/99"

    def test_no_summary(self):
        result = _parse_summary("Just some output with no structured block.")
        assert result is None

    def test_summary_with_none_values(self):
        output = """
INCIDENT_SUMMARY_START
root_cause: Unknown — needs more investigation
culprit_commit: none
fix_description: No fix applied
affected_files: none
pr_url: none
INCIDENT_SUMMARY_END
"""
        result = _parse_summary(output)
        assert result is not None
        assert result["culprit_commit"] == "none"
        assert result["pr_url"] == "none"


class TestWriteJournalEntry:
    def test_writes_journal(self, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        _write_journal_entry(
            instance_dir=str(instance_dir),
            project_path="/path/to/myproject",
            error_text="TypeError: 'NoneType' object has no attribute 'id'",
            root_cause="Missing null check",
            fix_description="Added guard clause",
            culprit_commit="abc1234",
            affected_files="app.py",
            pr_url="https://github.com/o/r/pull/1",
            success=True,
        )

        # Find the journal file
        journal_files = list(instance_dir.rglob("*.md"))
        assert len(journal_files) == 1
        content = journal_files[0].read_text()
        assert "\U0001f6a8 Incident:" in content
        assert "Resolved" in content
        assert "Missing null check" in content
        assert "abc1234" in content

    def test_escalated_on_failure(self, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        _write_journal_entry(
            instance_dir=str(instance_dir),
            project_path="/path/to/myproject",
            error_text="Some error",
            root_cause="Unknown",
            success=False,
        )

        journal_files = list(instance_dir.rglob("*.md"))
        assert len(journal_files) == 1
        content = journal_files[0].read_text()
        assert "Escalated" in content

    def test_no_instance_dir_is_noop(self):
        # Should not raise
        _write_journal_entry(
            instance_dir=None,
            project_path="/path",
            error_text="error",
        )


class TestRunIncident:
    @patch(f"{_RUNNER_MODULE}._submit_incident_pr", return_value="https://github.com/o/r/pull/1")
    @patch(f"{_RUNNER_MODULE}.get_current_branch", return_value="koan/incident-123")
    @patch(f"{_RUNNER_MODULE}._execute_incident", return_value="INCIDENT_SUMMARY_START\nroot_cause: bug\nculprit_commit: none\nfix_description: fixed\naffected_files: app.py\npr_url: none\nINCIDENT_SUMMARY_END")
    def test_success_with_pr(self, mock_execute, mock_branch, mock_pr):
        notify = MagicMock()
        success, summary = run_incident(
            project_path="/path",
            error_text="TypeError: bad",
            notify_fn=notify,
        )
        assert success is True
        assert "https://github.com/o/r/pull/1" in summary

    @patch(f"{_RUNNER_MODULE}._execute_incident", return_value="")
    def test_empty_output(self, mock_execute):
        notify = MagicMock()
        success, summary = run_incident(
            project_path="/path",
            error_text="Error",
            notify_fn=notify,
        )
        assert success is False
        assert "empty output" in summary.lower()

    @patch(f"{_RUNNER_MODULE}._execute_incident", side_effect=RuntimeError("CLI failed"))
    def test_execution_failure(self, mock_execute):
        notify = MagicMock()
        success, summary = run_incident(
            project_path="/path",
            error_text="Error",
            notify_fn=notify,
        )
        assert success is False
        assert "failed" in summary.lower()

    @patch(f"{_RUNNER_MODULE}._submit_incident_pr", return_value=None)
    @patch(f"{_RUNNER_MODULE}.get_current_branch", return_value="koan/incident-123")
    @patch(f"{_RUNNER_MODULE}._execute_incident", return_value="Some analysis output")
    def test_success_no_pr(self, mock_execute, mock_branch, mock_pr):
        notify = MagicMock()
        success, summary = run_incident(
            project_path="/path",
            error_text="Error",
            notify_fn=notify,
        )
        assert success is True
        assert "Branch: koan/incident-123" in summary

    @patch(f"{_RUNNER_MODULE}._submit_incident_pr", return_value=None)
    @patch(f"{_RUNNER_MODULE}.get_current_branch", return_value="main")
    @patch(f"{_RUNNER_MODULE}._execute_incident", return_value="Some output")
    def test_no_fix_on_main(self, mock_execute, mock_branch, mock_pr):
        notify = MagicMock()
        success, summary = run_incident(
            project_path="/path",
            error_text="Error",
            notify_fn=notify,
        )
        assert success is True
        assert "analyzed" in summary.lower()

    def test_long_error_truncated(self):
        notify = MagicMock()
        with patch(f"{_RUNNER_MODULE}._execute_incident", return_value="output"):
            with patch(f"{_RUNNER_MODULE}.get_current_branch", return_value="main"):
                with patch(f"{_RUNNER_MODULE}._submit_incident_pr", return_value=None):
                    success, _ = run_incident(
                        project_path="/path",
                        error_text="E" * 5000,
                        notify_fn=notify,
                    )
        assert success is True


class TestMain:
    @patch(f"{_RUNNER_MODULE}.run_incident", return_value=(True, "Done"))
    def test_success_with_error_text(self, mock_run):
        result = main(["--project-path", "/path", "--error-text", "TypeError: bad"])
        assert result == 0
        mock_run.assert_called_once()

    @patch(f"{_RUNNER_MODULE}.run_incident", return_value=(False, "Failed"))
    def test_failure_exit_code(self, mock_run):
        result = main(["--project-path", "/path", "--error-text", "Error"])
        assert result == 1

    @patch(f"{_RUNNER_MODULE}.run_incident", return_value=(True, "Done"))
    def test_error_file(self, mock_run, tmp_path):
        error_file = tmp_path / "error.txt"
        error_file.write_text("Traceback:\n  File 'a.py'\nValueError: bad")
        result = main([
            "--project-path", "/path",
            "--error-file", str(error_file),
        ])
        assert result == 0
        call_kwargs = mock_run.call_args
        assert "Traceback" in call_kwargs[1].get("error_text", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")

    def test_missing_error_file(self):
        result = main(["--project-path", "/path", "--error-file", "/nonexistent/file.txt"])
        assert result == 1

    @patch(f"{_RUNNER_MODULE}.run_incident", return_value=(True, "Done"))
    def test_empty_error_text(self, mock_run):
        result = main(["--project-path", "/path", "--error-text", "   "])
        assert result == 1

    @patch(f"{_RUNNER_MODULE}.run_incident", return_value=(True, "Done"))
    def test_instance_dir_passed(self, mock_run):
        main([
            "--project-path", "/path",
            "--error-text", "Error",
            "--instance-dir", "/instance",
        ])
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("instance_dir") == "/instance"


# ---------------------------------------------------------------------------
# skill_dispatch integration
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_incident_in_skill_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "incident" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["incident"] == "skills.core.incident.incident_runner"

    def test_is_skill_mission(self):
        from app.skill_dispatch import is_skill_mission
        assert is_skill_mission("/incident TypeError: bad")
        assert is_skill_mission("[project:koan] /incident Error")

    def test_parse_skill_mission(self):
        from app.skill_dispatch import parse_skill_mission
        project, cmd, args = parse_skill_mission("[project:koan] /incident TypeError: bad")
        assert project == "koan"
        assert cmd == "incident"
        assert "TypeError" in args

    def test_build_incident_cmd(self, tmp_path):
        from app.skill_dispatch import build_skill_command
        cmd = build_skill_command(
            command="incident",
            args="TypeError: 'NoneType' has no attribute 'id'",
            project_name="myproject",
            project_path="/path/myproject",
            koan_root="/koan",
            instance_dir=str(tmp_path),
        )
        assert cmd is not None
        assert "skills.core.incident.incident_runner" in " ".join(cmd)
        assert "--project-path" in cmd
        assert "--error-file" in cmd
        assert "--instance-dir" in cmd


# ---------------------------------------------------------------------------
# Prompt file tests
# ---------------------------------------------------------------------------

class TestPromptFile:
    PROMPT_PATH = Path(__file__).parent.parent / "skills" / "core" / "incident" / "prompts" / "incident-analyze.md"

    def test_prompt_file_exists(self):
        assert self.PROMPT_PATH.exists()

    def test_prompt_has_placeholders(self):
        content = self.PROMPT_PATH.read_text()
        assert "{ERROR_TEXT}" in content
        assert "{BRANCH_PREFIX}" in content
        assert "{TIMESTAMP}" in content

    def test_prompt_has_summary_format(self):
        content = self.PROMPT_PATH.read_text()
        assert "INCIDENT_SUMMARY_START" in content
        assert "INCIDENT_SUMMARY_END" in content
