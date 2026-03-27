"""Tests for pr_quality.py — post-mission PR quality pipeline."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Phase 1: scan_changes
# ---------------------------------------------------------------------------

class TestParseAddedLines:
    """Test _parse_diff_added_lines."""

    def test_parses_added_lines(self):
        from app.pr_quality import _parse_diff_added_lines

        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,5 @@\n"
            " existing line\n"
            "+print('debug')\n"
            "+new_function()\n"
            " another line\n"
        )
        result = _parse_diff_added_lines(diff)
        assert len(result) == 2
        assert result[0] == ("foo.py", 2, "print('debug')")
        assert result[1] == ("foo.py", 3, "new_function()")

    def test_handles_multiple_files(self):
        from app.pr_quality import _parse_diff_added_lines

        diff = (
            "+++ b/a.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
            "+++ b/b.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+line3\n"
        )
        result = _parse_diff_added_lines(diff)
        assert len(result) == 3
        assert result[0][0] == "a.py"
        assert result[2][0] == "b.py"

    def test_skips_removed_lines(self):
        from app.pr_quality import _parse_diff_added_lines

        diff = (
            "+++ b/foo.py\n"
            "@@ -1,3 +1,2 @@\n"
            " keep\n"
            "-removed\n"
            "+added\n"
        )
        result = _parse_diff_added_lines(diff)
        assert len(result) == 1
        assert result[0][2] == "added"

    def test_empty_diff(self):
        from app.pr_quality import _parse_diff_added_lines
        assert _parse_diff_added_lines("") == []

    def test_line_numbers_track_correctly_with_removals(self):
        from app.pr_quality import _parse_diff_added_lines

        diff = (
            "+++ b/foo.py\n"
            "@@ -1,4 +1,4 @@\n"
            " line1\n"
            "-old_line2\n"
            "+new_line2\n"
            " line3\n"
            "+inserted\n"
        )
        result = _parse_diff_added_lines(diff)
        assert result[0] == ("foo.py", 2, "new_line2")
        assert result[1] == ("foo.py", 4, "inserted")


class TestShouldSkipFile:
    """Test _should_skip_file."""

    def test_skips_test_files(self):
        from app.pr_quality import _should_skip_file
        assert _should_skip_file("tests/test_foo.py") is True
        assert _should_skip_file("src/test/foo.py") is True
        assert _should_skip_file("foo_test.py") is True
        assert _should_skip_file("foo.test.js") is True
        assert _should_skip_file("foo.spec.ts") is True

    def test_skips_config_files(self):
        from app.pr_quality import _should_skip_file
        assert _should_skip_file("package.json") is True
        assert _should_skip_file("config.yaml") is True
        assert _should_skip_file("pyproject.toml") is True

    def test_skips_generated_files(self):
        from app.pr_quality import _should_skip_file
        assert _should_skip_file("bundle.min.js") is True
        assert _should_skip_file("styles.min.css") is True

    def test_keeps_source_files(self):
        from app.pr_quality import _should_skip_file
        assert _should_skip_file("src/app.py") is False
        assert _should_skip_file("main.js") is False
        assert _should_skip_file("lib/utils.rs") is False


class TestScanChanges:
    """Test scan_changes."""

    @patch("app.pr_quality.run_git_strict")
    def test_detects_debug_prints(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,  # rev-parse --verify upstream/main
            "koan/test-branch",  # rev-parse --abbrev-ref HEAD
            (  # git diff
                "+++ b/src/main.py\n"
                "@@ -1,1 +1,3 @@\n"
                " existing\n"
                "+print('debug output')\n"
                "+real_code()\n"
            ),
        ]

        result = scan_changes("/project")
        assert result["clean"] is False
        assert any(i["type"] == "debug" and "print" in i["message"]
                    for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_detects_console_log(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,  # base ref check
            "koan/feature",  # branch
            (
                "+++ b/src/app.js\n"
                "@@ -1,1 +1,2 @@\n"
                "+console.log('test')\n"
            ),
        ]

        result = scan_changes("/project")
        assert result["clean"] is False
        assert any(i["message"] == "console.log statement" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_detects_todo_markers(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,
            "koan/feature",
            (
                "+++ b/src/main.py\n"
                "@@ -1,1 +1,2 @@\n"
                "+# TODO fix this later\n"
            ),
        ]

        result = scan_changes("/project")
        assert any(i["type"] == "marker" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_detects_secrets(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,
            "koan/feature",
            (
                "+++ b/src/config.py\n"
                "@@ -1,1 +1,2 @@\n"
                "+api_key = 'sk-abcdefghijklmnopqrstuvwxyz1234567890'\n"
            ),
        ]

        result = scan_changes("/project")
        assert any(i["type"] == "secret" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_skips_test_files(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,
            "koan/feature",
            (
                "+++ b/tests/test_main.py\n"
                "@@ -1,1 +1,2 @@\n"
                "+print('debug in test')\n"
            ),
        ]

        result = scan_changes("/project")
        assert result["clean"] is True

    @patch("app.pr_quality.run_git_strict")
    def test_returns_clean_on_main(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,  # base ref check
            "main",  # branch
        ]

        result = scan_changes("/project")
        assert result["clean"] is True
        assert result["issues"] == []

    @patch("app.pr_quality.run_git_strict")
    def test_returns_clean_no_base_ref(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = RuntimeError("no ref")

        result = scan_changes("/project")
        assert result["clean"] is True

    @patch("app.pr_quality.run_git_strict")
    def test_returns_clean_on_empty_diff(self, mock_git):
        from app.pr_quality import scan_changes

        mock_git.side_effect = [
            None,
            "koan/feature",
            "",
        ]

        result = scan_changes("/project")
        assert result["clean"] is True


class TestCheckLargeChanges:
    """Test _check_large_changes."""

    def test_flags_large_file(self):
        from app.pr_quality import _check_large_changes

        lines = ["+++ b/big.py\n"]
        lines.extend(["+x\n" for _ in range(501)])
        diff = "".join(lines)

        issues = _check_large_changes(diff)
        assert len(issues) == 1
        assert issues[0]["type"] == "large_change"
        assert "501" in issues[0]["message"]

    def test_no_flag_for_small_file(self):
        from app.pr_quality import _check_large_changes

        lines = ["+++ b/small.py\n"]
        lines.extend(["+x\n" for _ in range(100)])
        diff = "".join(lines)

        assert _check_large_changes(diff) == []


# ---------------------------------------------------------------------------
# Phase 3: validate_branch
# ---------------------------------------------------------------------------

class TestValidateBranch:
    """Test validate_branch."""

    @patch("app.pr_quality.run_git_strict")
    def test_valid_branch(self, mock_git):
        from app.pr_quality import validate_branch

        mock_git.side_effect = [
            "koan/feature-x",  # current branch
            None,  # base ref check (upstream/main exists)
            "abc1234 feat: add feature x",  # log
            None,  # origin/koan/feature-x exists
            "def5678 feat: previous commit\nghi9012 fix: another fix",  # base commits
        ]

        result = validate_branch("/project", "koan/")
        assert result["valid"] is True
        assert result["issues"] == []

    @patch("app.pr_quality.run_git_strict")
    def test_wrong_prefix(self, mock_git):
        from app.pr_quality import validate_branch

        mock_git.side_effect = [
            "feature/wrong-prefix",  # branch
            None,  # base ref
            "abc1234 some commit",  # log
            None,  # remote check
            "def5678 feat: a\nghi9012 fix: b",  # base commits
        ]

        result = validate_branch("/project", "koan/")
        assert any(i["type"] == "naming" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_empty_branch(self, mock_git):
        from app.pr_quality import validate_branch

        mock_git.side_effect = [
            "koan/empty",
            None,  # base ref
            "",  # empty log
        ]

        result = validate_branch("/project", "koan/")
        assert result["valid"] is False
        assert any(i["type"] == "empty" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_fixup_commits(self, mock_git):
        from app.pr_quality import validate_branch

        mock_git.side_effect = [
            "koan/feature",
            None,
            "abc1234 fixup! feat: original commit\ndef5678 feat: original commit",
            None,  # remote check
            "111 fix: a\n222 feat: b\n333 chore: c",  # base commits (conventional)
        ]

        result = validate_branch("/project", "koan/")
        assert any(i["type"] == "fixup" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_unpushed_branch(self, mock_git):
        from app.pr_quality import validate_branch

        mock_git.side_effect = [
            "koan/unpushed",
            None,
            "abc1234 feat: something",
            RuntimeError("no such ref"),  # remote check fails
            "111 fix: a\n222 feat: b\n333 chore: c",
        ]

        result = validate_branch("/project", "koan/")
        assert any(i["type"] == "unpushed" for i in result["issues"])

    @patch("app.pr_quality.run_git_strict")
    def test_on_main_returns_valid(self, mock_git):
        from app.pr_quality import validate_branch

        mock_git.return_value = "main"

        result = validate_branch("/project", "koan/")
        assert result["valid"] is True


class TestProjectUsesConventionalCommits:
    """Test _project_uses_conventional_commits."""

    @patch("app.pr_quality.run_git_strict")
    def test_detects_conventional(self, mock_git):
        from app.pr_quality import _project_uses_conventional_commits

        mock_git.return_value = (
            "abc feat: add login\n"
            "def fix: typo\n"
            "ghi chore: update deps\n"
        )
        assert _project_uses_conventional_commits("/project", "upstream/main") is True

    @patch("app.pr_quality.run_git_strict")
    def test_detects_non_conventional(self, mock_git):
        from app.pr_quality import _project_uses_conventional_commits

        mock_git.return_value = (
            "abc Update readme\n"
            "def Fix the thing\n"
            "ghi Add new feature\n"
        )
        assert _project_uses_conventional_commits("/project", "upstream/main") is False


# ---------------------------------------------------------------------------
# Phase 4: PR enrichment
# ---------------------------------------------------------------------------

class TestEnrichPrDescription:
    """Test enrich_pr_description."""

    @patch("app.github.run_gh")
    @patch("app.pr_quality.run_git_strict")
    def test_enriches_existing_pr(self, mock_git, mock_gh):
        from app.pr_quality import enrich_pr_description

        mock_git.return_value = "koan/feature"
        mock_gh.side_effect = [
            json.dumps({"number": 42, "body": "Original body", "url": "https://github.com/o/r/pull/42"}),
            "",  # pr edit response
        ]

        report = {"scan": {"clean": True, "issues": []}, "tests": {"passed": True, "details": "OK", "skipped": False}, "branch": {"valid": True, "issues": []}}

        with patch("app.pr_quality._get_base_ref", return_value="upstream/main"), \
             patch("app.claude_step._get_diffstat", return_value="3 files changed"):
            result = enrich_pr_description("/project", report)

        assert result == "https://github.com/o/r/pull/42"
        # Check that pr edit was called with enriched body
        edit_call = mock_gh.call_args_list[1]
        body_arg = edit_call[0][4]  # args: "pr", "edit", num, "--body", body
        assert "Quality Report" in body_arg

    @patch("app.github.run_gh")
    @patch("app.pr_quality.run_git_strict")
    def test_skips_on_main(self, mock_git, mock_gh):
        from app.pr_quality import enrich_pr_description

        mock_git.return_value = "main"
        result = enrich_pr_description("/project", {})
        assert result is None
        mock_gh.assert_not_called()

    @patch("app.github.run_gh")
    @patch("app.pr_quality.run_git_strict")
    def test_skips_no_pr(self, mock_git, mock_gh):
        from app.pr_quality import enrich_pr_description

        mock_git.return_value = "koan/feature"
        mock_gh.side_effect = RuntimeError("no PR")

        result = enrich_pr_description("/project", {})
        assert result is None

    @patch("app.github.run_gh")
    @patch("app.pr_quality.run_git_strict")
    def test_replaces_previous_report(self, mock_git, mock_gh):
        from app.pr_quality import enrich_pr_description

        mock_git.return_value = "koan/feature"
        existing_body = "Original body\n\n---\n### Quality Report\n\nOld report content"
        mock_gh.side_effect = [
            json.dumps({"number": 42, "body": existing_body, "url": "https://github.com/o/r/pull/42"}),
            "",
        ]

        report = {"scan": {"clean": True, "issues": []}, "tests": {}, "branch": {"valid": True, "issues": []}}

        with patch("app.pr_quality._get_base_ref", return_value="upstream/main"), \
             patch("app.claude_step._get_diffstat", return_value=""):
            enrich_pr_description("/project", report)

        edit_call = mock_gh.call_args_list[1]
        body = edit_call[0][4]  # args: "pr", "edit", num, "--body", body
        # Should have exactly one Quality Report section
        assert body.count("### Quality Report") == 1
        assert "Old report content" not in body


class TestBuildQualityReportSection:
    """Test _build_quality_report_section."""

    def test_clean_report(self):
        from app.pr_quality import _build_quality_report_section

        report = {
            "scan": {"clean": True, "issues": []},
            "tests": {"passed": True, "details": "42 passed", "skipped": False},
            "branch": {"valid": True, "issues": []},
        }

        with patch("app.pr_quality._get_base_ref", return_value="upstream/main"), \
             patch("app.claude_step._get_diffstat", return_value="3 files changed"):
            section = _build_quality_report_section(report, "/project")

        assert "clean" in section
        assert "passed" in section
        assert "3 files changed" in section

    def test_report_with_issues(self):
        from app.pr_quality import _build_quality_report_section

        report = {
            "scan": {"clean": False, "issues": [
                {"type": "debug", "file": "main.py", "line": 10, "message": "debug print"},
            ]},
            "tests": {"passed": False, "details": "3 failed", "skipped": False},
            "branch": {"valid": False, "issues": [
                {"type": "fixup", "message": "Unsquashed fixup commit"},
            ]},
        }

        with patch("app.pr_quality._get_base_ref", return_value=None):
            section = _build_quality_report_section(report, "/project")

        assert "1 issue(s) found" in section
        assert "main.py:10" in section
        assert "failed" in section
        assert "Unsquashed fixup commit" in section


# ---------------------------------------------------------------------------
# Phase 5: Quality gate
# ---------------------------------------------------------------------------

class TestShouldBlockAutoMerge:
    """Test should_block_auto_merge."""

    def test_off_mode_never_blocks(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": False, "issues": [
                {"type": "secret", "file": "x", "line": 1, "message": "key"},
            ]},
            "tests": {"passed": False},
        }
        assert should_block_auto_merge(report, "off") is False

    def test_warn_mode_never_blocks(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": False, "issues": [
                {"type": "secret", "file": "x", "line": 1, "message": "key"},
            ]},
        }
        assert should_block_auto_merge(report, "warn") is False

    def test_strict_blocks_on_secrets(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": False, "issues": [
                {"type": "secret", "file": "x", "line": 1, "message": "API key"},
            ]},
        }
        assert should_block_auto_merge(report, "strict") is True

    def test_strict_does_not_block_on_debug(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": False, "issues": [
                {"type": "debug", "file": "x", "line": 1, "message": "print"},
            ]},
        }
        assert should_block_auto_merge(report, "strict") is False

    def test_strict_blocks_on_test_failure(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": True, "issues": []},
            "tests": {"passed": False, "skipped": False},
        }
        assert should_block_auto_merge(report, "strict") is True

    def test_strict_allows_skipped_tests(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": True, "issues": []},
            "tests": {"passed": True, "skipped": True},
        }
        assert should_block_auto_merge(report, "strict") is False

    def test_clean_report_not_blocked(self):
        from app.pr_quality import should_block_auto_merge

        report = {
            "scan": {"clean": True, "issues": []},
            "tests": {"passed": True, "skipped": False},
        }
        assert should_block_auto_merge(report, "strict") is False


class TestPostQualityComment:
    """Test post_quality_comment."""

    @patch("app.github.run_gh")
    def test_posts_comment_on_issues(self, mock_gh):
        from app.pr_quality import post_quality_comment

        report = {
            "scan": {"clean": False, "issues": [
                {"type": "debug", "file": "main.py", "line": 5, "message": "debug print"},
            ]},
            "tests": {"passed": True, "skipped": False},
            "branch": {"valid": True, "issues": []},
        }

        result = post_quality_comment("/project", report)
        assert result is True
        mock_gh.assert_called_once()
        comment_body = mock_gh.call_args[0][3]  # args: "pr", "comment", "--body", body
        assert "Quality Gate Warning" in comment_body

    @patch("app.github.run_gh")
    def test_no_comment_when_clean(self, mock_gh):
        from app.pr_quality import post_quality_comment

        report = {
            "scan": {"clean": True, "issues": []},
            "tests": {"passed": True, "skipped": False},
            "branch": {"valid": True, "issues": []},
        }

        result = post_quality_comment("/project", report)
        assert result is False
        mock_gh.assert_not_called()

    @patch("app.github.run_gh")
    def test_includes_test_failure_in_comment(self, mock_gh):
        from app.pr_quality import post_quality_comment

        report = {
            "scan": {"clean": True, "issues": []},
            "tests": {"passed": False, "details": "3 failed, 10 passed", "skipped": False},
            "branch": {"valid": True, "issues": []},
        }

        post_quality_comment("/project", report)
        comment_body = mock_gh.call_args[0][3]  # args: "pr", "comment", "--body", body
        assert "Tests failed" in comment_body
        assert "3 failed" in comment_body


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

class TestRunQualityPipeline:
    """Test run_quality_pipeline."""

    @patch("app.pr_quality.run_git_strict")
    def test_skips_on_main(self, mock_git):
        from app.pr_quality import run_quality_pipeline

        mock_git.return_value = "main"
        result = run_quality_pipeline("/project", "koan/")
        assert result["scan"] == {}
        assert result["tests"] == {}

    @patch("app.pr_quality.post_quality_comment")
    @patch("app.pr_quality.enrich_pr_description")
    @patch("app.pr_quality.validate_branch")
    @patch("app.pr_quality.scan_changes")
    @patch("app.pr_quality.run_git_strict")
    def test_runs_all_phases(self, mock_git, mock_scan, mock_branch, mock_enrich, mock_comment):
        from app.pr_quality import run_quality_pipeline

        mock_git.return_value = "koan/feature"
        mock_scan.return_value = {"clean": True, "issues": []}
        mock_branch.return_value = {"valid": True, "issues": []}
        mock_enrich.return_value = "https://github.com/o/r/pull/1"

        with patch("app.pr_review.detect_test_command", return_value="make test"), \
             patch("app.claude_step.run_project_tests", return_value={"passed": True, "output": "", "details": "OK"}):
            result = run_quality_pipeline("/project", "koan/")

        assert result["scan"]["clean"] is True
        assert result["tests"]["passed"] is True
        assert result["branch"]["valid"] is True
        assert result["pr_enriched"] == "https://github.com/o/r/pull/1"
        assert result["gate_blocked"] is False

    @patch("app.pr_quality.run_git_strict")
    def test_skips_wrong_prefix(self, mock_git):
        from app.pr_quality import run_quality_pipeline

        mock_git.return_value = "other/feature"
        result = run_quality_pipeline("/project", "koan/")
        assert result["scan"] == {}

    @patch("app.pr_quality.enrich_pr_description")
    @patch("app.pr_quality.validate_branch")
    @patch("app.pr_quality.scan_changes")
    @patch("app.pr_quality.run_git_strict")
    def test_skips_tests_when_disabled(self, mock_git, mock_scan, mock_branch, mock_enrich):
        from app.pr_quality import run_quality_pipeline

        mock_git.return_value = "koan/feature"
        mock_scan.return_value = {"clean": True, "issues": []}
        mock_branch.return_value = {"valid": True, "issues": []}
        mock_enrich.return_value = None

        result = run_quality_pipeline("/project", "koan/", run_tests=False)
        assert result["tests"] == {}

    @patch("app.pr_quality.post_quality_comment")
    @patch("app.pr_quality.enrich_pr_description")
    @patch("app.pr_quality.validate_branch")
    @patch("app.pr_quality.scan_changes")
    @patch("app.pr_quality.run_git_strict")
    def test_status_callback_called(self, mock_git, mock_scan, mock_branch, mock_enrich, mock_comment):
        from app.pr_quality import run_quality_pipeline

        mock_git.return_value = "koan/feature"
        mock_scan.return_value = {"clean": True, "issues": []}
        mock_branch.return_value = {"valid": True, "issues": []}
        mock_enrich.return_value = None

        callback = MagicMock()

        with patch("app.pr_review.detect_test_command", return_value=None):
            run_quality_pipeline("/project", "koan/", status_callback=callback)

        # Should have been called for each phase
        calls = [c[0][0] for c in callback.call_args_list]
        assert "scanning changes" in calls
        assert "running tests" in calls
        assert "validating branch" in calls
        assert "enriching PR description" in calls

    @patch("app.pr_quality.post_quality_comment")
    @patch("app.pr_quality.enrich_pr_description")
    @patch("app.pr_quality.validate_branch")
    @patch("app.pr_quality.scan_changes")
    @patch("app.pr_quality.run_git_strict")
    def test_handles_scan_exception(self, mock_git, mock_scan, mock_branch, mock_enrich, mock_comment):
        from app.pr_quality import run_quality_pipeline

        mock_git.return_value = "koan/feature"
        mock_scan.side_effect = RuntimeError("git error")
        mock_branch.return_value = {"valid": True, "issues": []}
        mock_enrich.return_value = None

        with patch("app.pr_review.detect_test_command", return_value=None):
            result = run_quality_pipeline("/project", "koan/")

        # Should recover gracefully
        assert result["scan"]["clean"] is True


# ---------------------------------------------------------------------------
# Integration: mission_runner
# ---------------------------------------------------------------------------

class TestMissionRunnerQualityIntegration:
    """Test quality pipeline integration in run_post_mission."""

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge")
    @patch("app.mission_runner.trigger_reflection")
    @patch("app.mission_runner._run_quality_pipeline")
    @patch("app.mission_runner.archive_pending")
    @patch("app.mission_runner._read_stdout_summary", return_value="")
    @patch("app.mission_runner._read_pending_content", return_value="test content")
    @patch("app.mission_runner.update_usage")
    def test_quality_pipeline_runs_on_success(
        self, mock_usage, mock_pending, mock_summary,
        mock_archive, mock_quality, mock_reflection,
        mock_merge, mock_outcome,
    ):
        from app.mission_runner import run_post_mission

        mock_usage.return_value = True
        mock_archive.return_value = True
        mock_quality.return_value = {"scan": {"clean": True}}
        mock_reflection.return_value = False
        mock_merge.return_value = None

        with patch("app.quota_handler.handle_quota_exhaustion", return_value=None):
            result = run_post_mission(
                instance_dir="/tmp/instance",
                project_name="test",
                project_path="/tmp/project",
                run_num=1,
                exit_code=0,
                stdout_file="/tmp/stdout",
                stderr_file="/tmp/stderr",
            )

        mock_quality.assert_called_once()
        assert "quality" in result

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge")
    @patch("app.mission_runner.trigger_reflection")
    @patch("app.mission_runner._run_quality_pipeline")
    @patch("app.mission_runner.archive_pending")
    @patch("app.mission_runner._read_stdout_summary", return_value="")
    @patch("app.mission_runner._read_pending_content", return_value="test content")
    @patch("app.mission_runner.update_usage")
    def test_quality_pipeline_skipped_on_failure(
        self, mock_usage, mock_pending, mock_summary,
        mock_archive, mock_quality, mock_reflection,
        mock_merge, mock_outcome,
    ):
        from app.mission_runner import run_post_mission

        mock_usage.return_value = True
        mock_archive.return_value = True

        with patch("app.quota_handler.handle_quota_exhaustion", return_value=None):
            result = run_post_mission(
                instance_dir="/tmp/instance",
                project_name="test",
                project_path="/tmp/project",
                run_num=1,
                exit_code=1,  # Failed
                stdout_file="/tmp/stdout",
                stderr_file="/tmp/stderr",
            )

        mock_quality.assert_not_called()

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.archive_pending")
    @patch("app.mission_runner._read_stdout_summary", return_value="")
    @patch("app.mission_runner._read_pending_content", return_value="test content")
    @patch("app.mission_runner.update_usage")
    def test_quality_pipeline_skipped_on_quota_exhaustion(
        self, mock_usage, mock_pending, mock_summary,
        mock_archive, mock_outcome,
    ):
        from app.mission_runner import run_post_mission

        mock_usage.return_value = True

        with patch("app.quota_handler.handle_quota_exhaustion", return_value=("2h", "resume msg")):
            result = run_post_mission(
                instance_dir="/tmp/instance",
                project_name="test",
                project_path="/tmp/project",
                run_num=1,
                exit_code=0,
                stdout_file="/tmp/stdout",
                stderr_file="/tmp/stderr",
            )

        assert result["quota_exhausted"] is True
        assert "quality" not in result


class TestGetQualityGateMode:
    """Test _get_quality_gate_mode."""

    @patch("app.projects_config.get_project_config")
    @patch("app.projects_config.load_projects_config")
    def test_returns_configured_mode(self, mock_load, mock_config):
        from app.mission_runner import _get_quality_gate_mode

        mock_load.return_value = {"projects": {}}
        mock_config.return_value = {"pr_quality": {"gate": "strict"}}

        result = _get_quality_gate_mode("/tmp/instance", "test")
        assert result == "strict"

    @patch("app.projects_config.get_project_config")
    @patch("app.projects_config.load_projects_config")
    def test_defaults_to_warn(self, mock_load, mock_config):
        from app.mission_runner import _get_quality_gate_mode

        mock_load.return_value = {"projects": {}}
        mock_config.return_value = {}

        result = _get_quality_gate_mode("/tmp/instance", "test")
        assert result == "warn"

    def test_defaults_on_exception(self):
        from app.mission_runner import _get_quality_gate_mode

        with patch("app.projects_config.load_projects_config", side_effect=Exception("fail")):
            result = _get_quality_gate_mode("/tmp/instance", "test")
        assert result == "warn"

    @patch("app.projects_config.get_project_config")
    @patch("app.projects_config.load_projects_config")
    def test_rejects_invalid_mode(self, mock_load, mock_config):
        from app.mission_runner import _get_quality_gate_mode

        mock_load.return_value = {"projects": {}}
        mock_config.return_value = {"pr_quality": {"gate": "invalid"}}

        result = _get_quality_gate_mode("/tmp/instance", "test")
        assert result == "warn"


class TestCheckAutoMergeWithQuality:
    """Test check_auto_merge with quality gate integration."""

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("app.git_sync.run_git", return_value="koan/feature")
    def test_merges_when_no_quality_report(self, mock_git, mock_prefix, mock_merge):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge("/tmp/instance", "test", "/tmp/project")
        assert result == "koan/feature"
        mock_merge.assert_called_once()

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.pr_quality.post_quality_comment")
    @patch("app.pr_quality.should_block_auto_merge", return_value=True)
    @patch("app.mission_runner._get_quality_gate_mode", return_value="strict")
    @patch("app.projects_config.get_project_auto_merge", return_value={"enabled": True})
    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("app.git_sync.run_git", return_value="koan/feature")
    def test_blocks_merge_on_strict_gate(self, mock_git, mock_prefix, mock_load, mock_am, mock_gate, mock_block, mock_comment, mock_merge):
        from app.mission_runner import check_auto_merge

        quality_report = {"scan": {"clean": False, "issues": [{"type": "secret"}]}}
        result = check_auto_merge("/tmp/instance", "test", "/tmp/project", quality_report=quality_report)
        assert result is None
        mock_merge.assert_not_called()
        mock_comment.assert_called_once()

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.pr_quality.post_quality_comment")
    @patch("app.pr_quality.should_block_auto_merge", return_value=False)
    @patch("app.mission_runner._get_quality_gate_mode", return_value="warn")
    @patch("app.projects_config.get_project_auto_merge", return_value={"enabled": True})
    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("app.git_sync.run_git", return_value="koan/feature")
    def test_merges_without_comment_on_warn(self, mock_git, mock_prefix, mock_load, mock_am, mock_gate, mock_block, mock_comment, mock_merge):
        """In warn mode, quality info is in the PR description — no separate comment."""
        from app.mission_runner import check_auto_merge

        quality_report = {"scan": {"clean": False, "issues": [{"type": "debug"}]}}
        result = check_auto_merge("/tmp/instance", "test", "/tmp/project", quality_report=quality_report)
        assert result == "koan/feature"
        mock_merge.assert_called_once()
        mock_comment.assert_not_called()

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.pr_quality.post_quality_comment")
    @patch("app.projects_config.get_project_auto_merge", return_value={"enabled": False})
    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("app.git_sync.run_git", return_value="koan/feature")
    def test_skips_quality_gate_when_auto_merge_disabled(self, mock_git, mock_prefix, mock_load, mock_am, mock_comment, mock_merge):
        """No quality gate comments when auto-merge is not configured."""
        from app.mission_runner import check_auto_merge

        quality_report = {"scan": {"clean": False, "issues": [{"type": "secret"}]}}
        result = check_auto_merge("/tmp/instance", "test", "/tmp/project", quality_report=quality_report)
        assert result == "koan/feature"
        mock_merge.assert_called_once()
        mock_comment.assert_not_called()
