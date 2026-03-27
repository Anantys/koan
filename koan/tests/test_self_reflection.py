"""Tests for self_reflection module."""

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.self_reflection import (
    should_reflect,
    build_reflection_prompt,
    run_reflection,
    save_reflection,
    notify_outbox,
)


@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory for testing."""
    memory_dir = tmp_path / "memory" / "global"
    memory_dir.mkdir(parents=True)
    (tmp_path / "soul.md").write_text("You are Kōan.")
    return tmp_path


class TestShouldReflect:
    def test_no_summary_file(self, instance_dir):
        assert should_reflect(instance_dir) is False

    def test_session_divisible_by_10(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Session 100 (project: koan) : test\n")
        assert should_reflect(instance_dir) is True

    def test_session_not_divisible_by_10(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Session 103 (project: koan) : test\n")
        assert should_reflect(instance_dir) is False

    def test_multiple_sessions_uses_max(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text(
            "Session 98 (project: koan) : a\n"
            "Session 99 (project: koan) : b\n"
            "Session 100 (project: koan) : c\n"
        )
        assert should_reflect(instance_dir) is True

    def test_custom_interval(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Session 15 (project: koan) : test\n")
        assert should_reflect(instance_dir, interval=5) is True
        assert should_reflect(instance_dir, interval=10) is False


class TestBuildReflectionPrompt:
    def test_includes_soul(self, instance_dir):
        prompt = build_reflection_prompt(instance_dir)
        assert "Kōan" in prompt

    def test_includes_summary(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Session 42 (project: koan) : audited codebase\n")
        prompt = build_reflection_prompt(instance_dir)
        assert "audited codebase" in prompt

    def test_includes_personality(self, instance_dir):
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("# Personality\n\n- I like tests\n")
        prompt = build_reflection_prompt(instance_dir)
        assert "I like tests" in prompt

    def test_includes_emotional_memory(self, instance_dir):
        emotional = instance_dir / "memory" / "global" / "emotional-memory.md"
        emotional.write_text("# Emotional\n\n- 'tu déchires mec'\n")
        prompt = build_reflection_prompt(instance_dir)
        assert "tu déchires" in prompt

    def test_has_reflection_instructions(self, instance_dir):
        prompt = build_reflection_prompt(instance_dir)
        assert "Patterns" in prompt
        assert "Growth" in prompt
        assert "Relationship" in prompt


class TestSaveReflection:
    def test_creates_new_file(self, instance_dir):
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("# Personality Evolution\n")
        save_reflection(instance_dir, "- I notice I love audits")
        content = personality.read_text()
        assert "Reflection" in content
        assert "I notice I love audits" in content

    def test_appends_to_existing(self, instance_dir):
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("# Personality\n\n## Reflection — 2026-01-01\n\n- old\n")
        save_reflection(instance_dir, "- new observation")
        content = personality.read_text()
        assert "old" in content
        assert "new observation" in content

    def test_includes_date(self, instance_dir):
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("# Personality\n")
        save_reflection(instance_dir, "- test")
        content = personality.read_text()
        assert re.search(r"## Reflection — \d{4}-\d{2}-\d{2}", content)

    def test_handles_missing_personality_file(self, instance_dir):
        """save_reflection works even when personality-evolution.md doesn't exist."""
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.unlink(missing_ok=True)
        save_reflection(instance_dir, "- first observation")
        content = personality.read_text()
        assert "first observation" in content

    def test_handles_unreadable_personality_file(self, instance_dir, monkeypatch):
        """When personality file read raises OSError, treats as empty."""
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("existing content")
        original_read = Path.read_text

        def bad_read(self, *args, **kwargs):
            if self.name == "personality-evolution.md":
                raise OSError("Permission denied")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", bad_read)
        # Should not crash
        save_reflection(instance_dir, "- new observation")


class TestNotifyOutbox:
    def test_writes_to_outbox(self, instance_dir):
        notify_outbox(instance_dir, "- I notice patterns")
        outbox = instance_dir / "outbox.md"
        assert outbox.exists()
        content = outbox.read_text()
        assert "I notice patterns" in content
        assert "🪷" in content
        assert "personality-evolution.md" in content

    def test_appends_to_existing_outbox(self, instance_dir):
        outbox = instance_dir / "outbox.md"
        outbox.write_text("old message\n")
        notify_outbox(instance_dir, "- new reflection")
        content = outbox.read_text()
        assert "old message" in content
        assert "new reflection" in content

    def test_multiple_reflections_preserve_all(self, instance_dir):
        outbox = instance_dir / "outbox.md"
        notify_outbox(instance_dir, "- first reflection")
        notify_outbox(instance_dir, "- second reflection")
        content = outbox.read_text()
        assert "first reflection" in content
        assert "second reflection" in content


class TestShouldReflectEdgeCases:
    def test_empty_summary(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("")
        assert should_reflect(instance_dir) is False

    def test_no_session_numbers(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Just some text without session numbers\n")
        assert should_reflect(instance_dir) is False


class TestRunReflection:
    @patch("app.self_reflection.subprocess.run")
    def test_successful_reflection(self, mock_run, instance_dir):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="- I notice I like tests\n- I avoid fluff\n"
        )
        result = run_reflection(instance_dir)
        assert "I notice I like tests" in result
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "claude"
        assert "-p" in call_args

    @patch("app.self_reflection.subprocess.run")
    def test_claude_failure_returns_empty(self, mock_run, instance_dir):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")
        result = run_reflection(instance_dir)
        assert result == ""

    @patch("app.self_reflection.subprocess.run")
    def test_claude_failure_logs_stderr(self, mock_run, instance_dir, capsys):
        """Verify that Claude errors are logged to stderr (M4 security finding)."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="API key invalid")
        run_reflection(instance_dir)
        captured = capsys.readouterr()
        assert "[self_reflection] Claude error" in captured.err
        assert "API key invalid" in captured.err

    @patch("app.self_reflection.subprocess.run")
    def test_claude_empty_output(self, mock_run, instance_dir):
        mock_run.return_value = MagicMock(returncode=0, stdout="   ")
        result = run_reflection(instance_dir)
        assert result == ""

    @patch("app.self_reflection.subprocess.run")
    def test_timeout_returns_empty(self, mock_run, instance_dir):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        result = run_reflection(instance_dir)
        assert result == ""

    @patch("app.self_reflection.subprocess.run")
    def test_generic_exception_returns_empty(self, mock_run, instance_dir):
        mock_run.side_effect = OSError("No such file")
        result = run_reflection(instance_dir)
        assert result == ""

    @patch("app.self_reflection.subprocess.run")
    def test_strips_max_turns_error_from_output(self, mock_run, instance_dir):
        """Regression: CLI 'max turns' error was polluting personality-evolution.md."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- I notice patterns\n- I avoid fluff\nError: Reached max turns (1)\n",
        )
        result = run_reflection(instance_dir)
        assert "I notice patterns" in result
        assert "Error" not in result

    @patch("app.self_reflection.subprocess.run")
    def test_only_max_turns_error_returns_empty(self, mock_run, instance_dir):
        """When Claude produces no content, only the error line, return empty."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Error: Reached max turns (1)\n"
        )
        result = run_reflection(instance_dir)
        assert result == ""


class TestSelfReflectionCLI:
    """CLI tests use direct function calls instead of runpy to avoid re-import issues."""

    @patch("app.self_reflection.subprocess.run")
    def test_main_with_force(self, mock_subprocess, instance_dir):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="- observation"
        )
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("# Personality\n")
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Session 5 (project: koan) : test\n")

        from app.self_reflection import main
        with patch.object(sys, "argv", ["self_reflection.py", str(instance_dir), "--force"]):
            main()
        mock_subprocess.assert_called_once()
        assert "observation" in personality.read_text()

    def test_main_skips_when_not_time(self, instance_dir):
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("Session 3 (project: koan) : test\n")

        from app.self_reflection import main
        with patch.object(sys, "argv", ["self_reflection.py", str(instance_dir)]):
            main()  # Should complete without error (not time for reflection)

    def test_main_exits_on_missing_args(self):
        from app.self_reflection import main
        with patch.object(sys, "argv", ["self_reflection.py"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_exits_on_missing_dir(self, tmp_path):
        from app.self_reflection import main
        with patch.object(sys, "argv", ["self_reflection.py", str(tmp_path / "nonexistent")]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch("app.self_reflection.subprocess.run")
    def test_main_with_notify(self, mock_subprocess, instance_dir):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="- observation"
        )
        personality = instance_dir / "memory" / "global" / "personality-evolution.md"
        personality.write_text("# Personality\n")

        from app.self_reflection import main
        with patch.object(sys, "argv", ["self_reflection.py", str(instance_dir), "--force", "--notify"]):
            main()
        outbox = instance_dir / "outbox.md"
        assert outbox.exists()

    @patch("app.self_reflection.subprocess.run")
    def test_main_no_observations(self, mock_subprocess, instance_dir):
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")
        from app.self_reflection import main
        with patch.object(sys, "argv", ["self_reflection.py", str(instance_dir), "--force"]):
            main()  # Should complete without error
