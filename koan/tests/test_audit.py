"""Tests for the /audit skill — handler, runner, and parser."""

import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "audit" / "handler.py"


def _load_handler():
    """Load the audit handler module dynamically."""
    spec = importlib.util.spec_from_file_location("audit_handler", str(HANDLER_PATH))
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
        command_name="audit",
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

    def test_no_args_returns_error(self, handler, ctx):
        ctx.args = ""
        result = handler.handle(ctx)
        assert "\u274c" in result
        assert "Usage:" in result


class TestHandleQueueMission:
    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_named_project(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan"
        result = handler.handle(ctx)

        assert "Audit queued" in result
        assert "koan" in result
        mock_insert.assert_called_once()
        mission_entry = mock_insert.call_args[0][1]
        assert "[project:koan]" in mission_entry
        assert "/audit" in mission_entry

    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_with_extra_context(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan focus on error handling"
        result = handler.handle(ctx)

        assert "Audit queued" in result
        assert "focus: focus on error handling" in result
        mission_entry = mock_insert.call_args[0][1]
        assert "/audit focus on error handling" in mission_entry
        assert "[project:koan]" in mission_entry

    @patch("app.utils.resolve_project_path", return_value=None)
    @patch("app.utils.get_known_projects", return_value=[("web", "/path/web")])
    def test_unknown_project(self, mock_projects, mock_resolve, handler, ctx):
        ctx.args = "nonexistent"
        result = handler.handle(ctx)

        assert "\u274c" in result
        assert "nonexistent" in result
        assert "web" in result

    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_with_limit_override(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan focus on auth limit=10"
        result = handler.handle(ctx)

        assert "Audit queued" in result
        assert "limit=10" in result
        mission_entry = mock_insert.call_args[0][1]
        assert "limit=10" in mission_entry
        assert "/audit focus on auth" in mission_entry
        # limit=10 should not be in the context part
        assert "limit=10 limit=10" not in mission_entry

    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_default_limit_not_in_mission(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan"
        handler.handle(ctx)
        mission_entry = mock_insert.call_args[0][1]
        assert "limit=" not in mission_entry

    @patch("app.utils.resolve_project_path", return_value="/path/koan")
    @patch("app.utils.insert_pending_mission")
    def test_limit_without_context(self, mock_insert, mock_resolve, handler, ctx):
        ctx.args = "koan limit=3"
        result = handler.handle(ctx)

        assert "Audit queued" in result
        assert "limit=3" in result
        mission_entry = mock_insert.call_args[0][1]
        assert "limit=3" in mission_entry


# ---------------------------------------------------------------------------
# Runner tests — parsing
# ---------------------------------------------------------------------------

from skills.core.audit.audit_runner import (
    AuditFinding,
    DEFAULT_MAX_ISSUES,
    build_audit_prompt,
    parse_findings,
    prioritize_findings,
    _build_issue_body,
    _save_audit_report,
    create_issues,
    run_audit,
    main,
)


SAMPLE_OUTPUT = """\
Some preamble text from Claude.

---FINDING---
TITLE: refactor: extract duplicated errno-preservation pattern
SEVERITY: medium
CATEGORY: duplication
LOCATION: FileCheck.xs:105-152
PROBLEM: The errno-preservation pattern appears 3 times with identical code. If the pattern ever needs to change, all three instances must be updated manually.
WHY: Maintenance risk — a future change to one instance without updating the others would introduce subtle bugs.
SUGGESTED_FIX: Extract into a macro like LEAVE_PRESERVING_ERRNO().
EFFORT: small

---FINDING---
TITLE: fix: validate user input in parse_query
SEVERITY: high
CATEGORY: robustness
LOCATION: src/parser.py:42-58
PROBLEM: The parse_query function passes user input directly to a regex without escaping. Special regex characters in the input cause crashes.
WHY: User-facing bug that causes 500 errors on certain search queries.
SUGGESTED_FIX: Use re.escape() on the user input before passing to re.compile().
EFFORT: small

---FINDING---
TITLE: cleanup: remove unused legacy adapter
SEVERITY: low
CATEGORY: cleanup
LOCATION: src/adapters/legacy.py:1-120
PROBLEM: The LegacyAdapter class has no references in the codebase. It was superseded by NewAdapter in v2.0.
WHY: Dead code adds cognitive load and maintenance burden.
SUGGESTED_FIX: Delete the file after confirming no external consumers depend on it.
EFFORT: small
"""


class TestParseFindingsBasic:
    def test_parses_multiple_findings(self):
        findings = parse_findings(SAMPLE_OUTPUT)
        assert len(findings) == 3

    def test_first_finding_fields(self):
        findings = parse_findings(SAMPLE_OUTPUT)
        f = findings[0]
        assert f.title == "refactor: extract duplicated errno-preservation pattern"
        assert f.severity == "medium"
        assert f.category == "duplication"
        assert f.location == "FileCheck.xs:105-152"
        assert "errno" in f.problem.lower()
        assert f.effort == "small"

    def test_second_finding_severity(self):
        findings = parse_findings(SAMPLE_OUTPUT)
        assert findings[1].severity == "high"
        assert findings[1].category == "robustness"

    def test_empty_output(self):
        assert parse_findings("") == []

    def test_no_findings_in_output(self):
        assert parse_findings("Just some regular text without findings.") == []

    def test_invalid_finding_missing_title(self):
        raw = "---FINDING---\nSEVERITY: high\nLOCATION: foo.py:1\nPROBLEM: something\n"
        findings = parse_findings(raw)
        assert len(findings) == 0  # missing title = invalid

    def test_invalid_finding_missing_location(self):
        raw = "---FINDING---\nTITLE: fix something\nPROBLEM: it's broken\n"
        findings = parse_findings(raw)
        assert len(findings) == 0  # missing location = invalid


class TestPrioritizeFindings:
    def _make_finding(self, severity):
        return AuditFinding(
            title=f"{severity} issue",
            severity=severity,
            location="a.py:1",
            problem="broken",
        )

    def test_keeps_all_when_under_limit(self):
        findings = [self._make_finding("high"), self._make_finding("low")]
        result = prioritize_findings(findings, max_issues=5)
        assert len(result) == 2

    def test_truncates_to_limit(self):
        findings = [
            self._make_finding("low"),
            self._make_finding("medium"),
            self._make_finding("critical"),
            self._make_finding("high"),
        ]
        result = prioritize_findings(findings, max_issues=2)
        assert len(result) == 2
        assert result[0].severity == "critical"
        assert result[1].severity == "high"

    def test_default_limit_is_five(self):
        findings = [self._make_finding("low") for _ in range(8)]
        result = prioritize_findings(findings)
        assert len(result) == DEFAULT_MAX_ISSUES

    def test_preserves_order_within_same_severity(self):
        findings = [
            AuditFinding(title="A", severity="medium", location="a:1", problem="p"),
            AuditFinding(title="B", severity="medium", location="b:1", problem="p"),
            AuditFinding(title="C", severity="medium", location="c:1", problem="p"),
        ]
        result = prioritize_findings(findings, max_issues=2)
        assert result[0].title == "A"
        assert result[1].title == "B"


class TestLimitExtraction:
    """Test limit=N parsing from handler."""

    def test_extract_limit_present(self):
        handler = _load_handler()
        limit, cleaned = handler._extract_limit("focus on auth limit=10")
        assert limit == 10
        assert cleaned == "focus on auth"

    def test_extract_limit_absent(self):
        handler = _load_handler()
        limit, cleaned = handler._extract_limit("focus on auth")
        assert limit == handler.DEFAULT_MAX_ISSUES
        assert cleaned == "focus on auth"

    def test_extract_limit_only(self):
        handler = _load_handler()
        limit, cleaned = handler._extract_limit("limit=3")
        assert limit == 3
        assert cleaned == ""

    def test_extract_limit_case_insensitive(self):
        handler = _load_handler()
        limit, cleaned = handler._extract_limit("focus LIMIT=7")
        assert limit == 7
        assert cleaned == "focus"

    def test_extract_limit_zero_becomes_one(self):
        handler = _load_handler()
        limit, _ = handler._extract_limit("limit=0")
        assert limit == 1


class TestAuditFinding:
    def test_is_valid_with_required_fields(self):
        f = AuditFinding(title="fix X", problem="broken", location="a.py:1")
        assert f.is_valid()

    def test_is_invalid_without_title(self):
        f = AuditFinding(problem="broken", location="a.py:1")
        assert not f.is_valid()

    def test_is_invalid_without_problem(self):
        f = AuditFinding(title="fix X", location="a.py:1")
        assert not f.is_valid()

    def test_is_invalid_without_location(self):
        f = AuditFinding(title="fix X", problem="broken")
        assert not f.is_valid()


class TestBuildIssueBody:
    def test_contains_all_sections(self):
        finding = AuditFinding(
            title="fix: something",
            severity="high",
            category="robustness",
            location="src/foo.py:10-20",
            problem="It's broken.",
            why="Users see errors.",
            suggested_fix="Add validation.",
            effort="small",
        )
        body = _build_issue_body(finding)
        assert "## Problem" in body
        assert "## Why This Matters" in body
        assert "## Suggested Fix" in body
        assert "## Details" in body
        assert "High" in body
        assert "`src/foo.py:10-20`" in body
        assert "robustness" in body
        assert "Quick fix" in body
        assert "K\u014dan" in body

    def test_severity_icons(self):
        for severity in ("critical", "high", "medium", "low"):
            f = AuditFinding(
                title="t", severity=severity,
                problem="p", location="l",
            )
            body = _build_issue_body(f)
            assert severity.capitalize() in body


class TestBuildPrompt:
    def test_prompt_contains_project_name(self):
        prompt = build_audit_prompt(
            "myproject",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "audit",
        )
        assert "myproject" in prompt

    def test_prompt_contains_instructions(self):
        prompt = build_audit_prompt(
            "test",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "audit",
        )
        assert "FINDING" in prompt
        assert "audit" in prompt.lower()

    def test_prompt_with_extra_context(self):
        prompt = build_audit_prompt(
            "test", extra_context="focus on auth",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "audit",
        )
        assert "focus on auth" in prompt
        assert "Additional Focus" in prompt

    def test_prompt_without_extra_context(self):
        prompt = build_audit_prompt(
            "test",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "audit",
        )
        assert "Additional Focus" not in prompt

    def test_prompt_default_max_issues(self):
        prompt = build_audit_prompt(
            "test",
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "audit",
        )
        assert f"at most {DEFAULT_MAX_ISSUES} findings" in prompt

    def test_prompt_custom_max_issues(self):
        prompt = build_audit_prompt(
            "test", max_issues=12,
            skill_dir=Path(__file__).parent.parent / "skills" / "core" / "audit",
        )
        assert "at most 12 findings" in prompt


class TestSaveAuditReport:
    def test_creates_report_file(self, tmp_path):
        findings = [
            AuditFinding(
                title="fix X", severity="high",
                location="a.py:1", problem="broken",
            ),
        ]
        path = _save_audit_report(tmp_path, "myproj", findings, ["https://github.com/o/r/issues/1"])
        assert path.exists()
        content = path.read_text()
        assert "Last audit:" in content
        assert "Findings: 1" in content
        assert "fix X" in content
        assert "issues/1" in content

    def test_creates_directory_structure(self, tmp_path):
        _save_audit_report(tmp_path, "newproj", [], [])
        assert (tmp_path / "memory" / "projects" / "newproj").exists()

    def test_handles_fewer_urls_than_findings(self, tmp_path):
        findings = [
            AuditFinding(title="a", severity="h", location="x:1", problem="p"),
            AuditFinding(title="b", severity="m", location="y:2", problem="q"),
        ]
        path = _save_audit_report(tmp_path, "proj", findings, ["url1"])
        content = path.read_text()
        assert "url1" in content
        assert "no issue created" in content


class TestCreateIssues:
    @patch("app.github.resolve_target_repo", return_value="upstream/repo")
    @patch("app.github.issue_create")
    def test_creates_issues_for_findings(self, mock_create, mock_repo):
        mock_create.side_effect = [
            "https://github.com/o/r/issues/1\n",
            "https://github.com/o/r/issues/2\n",
        ]
        findings = [
            AuditFinding(title="fix A", severity="high", location="a.py:1", problem="p1"),
            AuditFinding(title="fix B", severity="low", location="b.py:2", problem="p2"),
        ]
        urls = create_issues(findings, "/path/proj")

        assert len(urls) == 2
        assert mock_create.call_count == 2
        # Check repo targeting
        assert mock_create.call_args_list[0][1]["repo"] == "upstream/repo"

    @patch("app.github.resolve_target_repo", return_value=None)
    @patch("app.github.issue_create")
    def test_no_upstream_uses_local(self, mock_create, mock_repo):
        mock_create.return_value = "https://github.com/o/r/issues/1\n"
        findings = [
            AuditFinding(title="fix A", severity="high", location="a.py:1", problem="p"),
        ]
        create_issues(findings, "/path/proj")
        assert mock_create.call_args[1]["repo"] is None

    @patch("app.github.resolve_target_repo", return_value="upstream/repo")
    @patch("app.github.issue_create")
    def test_notify_fn_receives_issue_url(self, mock_create, mock_repo):
        mock_create.side_effect = [
            "https://github.com/o/r/issues/1\n",
            "https://github.com/o/r/issues/2\n",
        ]
        findings = [
            AuditFinding(title="fix A", severity="high", location="a.py:1", problem="p1"),
            AuditFinding(title="fix B", severity="low", location="b.py:2", problem="p2"),
        ]
        notify = MagicMock()
        create_issues(findings, "/path/proj", notify_fn=notify)

        # Should get 4 calls: "Creating issue" + URL for each finding
        assert notify.call_count == 4
        # Odd calls are "Creating issue ...", even calls are the URL
        url_calls = [c.args[0] for c in notify.call_args_list if "github.com" in c.args[0]]
        assert len(url_calls) == 2
        assert "https://github.com/o/r/issues/1" in url_calls[0]
        assert "https://github.com/o/r/issues/2" in url_calls[1]

    @patch("app.github.resolve_target_repo", return_value=None)
    @patch("app.github.issue_create", side_effect=RuntimeError("API error"))
    def test_continues_on_failure(self, mock_create, mock_repo):
        findings = [
            AuditFinding(title="fix A", severity="high", location="a.py:1", problem="p"),
            AuditFinding(title="fix B", severity="low", location="b.py:2", problem="q"),
        ]
        urls = create_issues(findings, "/path/proj")
        assert len(urls) == 0
        assert mock_create.call_count == 2


class TestRunAudit:
    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="audit prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", return_value=SAMPLE_OUTPUT)
    @patch("skills.core.audit.audit_runner.create_issues")
    def test_full_pipeline_success(self, mock_issues, mock_scan, mock_prompt, tmp_path):
        mock_issues.return_value = [
            "https://github.com/o/r/issues/1",
            "https://github.com/o/r/issues/2",
            "https://github.com/o/r/issues/3",
        ]
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        success, summary = run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            notify_fn=notify,
        )

        assert success
        assert "3 findings" in summary
        assert "3 GitHub issues created" in summary
        assert "audit.md" in summary

    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", return_value=SAMPLE_OUTPUT)
    @patch("skills.core.audit.audit_runner.create_issues")
    def test_passes_extra_context(self, mock_issues, mock_scan, mock_prompt, tmp_path):
        mock_issues.return_value = []
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            extra_context="focus on auth",
            notify_fn=MagicMock(),
        )

        mock_prompt.assert_called_once()
        assert mock_prompt.call_args[0][1] == "focus on auth"

    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", side_effect=RuntimeError("quota"))
    def test_scan_failure(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        success, summary = run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            notify_fn=MagicMock(),
        )

        assert not success
        assert "failed" in summary.lower()

    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", return_value="")
    def test_empty_output(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        success, summary = run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            notify_fn=MagicMock(),
        )

        assert not success
        assert "no output" in summary.lower()

    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", return_value="No findings here.")
    def test_no_findings(self, mock_scan, mock_prompt, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        success, summary = run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            notify_fn=notify,
        )

        assert success
        assert "no findings" in summary.lower()

    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", return_value=SAMPLE_OUTPUT)
    @patch("skills.core.audit.audit_runner.create_issues")
    def test_max_issues_truncates_findings(self, mock_issues, mock_scan, mock_prompt, tmp_path):
        mock_issues.return_value = ["https://github.com/o/r/issues/1"]
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        notify = MagicMock()

        # SAMPLE_OUTPUT has 3 findings, limit to 1
        success, summary = run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            max_issues=1,
            notify_fn=notify,
        )

        assert success
        assert "1 findings" in summary
        # create_issues should receive only 1 finding
        assert len(mock_issues.call_args[0][0]) == 1
        # The kept finding should be the highest severity one (high)
        assert mock_issues.call_args[0][0][0].severity == "high"

    @patch("skills.core.audit.audit_runner.build_audit_prompt", return_value="prompt")
    @patch("skills.core.audit.audit_runner._run_claude_audit", return_value=SAMPLE_OUTPUT)
    @patch("skills.core.audit.audit_runner.create_issues")
    def test_max_issues_passed_to_prompt(self, mock_issues, mock_scan, mock_prompt, tmp_path):
        mock_issues.return_value = []
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        run_audit(
            project_path="/path/proj",
            project_name="proj",
            instance_dir=str(instance_dir),
            max_issues=8,
            notify_fn=MagicMock(),
        )

        assert mock_prompt.call_args[1].get("max_issues") == 8


class TestCLI:
    @patch("skills.core.audit.audit_runner.run_audit", return_value=(True, "Done"))
    def test_main_success(self, mock_run, tmp_path):
        exit_code = main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        assert exit_code == 0
        mock_run.assert_called_once()

    @patch("skills.core.audit.audit_runner.run_audit", return_value=(False, "Failed"))
    def test_main_failure(self, mock_run, tmp_path):
        exit_code = main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        assert exit_code == 1

    @patch("skills.core.audit.audit_runner.run_audit", return_value=(True, "Done"))
    def test_main_with_context(self, mock_run, tmp_path):
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
            "--context", "focus on auth",
        ])
        _, kwargs = mock_run.call_args
        assert kwargs.get("extra_context") == "focus on auth"

    @patch("skills.core.audit.audit_runner.run_audit", return_value=(True, "Done"))
    def test_main_with_context_file(self, mock_run, tmp_path):
        ctx_file = tmp_path / "context.txt"
        ctx_file.write_text("look at the database layer")
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
            "--context-file", str(ctx_file),
        ])
        _, kwargs = mock_run.call_args
        assert kwargs.get("extra_context") == "look at the database layer"

    @patch("skills.core.audit.audit_runner.run_audit", return_value=(True, "Done"))
    def test_main_sets_skill_dir(self, mock_run, tmp_path):
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        _, kwargs = mock_run.call_args
        skill_dir = kwargs.get("skill_dir")
        assert skill_dir is not None
        assert skill_dir.name == "audit"

    @patch("skills.core.audit.audit_runner.run_audit", return_value=(True, "Done"))
    def test_main_with_max_issues(self, mock_run, tmp_path):
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
            "--max-issues", "8",
        ])
        _, kwargs = mock_run.call_args
        assert kwargs.get("max_issues") == 8

    @patch("skills.core.audit.audit_runner.run_audit", return_value=(True, "Done"))
    def test_main_default_max_issues(self, mock_run, tmp_path):
        main([
            "--project-path", "/path/proj",
            "--project-name", "proj",
            "--instance-dir", str(tmp_path),
        ])
        _, kwargs = mock_run.call_args
        assert kwargs.get("max_issues") == DEFAULT_MAX_ISSUES


# ---------------------------------------------------------------------------
# skill_dispatch integration tests
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_audit_in_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "audit" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["audit"] == "skills.core.audit.audit_runner"

    def test_build_skill_command(self):
        from app.skill_dispatch import build_skill_command

        cmd = build_skill_command(
            command="audit",
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

    def test_build_skill_command_with_context(self):
        from app.skill_dispatch import build_skill_command

        cmd = build_skill_command(
            command="audit",
            args="focus on auth module",
            project_name="myproj",
            project_path="/path/myproj",
            koan_root="/koan",
            instance_dir="/koan/instance",
        )

        assert cmd is not None
        assert "--context-file" in cmd

    def test_parse_skill_mission(self):
        from app.skill_dispatch import parse_skill_mission

        project, command, args = parse_skill_mission("/audit")
        assert command == "audit"
        assert args == ""

    def test_parse_with_project_tag(self):
        from app.skill_dispatch import parse_skill_mission

        project, command, args = parse_skill_mission(
            "[project:koan] /audit focus on error handling"
        )
        assert project == "koan"
        assert command == "audit"
        assert args == "focus on error handling"

    def test_build_skill_command_with_limit(self):
        from app.skill_dispatch import build_skill_command

        cmd = build_skill_command(
            command="audit",
            args="focus on auth limit=8",
            project_name="myproj",
            project_path="/path/myproj",
            koan_root="/koan",
            instance_dir="/koan/instance",
        )

        assert cmd is not None
        assert "--max-issues" in cmd
        idx = cmd.index("--max-issues")
        assert cmd[idx + 1] == "8"
