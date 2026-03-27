"""Tests for format_outbox module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.format_outbox import (
    load_soul,
    load_human_prefs,
    load_memory_context,
    format_message,
    fallback_format,
)
from app.response_cache import _format_cache


@pytest.fixture(autouse=True)
def _clear_format_cache():
    """Reset format cache between tests."""
    _format_cache.clear()
    yield
    _format_cache.clear()


class TestLoadSoul:
    def test_returns_content_when_file_exists(self, instance_dir):
        result = load_soul(instance_dir)
        assert result == "# Test Soul"

    def test_returns_empty_when_file_missing(self, tmp_path):
        result = load_soul(tmp_path)
        assert result == ""


class TestLoadHumanPrefs:
    def test_returns_content_when_file_exists(self, instance_dir):
        prefs_dir = instance_dir / "memory" / "global"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        (prefs_dir / "human-preferences.md").write_text("Prefers French")
        result = load_human_prefs(instance_dir)
        assert result == "Prefers French"

    def test_returns_empty_when_file_missing(self, instance_dir):
        result = load_human_prefs(instance_dir)
        assert result == ""


class TestFallbackFormat:
    def test_removes_markdown_headers(self):
        assert fallback_format("## Title\nContent") == "Title\nContent"

    def test_removes_code_fences(self):
        assert fallback_format("```python\ncode\n```") == "python\ncode"

    def test_removes_bold_and_underline(self):
        assert fallback_format("**bold** and __underline__") == "bold and underline"

    def test_removes_strikethrough(self):
        assert fallback_format("~~deleted~~") == "deleted"

    def test_removes_list_markers(self):
        result = fallback_format("- item one\n* item two\n> quoted")
        assert "- " not in result
        assert "* " not in result
        assert "> " not in result

    def test_truncates_long_content(self):
        long_text = "a" * 2500
        result = fallback_format(long_text)
        assert len(result) == 2000  # 1997 + "..."
        assert result.endswith("...")

    def test_strips_whitespace(self):
        assert fallback_format("  hello  ") == "hello"

    def test_short_content_not_truncated(self):
        assert fallback_format("Short message") == "Short message"


class TestFormatForTelegram:
    @patch("app.cli_exec.run_cli")
    def test_returns_claude_output_on_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Voici le résumé formaté.\n", stderr=""
        )
        result = format_message("raw content", "soul", "prefs")
        assert result == "Voici le résumé formaté."
        mock_run.assert_called_once()

    @patch("app.cli_exec.run_cli")
    def test_strips_markdown_from_claude_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="**bold** and ```code``` and __under__ and ~~strike~~", stderr=""
        )
        result = format_message("raw", "soul", "")
        assert "**" not in result
        assert "```" not in result
        assert "__" not in result
        assert "~~" not in result

    @patch("app.cli_exec.run_cli")
    def test_fallback_on_nonzero_returncode(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error"
        )
        result = format_message("## Raw content", "soul", "")
        # Should use fallback (removes #)
        assert "#" not in result
        assert "Raw content" in result

    @patch("app.cli_exec.run_cli")
    def test_fallback_on_empty_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  \n  ", stderr=""
        )
        result = format_message("Some raw content", "soul", "")
        assert "Some raw content" in result

    @patch("app.cli_exec.run_cli")
    def test_fallback_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        result = format_message("Raw", "soul", "")
        assert result == "Raw"

    @patch("app.cli_exec.run_cli")
    def test_fallback_on_exception(self, mock_run):
        mock_run.side_effect = FileNotFoundError("claude not found")
        result = format_message("Raw", "soul", "")
        assert result == "Raw"

    @patch("app.cli_exec.run_cli")
    def test_prompt_includes_soul_and_prefs(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        format_message("content", "my-soul", "my-prefs")
        call_args = mock_run.call_args[0][0]
        prompt = call_args[2]  # ["claude", "-p", prompt]
        assert "my-soul" in prompt
        assert "my-prefs" in prompt

    @patch("app.cli_exec.run_cli")
    def test_prompt_omits_prefs_when_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        format_message("content", "soul", "")
        call_args = mock_run.call_args[0][0]
        prompt = call_args[2]
        assert "Human preferences:" not in prompt

    @patch("app.cli_exec.run_cli")
    def test_prompt_includes_memory_context(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        format_message("content", "soul", "prefs", memory_context="Session 61: tests")
        call_args = mock_run.call_args[0][0]
        prompt = call_args[2]
        assert "Session 61: tests" in prompt
        assert "Recent memory context" in prompt

    @patch("app.cli_exec.run_cli")
    def test_prompt_omits_memory_when_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        format_message("content", "soul", "prefs", memory_context="")
        call_args = mock_run.call_args[0][0]
        prompt = call_args[2]
        assert "Recent memory context" not in prompt


class TestLoadMemoryContext:
    def test_returns_empty_when_no_files(self, tmp_path):
        result = load_memory_context(tmp_path)
        assert result == ""

    def test_loads_summary_last_lines(self, instance_dir):
        summary_dir = instance_dir / "memory"
        summary_dir.mkdir(parents=True, exist_ok=True)
        (summary_dir / "summary.md").write_text(
            "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\nLine 6\nLine 7"
        )
        result = load_memory_context(instance_dir)
        assert "Line 7" in result
        assert "Line 3" in result
        assert "Line 2" not in result

    def test_loads_project_learnings(self, instance_dir):
        learnings_dir = instance_dir / "memory" / "projects" / "koan"
        learnings_dir.mkdir(parents=True, exist_ok=True)
        (learnings_dir / "learnings.md").write_text("## Architecture\n- KOAN_ROOT is env var")
        result = load_memory_context(instance_dir, "koan")
        assert "KOAN_ROOT" in result

    def test_no_learnings_without_project_name(self, instance_dir):
        learnings_dir = instance_dir / "memory" / "projects" / "koan"
        learnings_dir.mkdir(parents=True, exist_ok=True)
        (learnings_dir / "learnings.md").write_text("Secret learning")
        result = load_memory_context(instance_dir, "")
        assert "Secret learning" not in result

    def test_combines_summary_and_learnings(self, instance_dir):
        summary_dir = instance_dir / "memory"
        summary_dir.mkdir(parents=True, exist_ok=True)
        (summary_dir / "summary.md").write_text("Session 61: tests")
        learnings_dir = summary_dir / "projects" / "koan"
        learnings_dir.mkdir(parents=True, exist_ok=True)
        (learnings_dir / "learnings.md").write_text("- Important fact")
        result = load_memory_context(instance_dir, "koan")
        assert "Session 61" in result
        assert "Important fact" in result

    def test_loads_personality_evolution(self, instance_dir):
        """personality-evolution.md should be included in memory context."""
        personality_dir = instance_dir / "memory" / "global"
        personality_dir.mkdir(parents=True, exist_ok=True)
        (personality_dir / "personality-evolution.md").write_text(
            "# Personality Evolution\n\n- I like DRY refactorings\n- I prefer French for outbox\n"
        )
        result = load_memory_context(instance_dir)
        assert "DRY refactorings" in result
        assert "Acquired personality traits" in result

    def test_personality_evolution_skips_headers(self, instance_dir):
        """Headers in personality-evolution.md should be filtered out."""
        personality_dir = instance_dir / "memory" / "global"
        personality_dir.mkdir(parents=True, exist_ok=True)
        (personality_dir / "personality-evolution.md").write_text(
            "# Personality Evolution\n## Observations\n- I enjoy audits\n"
        )
        result = load_memory_context(instance_dir)
        assert "I enjoy audits" in result
        assert "# Personality" not in result


class TestGetTimeHint:
    """Tests for _get_time_hint — tone adaptation by time of day."""

    def _make_fake_now(self, hour):
        from datetime import datetime as real_dt
        return real_dt(2026, 2, 2, hour, 0)

    def test_morning(self):
        from app.format_outbox import _get_time_hint
        fake = self._make_fake_now(9)
        with patch("app.format_outbox.datetime") as mock_dt:
            mock_dt.now.return_value = fake
            result = _get_time_hint()
        assert "morning" in result.lower()

    def test_evening(self):
        from app.format_outbox import _get_time_hint
        fake = self._make_fake_now(20)
        with patch("app.format_outbox.datetime") as mock_dt:
            mock_dt.now.return_value = fake
            result = _get_time_hint()
        assert "evening" in result.lower()

    def test_late_night(self):
        from app.format_outbox import _get_time_hint
        fake = self._make_fake_now(23)
        with patch("app.format_outbox.datetime") as mock_dt:
            mock_dt.now.return_value = fake
            result = _get_time_hint()
        assert "late night" in result.lower()


class TestFormatOutboxCLI:
    """Tests for main() CLI entry point (lines 177-210)."""

    def test_cli_formats_stdin(self, instance_dir, monkeypatch):
        import io, contextlib
        monkeypatch.setattr("sys.argv", ["format_outbox.py", str(instance_dir)])
        monkeypatch.setattr("sys.stdin", io.StringIO("Raw message to format"))
        f = io.StringIO()
        with patch("app.format_outbox.subprocess.run") as mock_run, \
             contextlib.redirect_stdout(f):
            mock_run.return_value = MagicMock(returncode=0, stdout="Formatted!", stderr="")
            from app.format_outbox import main
            main()
        assert "Formatted!" in f.getvalue()

    def test_cli_with_project_name(self, instance_dir, monkeypatch):
        import io, contextlib
        monkeypatch.setattr("sys.argv", ["format_outbox.py", str(instance_dir), "koan"])
        monkeypatch.setattr("sys.stdin", io.StringIO("Raw"))
        f = io.StringIO()
        with patch("app.format_outbox.subprocess.run") as mock_run, \
             contextlib.redirect_stdout(f):
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            from app.format_outbox import main
            main()
        assert "OK" in f.getvalue()

    def test_cli_no_args(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["format_outbox.py"])
        from app.format_outbox import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_missing_instance_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["format_outbox.py", str(tmp_path / "nonexistent")])
        from app.format_outbox import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_empty_stdin(self, instance_dir, monkeypatch):
        import io
        monkeypatch.setattr("sys.argv", ["format_outbox.py", str(instance_dir)])
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        from app.format_outbox import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


class TestFormatMessageCaching:
    """Tests for format_message() response caching."""

    @patch("app.cli_exec.run_cli")
    def test_cache_hit_skips_cli_call(self, mock_run):
        """Second call with identical inputs returns cached result without CLI."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Formatted result", stderr=""
        )
        # First call — cache miss, invokes CLI
        result1 = format_message("raw", "soul", "prefs", "memory")
        assert mock_run.call_count == 1

        # Second call — cache hit, no CLI invocation
        result2 = format_message("raw", "soul", "prefs", "memory")
        assert mock_run.call_count == 1  # still 1
        assert result1 == result2

    @patch("app.cli_exec.run_cli")
    def test_different_content_produces_cache_miss(self, mock_run):
        """Different raw_content should not hit the cache."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Formatted", stderr=""
        )
        format_message("content A", "soul", "prefs")
        format_message("content B", "soul", "prefs")
        assert mock_run.call_count == 2

    @patch("app.cli_exec.run_cli")
    def test_fallback_result_not_cached(self, mock_run):
        """Failed CLI calls should not cache the fallback result."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error"
        )
        result1 = format_message("## Raw", "soul", "")
        # Second call should still invoke CLI (fallback not cached)
        result2 = format_message("## Raw", "soul", "")
        assert mock_run.call_count == 2

    @patch("app.cli_exec.run_cli")
    def test_timeout_not_cached(self, mock_run):
        """Timeout fallback should not be cached."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        format_message("Raw", "soul", "")
        format_message("Raw", "soul", "")
        assert mock_run.call_count == 2

    @patch("app.cli_exec.run_cli")
    def test_cache_stats_updated(self, mock_run):
        """Cache stats should reflect hits and misses."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="OK", stderr=""
        )
        format_message("raw", "soul", "prefs")
        format_message("raw", "soul", "prefs")  # hit
        stats = _format_cache.stats()
        assert stats["hits"] >= 1
        assert stats["misses"] >= 1

    @patch("app.cli_exec.run_cli")
    def test_different_time_hint_produces_miss(self, mock_run):
        """Different time-of-day hint should produce a different cache key."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="OK", stderr=""
        )
        from datetime import datetime as real_dt

        morning = real_dt(2026, 3, 8, 9, 0)
        evening = real_dt(2026, 3, 8, 20, 0)

        with patch("app.format_outbox.datetime") as mock_dt:
            mock_dt.now.return_value = morning
            format_message("raw", "soul", "prefs")

        with patch("app.format_outbox.datetime") as mock_dt:
            mock_dt.now.return_value = evening
            format_message("raw", "soul", "prefs")

        assert mock_run.call_count == 2
