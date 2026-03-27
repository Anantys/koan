"""Tests for recurring_scheduler.py — CLI entry point for recurring missions."""

from unittest.mock import patch
from io import StringIO

import pytest

from app.recurring_scheduler import main


# ---------------------------------------------------------------------------
# CLI argument handling
# ---------------------------------------------------------------------------


class TestCLIArgs:
    def test_missing_arg_exits_1(self):
        """Missing instance_dir argument should exit with code 1."""
        with patch("sys.argv", ["recurring_scheduler.py"]), \
             patch("sys.stderr", new_callable=StringIO) as mock_stderr, \
             pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "Usage:" in mock_stderr.getvalue()

    def test_missing_recurring_json_exits_0(self, tmp_path):
        """If recurring.json doesn't exist, exit silently with code 0."""
        with patch("sys.argv", ["recurring_scheduler.py", str(tmp_path)]), \
             pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Successful injection
# ---------------------------------------------------------------------------


class TestSuccessfulInjection:
    def test_prints_injected_missions(self, tmp_path):
        """Injected missions should be printed with [recurring] prefix."""
        recurring_file = tmp_path / "recurring.json"
        recurring_file.write_text("[]")  # Empty but valid JSON

        with patch("sys.argv", ["recurring_scheduler.py", str(tmp_path)]), \
             patch("app.recurring.check_and_inject", return_value=["Mission 1", "Mission 2"]) as mock_inject, \
             patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            main()

        output = mock_stdout.getvalue()
        assert "[recurring] Injected: Mission 1" in output
        assert "[recurring] Injected: Mission 2" in output
        mock_inject.assert_called_once()

    def test_no_output_when_nothing_injected(self, tmp_path):
        """No output when check_and_inject returns empty list."""
        recurring_file = tmp_path / "recurring.json"
        recurring_file.write_text("[]")

        with patch("sys.argv", ["recurring_scheduler.py", str(tmp_path)]), \
             patch("app.recurring.check_and_inject", return_value=[]), \
             patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            main()

        assert mock_stdout.getvalue() == ""


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_exception_logged_to_stderr(self, tmp_path):
        """Exceptions should be logged to stderr, not crash."""
        recurring_file = tmp_path / "recurring.json"
        recurring_file.write_text("[]")

        with patch("sys.argv", ["recurring_scheduler.py", str(tmp_path)]), \
             patch("app.recurring.check_and_inject", side_effect=ValueError("parse error")), \
             patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            # Should not raise
            main()

        output = mock_stderr.getvalue()
        assert "[recurring] Error:" in output
        assert "parse error" in output

    def test_does_not_exit_on_error(self, tmp_path):
        """Errors are non-fatal — function should return, not exit."""
        recurring_file = tmp_path / "recurring.json"
        recurring_file.write_text("[]")

        with patch("sys.argv", ["recurring_scheduler.py", str(tmp_path)]), \
             patch("app.recurring.check_and_inject", side_effect=Exception("boom")), \
             patch("sys.stderr", new_callable=StringIO):
            # Should complete without raising or exiting
            main()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_recurring_json_path(self, tmp_path):
        """Should look for recurring.json in instance_dir."""
        recurring_file = tmp_path / "recurring.json"
        recurring_file.write_text("[]")

        with patch("sys.argv", ["recurring_scheduler.py", str(tmp_path)]), \
             patch("app.recurring.check_and_inject") as mock_inject:
            mock_inject.return_value = []
            main()

        # Check that check_and_inject was called with correct paths
        args = mock_inject.call_args[0]
        assert args[0] == recurring_file
        assert args[1] == tmp_path / "missions.md"
