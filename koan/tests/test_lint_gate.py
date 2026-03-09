"""Tests for lint_gate module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.lint_gate import (
    LintResult,
    _expand_command,
    _get_changed_files,
    _write_journal_entry,
    get_project_lint_config,
    run_lint_gate,
)


# ---------------------------------------------------------------------------
# get_project_lint_config
# ---------------------------------------------------------------------------

class TestGetProjectLintConfig:
    def test_returns_defaults_when_no_lint_section(self):
        config = {"defaults": {}, "projects": {"myproj": {"path": "/tmp"}}}
        result = get_project_lint_config(config, "myproj")
        assert result == {
            "enabled": False,
            "command": "",
            "timeout": 60,
            "blocking": True,
        }

    def test_reads_lint_config_from_project(self):
        config = {
            "defaults": {},
            "projects": {
                "myproj": {
                    "path": "/tmp",
                    "lint": {
                        "enabled": True,
                        "command": "ruff check {files}",
                        "timeout": 30,
                        "blocking": False,
                    },
                },
            },
        }
        result = get_project_lint_config(config, "myproj")
        assert result["enabled"] is True
        assert result["command"] == "ruff check {files}"
        assert result["timeout"] == 30
        assert result["blocking"] is False

    def test_falls_back_to_defaults_section(self):
        config = {
            "defaults": {
                "lint": {"enabled": True, "command": "make lint", "timeout": 90},
            },
            "projects": {"myproj": {"path": "/tmp"}},
        }
        result = get_project_lint_config(config, "myproj")
        assert result["enabled"] is True
        assert result["command"] == "make lint"
        assert result["timeout"] == 90
        assert result["blocking"] is True  # default

    def test_project_overrides_defaults(self):
        config = {
            "defaults": {
                "lint": {"enabled": True, "command": "make lint"},
            },
            "projects": {
                "myproj": {
                    "path": "/tmp",
                    "lint": {"command": "ruff check"},
                },
            },
        }
        result = get_project_lint_config(config, "myproj")
        # project overrides command, inherits enabled from defaults
        assert result["enabled"] is True
        assert result["command"] == "ruff check"

    def test_handles_none_lint_section(self):
        config = {
            "defaults": {},
            "projects": {"myproj": {"path": "/tmp", "lint": None}},
        }
        result = get_project_lint_config(config, "myproj")
        assert result["enabled"] is False


# ---------------------------------------------------------------------------
# _expand_command
# ---------------------------------------------------------------------------

class TestExpandCommand:
    def test_no_placeholder_returns_as_is(self):
        assert _expand_command("make lint", ["a.py"]) == "make lint"

    def test_expands_files_placeholder(self):
        result = _expand_command("ruff check {files}", ["a.py", "b.py"])
        assert "a.py" in result
        assert "b.py" in result

    def test_caps_at_100_files(self):
        files = [f"file_{i}.py" for i in range(200)]
        result = _expand_command("ruff check {files}", files)
        # Should only include first 100
        assert "file_99.py" in result
        assert "file_100.py" not in result

    def test_quotes_files_with_spaces(self):
        result = _expand_command("ruff check {files}", ["my file.py"])
        assert "my\\ file.py" in result or "'my file.py'" in result


# ---------------------------------------------------------------------------
# _get_changed_files
# ---------------------------------------------------------------------------

class TestGetChangedFiles:
    @patch("app.lint_gate.subprocess.run")
    def test_returns_changed_files(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="a.py\nb.py\n"
        )
        result = _get_changed_files("/tmp/proj", "main")
        assert result == ["a.py", "b.py"]
        mock_run.assert_called_once()

    @patch("app.lint_gate.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _get_changed_files("/tmp/proj", "main") == []

    @patch("app.lint_gate.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("git", 30)
        assert _get_changed_files("/tmp/proj", "main") == []

    @patch("app.lint_gate.subprocess.run")
    def test_filters_empty_lines(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="a.py\n\n  \nb.py\n"
        )
        result = _get_changed_files("/tmp/proj", "main")
        assert result == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# run_lint_gate
# ---------------------------------------------------------------------------

class TestRunLintGate:
    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.load_projects_config")
    def test_returns_none_when_not_enabled(self, mock_config):
        mock_config.return_value = {
            "defaults": {},
            "projects": {"proj": {"path": "/tmp"}},
        }
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is None

    @patch.dict("os.environ", {"KOAN_ROOT": ""})
    def test_returns_none_when_no_koan_root(self):
        assert run_lint_gate("/tmp/proj", "proj") is None

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.load_projects_config")
    def test_returns_none_when_no_config(self, mock_config):
        mock_config.return_value = None
        assert run_lint_gate("/tmp/proj", "proj") is None

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_returns_none_when_no_changed_files(
        self, mock_config, mock_base, mock_run
    ):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {"enabled": True, "command": "ruff check"},
                },
            },
        }
        # git diff returns no files
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is None

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_lint_passes(self, mock_config, mock_base, mock_run):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {"enabled": True, "command": "ruff check"},
                },
            },
        }
        # First call: git diff (changed files)
        # Second call: lint command
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py\n"),
            MagicMock(returncode=0, stdout="All checks passed\n", stderr=""),
        ]
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is not None
        assert result.passed is True
        assert "ruff check" in result.command

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_lint_fails(self, mock_config, mock_base, mock_run):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {"enabled": True, "command": "ruff check"},
                },
            },
        }
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py\n"),
            MagicMock(returncode=1, stdout="", stderr="E501: line too long"),
        ]
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is not None
        assert result.passed is False
        assert "E501" in result.output

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_lint_timeout(self, mock_config, mock_base, mock_run):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {
                        "enabled": True,
                        "command": "ruff check",
                        "timeout": 10,
                    },
                },
            },
        }
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py\n"),
            subprocess.TimeoutExpired("ruff", 10),
        ]
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is not None
        assert result.passed is False
        assert "timed out" in result.output

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_lint_command_not_found(self, mock_config, mock_base, mock_run):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {"enabled": True, "command": "nonexistent-tool"},
                },
            },
        }
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py\n"),
            FileNotFoundError("nonexistent-tool"),
        ]
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is None  # Treated as warning, not failure

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate._write_journal_entry")
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_writes_journal_when_instance_dir_provided(
        self, mock_config, mock_base, mock_run, mock_journal
    ):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {"enabled": True, "command": "make lint"},
                },
            },
        }
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py\n"),
            MagicMock(returncode=0, stdout="ok\n", stderr=""),
        ]
        run_lint_gate("/tmp/proj", "proj", instance_dir="/tmp/instance")
        mock_journal.assert_called_once()

    @patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"})
    @patch("app.lint_gate.subprocess.run")
    @patch("app.lint_gate.resolve_base_branch", return_value="main")
    @patch("app.lint_gate.load_projects_config")
    def test_expands_files_in_command(self, mock_config, mock_base, mock_run):
        mock_config.return_value = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/tmp",
                    "lint": {"enabled": True, "command": "ruff check {files}"},
                },
            },
        }
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py\nb.py\n"),
            MagicMock(returncode=0, stdout="ok\n", stderr=""),
        ]
        result = run_lint_gate("/tmp/proj", "proj")
        assert result is not None
        # The lint command should have expanded {files}
        assert "a.py" in result.command
        assert "b.py" in result.command


# ---------------------------------------------------------------------------
# _write_journal_entry
# ---------------------------------------------------------------------------

class TestWriteJournalEntry:
    @patch("app.journal.append_to_journal")
    def test_writes_passed_entry(self, mock_journal):
        result = LintResult(passed=True, output="ok", command="make lint")
        _write_journal_entry("/tmp/instance", "proj", result)
        mock_journal.assert_called_once()
        entry = mock_journal.call_args[0][2]
        assert "PASSED" in entry
        assert "make lint" in entry

    @patch("app.journal.append_to_journal")
    def test_writes_failed_entry_with_output(self, mock_journal):
        result = LintResult(
            passed=False, output="error on line 5", command="ruff check"
        )
        _write_journal_entry("/tmp/instance", "proj", result)
        entry = mock_journal.call_args[0][2]
        assert "FAILED" in entry
        assert "error on line 5" in entry

    @patch("app.journal.append_to_journal")
    def test_handles_journal_failure_gracefully(self, mock_journal):
        mock_journal.side_effect = OSError("disk full")
        result = LintResult(passed=True, output="ok", command="make lint")
        # Should not raise
        _write_journal_entry("/tmp/instance", "proj", result)


# ---------------------------------------------------------------------------
# Integration with mission_runner
# ---------------------------------------------------------------------------

class TestMissionRunnerLintIntegration:
    """Test lint gate integration in run_post_mission."""

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge")
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner._is_lint_blocking", return_value=True)
    @patch("app.mission_runner._run_lint_gate")
    @patch("app.mission_runner._run_quality_pipeline", return_value={})
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_lint_failure_blocks_auto_merge(
        self,
        mock_usage,
        mock_quota,
        mock_archive,
        mock_quality,
        mock_lint,
        mock_is_blocking,
        mock_reflect,
        mock_merge,
        mock_outcome,
        tmp_path,
    ):
        from app.mission_runner import run_post_mission

        mock_lint.return_value = LintResult(
            passed=False, output="lint error", command="ruff check"
        )

        result = run_post_mission(
            instance_dir=str(tmp_path),
            project_name="proj",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file=str(tmp_path / "stdout"),
            stderr_file=str(tmp_path / "stderr"),
        )

        assert result["lint_passed"] is False
        # check_auto_merge should be called with lint_blocked=True
        mock_merge.assert_called_once()
        _, kwargs = mock_merge.call_args
        assert kwargs.get("lint_blocked") is True

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge")
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner._is_lint_blocking", return_value=False)
    @patch("app.mission_runner._run_lint_gate")
    @patch("app.mission_runner._run_quality_pipeline", return_value={})
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_lint_passes_allows_auto_merge(
        self,
        mock_usage,
        mock_quota,
        mock_archive,
        mock_quality,
        mock_lint,
        mock_is_blocking,
        mock_reflect,
        mock_merge,
        mock_outcome,
        tmp_path,
    ):
        from app.mission_runner import run_post_mission

        mock_lint.return_value = LintResult(
            passed=True, output="ok", command="ruff check"
        )

        result = run_post_mission(
            instance_dir=str(tmp_path),
            project_name="proj",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file=str(tmp_path / "stdout"),
            stderr_file=str(tmp_path / "stderr"),
        )

        assert result["lint_passed"] is True
        mock_merge.assert_called_once()
        _, kwargs = mock_merge.call_args
        assert kwargs.get("lint_blocked") is False

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge")
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner._run_lint_gate")
    @patch("app.mission_runner._run_quality_pipeline", return_value={})
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_no_lint_config_skips_gate(
        self,
        mock_usage,
        mock_quota,
        mock_archive,
        mock_quality,
        mock_lint,
        mock_reflect,
        mock_merge,
        mock_outcome,
        tmp_path,
    ):
        from app.mission_runner import run_post_mission

        mock_lint.return_value = None  # Not configured

        result = run_post_mission(
            instance_dir=str(tmp_path),
            project_name="proj",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file=str(tmp_path / "stdout"),
            stderr_file=str(tmp_path / "stderr"),
        )

        assert "lint_passed" not in result
        mock_merge.assert_called_once()
        _, kwargs = mock_merge.call_args
        assert kwargs.get("lint_blocked") is False
