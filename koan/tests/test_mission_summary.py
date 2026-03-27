"""Tests for mission_summary.py"""

from datetime import date
from pathlib import Path

import pytest

from app.mission_summary import (
    extract_error_lines,
    extract_latest_section,
    get_failure_context,
    get_mission_summary,
    summarize_section,
)


class TestExtractLatestSection:
    def test_single_section(self):
        text = "## Session 1\n\nDid some work."
        assert extract_latest_section(text) == "## Session 1\n\nDid some work."

    def test_multiple_sections(self):
        text = "## Session 1\n\nOld stuff.\n\n## Session 2\n\nNew stuff."
        result = extract_latest_section(text)
        assert "Session 2" in result
        assert "New stuff" in result
        assert "Old stuff" not in result

    def test_no_sections(self):
        text = "Just plain text."
        assert extract_latest_section(text) == "Just plain text."

    def test_empty(self):
        assert extract_latest_section("") == ""


class TestSummarizeSection:
    def test_basic(self):
        section = "## Fix auth bug\n\nFixed the token refresh logic.\nAdded retry on 401."
        result = summarize_section(section)
        assert "Fix auth bug" in result
        assert "token refresh" in result

    def test_truncates_long_text(self):
        section = "## Long\n\n" + "A" * 500
        result = summarize_section(section, max_chars=100)
        assert len(result) < 200  # heading + truncated body

    def test_stops_at_code_block(self):
        section = "## Impl\n\nAdded feature.\n```python\ncode here\n```"
        result = summarize_section(section)
        assert "code here" not in result
        assert "Added feature" in result

    def test_empty(self):
        assert summarize_section("") == ""

    def test_max_lines(self):
        section = "## Title\n\nLine 1\nLine 2\nLine 3\nLine 4\nLine 5\nLine 6"
        result = summarize_section(section)
        # Should cap at 4 body lines
        assert "Line 5" not in result


class TestGetMissionSummary:
    def test_nested_journal(self, tmp_path):
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal" / today
        journal_dir.mkdir(parents=True)
        (journal_dir / "myproject.md").write_text(
            "## Session 1\n\nEarly work.\n\n## Session 2\n\nLatest findings."
        )
        result = get_mission_summary(str(tmp_path), "myproject")
        assert "Session 2" in result
        assert "Latest findings" in result

    def test_flat_journal_fallback(self, tmp_path):
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir(parents=True)
        (journal_dir / f"{today}.md").write_text("## Work\n\nDid things.")
        result = get_mission_summary(str(tmp_path), "anyproject")
        assert "Did things" in result

    def test_no_journal(self, tmp_path):
        result = get_mission_summary(str(tmp_path), "noproject")
        assert result == ""

    def test_empty_journal(self, tmp_path):
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal" / today
        journal_dir.mkdir(parents=True)
        (journal_dir / "proj.md").write_text("")
        result = get_mission_summary(str(tmp_path), "proj")
        assert result == ""


class TestSummarizeSectionEdgeCases:
    """Cover line 51 (rsplit truncation) and line 64 (no heading)."""

    def test_truncation_with_rsplit(self):
        """Long body line triggers rsplit truncation at word boundary."""
        section = "## Title\n\n" + "word " * 100
        result = summarize_section(section, max_chars=50)
        assert result.endswith("...")

    def test_no_heading_returns_body_only(self):
        """Section without ## heading returns body lines only."""
        section = "Just body text\nMore text"
        result = summarize_section(section)
        assert "Just body text" in result

    def test_skips_separator_lines(self):
        """--- lines are skipped."""
        section = "## Title\n\n---\nActual content"
        result = summarize_section(section)
        assert "---" not in result
        assert "Actual content" in result


class TestMissionSummaryCLI:
    """Tests for __main__ CLI entry point (lines 84-95)."""

    def test_cli_prints_summary(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal" / today
        journal_dir.mkdir(parents=True)
        (journal_dir / "proj.md").write_text("## Session\n\nWork done.")

        monkeypatch.setattr("sys.argv", ["mission_summary.py", str(tmp_path), "proj"])
        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            run_module("app.mission_summary", run_name="__main__")
        assert "Work done" in f.getvalue()

    def test_cli_with_max_chars(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal" / today
        journal_dir.mkdir(parents=True)
        (journal_dir / "proj.md").write_text("## Session\n\n" + "x " * 500)

        monkeypatch.setattr("sys.argv", ["mission_summary.py", str(tmp_path), "proj", "50"])
        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            run_module("app.mission_summary", run_name="__main__")
        output = f.getvalue().strip()
        assert len(output) < 200

    def test_cli_no_args(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["mission_summary.py"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.mission_summary", run_name="__main__")
        assert exc_info.value.code == 1

    def test_cli_no_output_when_empty(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        monkeypatch.setattr("sys.argv", ["mission_summary.py", str(tmp_path), "proj"])
        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            run_module("app.mission_summary", run_name="__main__")
        assert f.getvalue().strip() == ""


class TestExtractErrorLines:
    """Tests for extract_error_lines()."""

    def test_finds_fail_keyword(self):
        text = "Running tests...\nFAILED test_foo.py::test_bar\nDone."
        result = extract_error_lines(text)
        assert len(result) == 1
        assert "FAILED" in result[0]

    def test_finds_error_colon(self):
        text = "Building...\nError: module not found\nContinuing."
        result = extract_error_lines(text)
        assert len(result) == 1
        assert "module not found" in result[0]

    def test_finds_traceback(self):
        text = "Traceback (most recent call last):\n  File foo.py\nKeyError: 'bar'"
        result = extract_error_lines(text)
        assert len(result) == 2
        assert "Traceback" in result[0]

    def test_finds_exit_code(self):
        text = "Process finished with exit code: 1"
        result = extract_error_lines(text)
        assert len(result) == 1
        assert "exit code" in result[0]

    def test_finds_returncode(self):
        text = "returncode: 127"
        result = extract_error_lines(text)
        assert len(result) == 1

    def test_finds_rebase_conflict(self):
        text = "Rebase conflict detected in file.py"
        result = extract_error_lines(text)
        assert len(result) == 1
        assert "Rebase conflict" in result[0]

    def test_finds_permission_denied(self):
        text = "Permission denied: /root/secrets"
        result = extract_error_lines(text)
        assert len(result) == 1

    def test_finds_git_fatal(self):
        text = "fatal: not a git repository"
        result = extract_error_lines(text)
        assert len(result) == 1
        assert "fatal:" in result[0]

    def test_skips_noise_max_turns(self):
        text = "Error: max turns reached"
        result = extract_error_lines(text)
        assert result == []

    def test_skips_noise_error_handling(self):
        text = "Improved error handling in module"
        result = extract_error_lines(text)
        assert result == []

    def test_skips_noise_test_error(self):
        text = "Added test for error case"
        result = extract_error_lines(text)
        assert result == []

    def test_skips_noise_fix_error(self):
        text = "fix error parsing in CLI"
        result = extract_error_lines(text)
        assert result == []

    def test_skips_noise_error_context(self):
        text = "Added error_context to notifications"
        result = extract_error_lines(text)
        assert result == []

    def test_respects_max_lines(self):
        text = "\n".join(f"Error: problem {i}" for i in range(10))
        result = extract_error_lines(text, max_lines=3)
        assert len(result) == 3

    def test_empty_text(self):
        assert extract_error_lines("") == []

    def test_no_errors(self):
        text = "All good.\nTests passed.\nBuild succeeded."
        assert extract_error_lines(text) == []

    def test_skips_blank_lines(self):
        text = "\n\n  \n\nError: real error\n\n"
        result = extract_error_lines(text)
        assert len(result) == 1

    def test_multiple_patterns(self):
        text = "FAIL test_a\nError: boom\nfatal: bad ref\n"
        result = extract_error_lines(text)
        assert len(result) == 3

    def test_case_insensitive_fail(self):
        text = "failure in test_xyz"
        result = extract_error_lines(text)
        assert len(result) == 1


class TestGetFailureContext:
    """Tests for get_failure_context()."""

    def _write_journal(self, tmp_path, project, content):
        today = date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal" / today
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / f"{project}.md").write_text(content)

    def test_extracts_errors_from_journal(self, tmp_path):
        self._write_journal(tmp_path, "proj", (
            "## Session 1\n\nGood stuff.\n\n"
            "## Session 2\n\nRunning build...\n"
            "FAILED to compile module\n"
            "fatal: unable to resolve ref\n"
        ))
        result = get_failure_context(str(tmp_path), "proj")
        assert "FAILED" in result
        assert "fatal:" in result

    def test_returns_empty_for_no_errors(self, tmp_path):
        self._write_journal(tmp_path, "proj",
            "## Session\n\nAll tests passed. Everything is fine."
        )
        result = get_failure_context(str(tmp_path), "proj")
        assert result == ""

    def test_returns_empty_for_missing_journal(self, tmp_path):
        result = get_failure_context(str(tmp_path), "noproject")
        assert result == ""

    def test_returns_empty_for_empty_journal(self, tmp_path):
        self._write_journal(tmp_path, "proj", "")
        result = get_failure_context(str(tmp_path), "proj")
        assert result == ""

    def test_truncates_long_output(self, tmp_path):
        errors = "\n".join(f"Error: very long error message number {i} with details" for i in range(20))
        self._write_journal(tmp_path, "proj", f"## Session\n\n{errors}")
        result = get_failure_context(str(tmp_path), "proj", max_chars=100)
        assert len(result) <= 100

    def test_truncation_fallback_single_long_line(self, tmp_path):
        """When rsplit('\\n') leaves empty string, falls back to first line truncation."""
        long_error = "Error: " + "x" * 500
        self._write_journal(tmp_path, "proj", f"## Session\n\n{long_error}")
        result = get_failure_context(str(tmp_path), "proj", max_chars=50)
        assert len(result) == 50

    def test_uses_latest_section(self, tmp_path):
        self._write_journal(tmp_path, "proj", (
            "## Session 1\n\nfatal: early error\n\n"
            "## Session 2\n\nAll clean now."
        ))
        result = get_failure_context(str(tmp_path), "proj")
        # Latest section has no errors
        assert result == ""

    def test_filters_noise_in_journal(self, tmp_path):
        self._write_journal(tmp_path, "proj", (
            "## Session\n\n"
            "Improved error handling across the codebase.\n"
            "Added test for error edge case.\n"
        ))
        result = get_failure_context(str(tmp_path), "proj")
        assert result == ""
