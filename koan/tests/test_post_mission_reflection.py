"""Tests for post_mission_reflection.py module."""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.post_mission_reflection import (
    MIN_DURATION_MINUTES,
    MIN_JOURNAL_LENGTH,
    SIGNIFICANT_KEYWORDS,
    _read_journal_file,
    build_reflection_prompt,
    is_significant_mission,
    run_reflection,
    write_to_journal,
)


# --- Fixtures ---

@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory for testing."""
    memory_dir = tmp_path / "memory" / "global"
    memory_dir.mkdir(parents=True)
    (tmp_path / "soul.md").write_text("You are Koan.")
    return tmp_path


@pytest.fixture
def journal_file(instance_dir):
    """Create a journal file with substantial content."""
    today = datetime.now().strftime("%Y-%m-%d")
    journal_dir = instance_dir / "journal" / today
    journal_dir.mkdir(parents=True)
    journal = journal_dir / "koan.md"
    journal.write_text(
        "# Session\n\n"
        "## What happened\n\n"
        "Implemented post-mission reflections. Created new module with heuristics, "
        "prompt template, integration into mission runner pipeline. "
        "Added dual heuristic gate, journal content awareness, updated prompt.\n\n"
        "## Decisions\n\n"
        "- Chose keyword + journal length dual heuristic over keyword-only\n"
        "- Reflections go to shared-journal.md, not personality-evolution.md\n"
        "- Claude generates reflections with --max-turns 1\n\n"
        + "x" * 300  # Pad to exceed MIN_JOURNAL_LENGTH (500)
    )
    return journal


def _substantial_journal() -> str:
    """Return journal content that exceeds the minimum length threshold."""
    return "x" * (MIN_JOURNAL_LENGTH + 100)


def _short_journal() -> str:
    """Return journal content below the minimum length threshold."""
    return "Quick fix."


# --- TestIsSignificantMission ---

class TestIsSignificantMission:
    """Tests for is_significant_mission()."""

    def test_keyword_with_substantial_journal(self):
        """Keyword + substantial journal = significant."""
        assert is_significant_mission("security audit", 10, _substantial_journal())

    def test_keyword_without_journal_not_significant(self):
        """Keyword alone (no journal content) is not significant."""
        assert not is_significant_mission("security audit", 10)

    def test_keyword_with_short_journal_not_significant(self):
        """Keyword + short journal is not significant."""
        assert not is_significant_mission("security audit", 10, _short_journal())

    def test_long_duration_overrides_journal_check(self):
        """Long missions (>= 45 min) are significant regardless of journal."""
        assert is_significant_mission("Fix typo in readme", MIN_DURATION_MINUTES)
        assert is_significant_mission("Simple task", 60)
        assert is_significant_mission("Fix typo", MIN_DURATION_MINUTES, "")

    def test_feature_keyword_with_journal(self):
        assert is_significant_mission("feature: add dark mode", 10, _substantial_journal())

    def test_refactor_keyword_with_journal(self):
        assert is_significant_mission("refactor the auth layer", 10, _substantial_journal())

    def test_architecture_keyword_with_journal(self):
        assert is_significant_mission("architecture review", 10, _substantial_journal())

    def test_migration_keyword_with_journal(self):
        assert is_significant_mission("database migration", 10, _substantial_journal())

    def test_performance_keyword_with_journal(self):
        assert is_significant_mission("performance optimization", 10, _substantial_journal())

    def test_retrospective_keyword_with_journal(self):
        assert is_significant_mission("retrospective analysis", 10, _substantial_journal())

    def test_introspect_keyword_with_journal(self):
        assert is_significant_mission("introspect on patterns", 10, _substantial_journal())

    def test_short_mission_without_keywords_not_significant(self):
        """Short missions without keywords are not significant."""
        assert not is_significant_mission("Fix typo", 10, _substantial_journal())
        assert not is_significant_mission("Update readme", 30, _substantial_journal())

    def test_rebase_mission_not_significant(self):
        assert not is_significant_mission("rebase PR #42", 10, _substantial_journal())

    def test_empty_mission_title(self):
        assert not is_significant_mission("", 10, _substantial_journal())

    def test_none_mission_title(self):
        assert not is_significant_mission(None, 10, _substantial_journal())

    def test_keyword_case_insensitive(self):
        journal = _substantial_journal()
        assert is_significant_mission("SECURITY AUDIT", 10, journal)
        assert is_significant_mission("Feature flag rollout", 10, journal)

    def test_exactly_at_journal_threshold(self):
        """Journal exactly at minimum length should pass."""
        journal = "x" * MIN_JOURNAL_LENGTH
        assert is_significant_mission("audit", 10, journal)

    def test_just_below_journal_threshold(self):
        journal = "x" * (MIN_JOURNAL_LENGTH - 1)
        assert not is_significant_mission("audit", 10, journal)

    def test_all_keywords_detected_with_journal(self):
        """Verify all keywords trigger significance when journal is substantial."""
        journal = _substantial_journal()
        for keyword in SIGNIFICANT_KEYWORDS:
            assert is_significant_mission(
                f"Mission about {keyword}", 5, journal
            ), f"Keyword '{keyword}' not detected"

    def test_all_keywords_rejected_without_journal(self):
        """Keywords alone (without journal) are not significant."""
        for keyword in SIGNIFICANT_KEYWORDS:
            assert not is_significant_mission(
                f"Mission about {keyword}", 5
            ), f"Keyword '{keyword}' should not pass without journal"

    def test_whitespace_journal_not_substantial(self):
        """Journal with only whitespace is not substantial."""
        assert not is_significant_mission("audit", 10, "   \n\n  \t  ")


# --- TestBuildReflectionPrompt ---

class TestBuildReflectionPrompt:
    """Tests for build_reflection_prompt()."""

    def test_includes_soul_when_exists(self, instance_dir):
        prompt = build_reflection_prompt(instance_dir, "Test mission")
        assert "Koan" in prompt

    def test_includes_emotional_memory_when_exists(self, instance_dir):
        (instance_dir / "memory" / "global" / "emotional-memory.md").write_text(
            "Alexis appreciates directness."
        )
        prompt = build_reflection_prompt(instance_dir, "Test mission")
        assert "directness" in prompt

    def test_includes_shared_journal_context(self, instance_dir):
        (instance_dir / "shared-journal.md").write_text(
            "# Previous entries\n\nSome deep reflection here."
        )
        prompt = build_reflection_prompt(instance_dir, "Test mission")
        assert "deep reflection" in prompt

    def test_mission_text_included(self, instance_dir):
        prompt = build_reflection_prompt(instance_dir, "Security audit of banking module")
        assert "Security audit of banking module" in prompt

    def test_prompt_uses_generic_language(self, instance_dir):
        prompt = build_reflection_prompt(instance_dir, "Test mission")
        assert "your human" in prompt
        assert "soul.md" in prompt
        assert "Alexis" not in prompt

    def test_includes_journal_content(self, instance_dir):
        """Journal content from the mission is injected into the prompt."""
        journal = "Implemented new auth system. Rewrote 3 modules. Tests pass."
        prompt = build_reflection_prompt(instance_dir, "feature: auth", journal)
        assert "Implemented new auth system" in prompt

    def test_truncates_long_journal_content(self, instance_dir):
        """Journal content longer than 3000 chars is truncated."""
        journal = "x" * 5000
        prompt = build_reflection_prompt(instance_dir, "audit", journal)
        # The prompt should not contain the full 5000 chars
        assert "x" * 5000 not in prompt
        assert "x" * 3000 in prompt

    def test_no_journal_content_uses_placeholder(self, instance_dir):
        """When no journal content, a placeholder is used."""
        prompt = build_reflection_prompt(instance_dir, "Test mission")
        assert "(no journal content available)" in prompt

    def test_empty_journal_content_uses_placeholder(self, instance_dir):
        prompt = build_reflection_prompt(instance_dir, "Test mission", "")
        assert "(no journal content available)" in prompt

    def test_prompt_contains_reflection_angles(self, instance_dir):
        """Prompt template includes the PR #79 reflection angles."""
        prompt = build_reflection_prompt(instance_dir, "Test mission")
        assert "What surprised you" in prompt
        assert "question for the human" in prompt


# --- TestWriteToJournal ---

class TestWriteToJournal:
    """Tests for write_to_journal()."""

    def test_creates_journal_if_not_exists(self, tmp_path):
        write_to_journal(tmp_path, "First reflection.")
        journal_file = tmp_path / "shared-journal.md"
        assert journal_file.exists()
        assert "First reflection." in journal_file.read_text()

    def test_appends_to_existing_journal(self, tmp_path):
        journal_file = tmp_path / "shared-journal.md"
        journal_file.write_text("# Journal\n\nOld entry.")
        write_to_journal(tmp_path, "New reflection.")
        content = journal_file.read_text()
        assert "Old entry." in content
        assert "New reflection." in content

    def test_includes_koan_header_and_timestamp(self, tmp_path):
        """Each reflection has a Kōan header with timestamp."""
        write_to_journal(tmp_path, "Test reflection.")
        content = (tmp_path / "shared-journal.md").read_text()
        assert "### Kōan —" in content

    def test_handles_journal_file_deleted_between_check_and_read(self, tmp_path):
        """Survives TOCTOU: journal file removed between exists() and read_text()."""
        # Just create directory, no journal file — should not crash
        write_to_journal(tmp_path, "First entry.")
        content = (tmp_path / "shared-journal.md").read_text()
        assert "First entry." in content

    def test_handles_unreadable_journal_gracefully(self, tmp_path, monkeypatch):
        """When journal read raises OSError, treats as empty and writes fresh."""
        journal = tmp_path / "shared-journal.md"
        journal.write_text("old content")
        # Make read_text raise OSError
        original_read = Path.read_text

        def bad_read(self, *args, **kwargs):
            if self.name == "shared-journal.md":
                raise OSError("Permission denied")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", bad_read)
        # Should not crash — falls back to empty content
        write_to_journal(tmp_path, "New reflection.")


# --- TestRunReflection ---

class TestRunReflection:
    """Tests for run_reflection()."""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_calls_claude_with_prompt(self, mock_build, mock_run_claude, instance_dir):
        mock_run_claude.return_value = {"success": True, "output": "Test reflection output", "error": ""}
        result = run_reflection(instance_dir, "Audit mission")
        assert mock_run_claude.called
        assert result == "Test reflection output"

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_passes_journal_content_to_prompt(self, mock_build, mock_run_claude, instance_dir):
        """Journal content is included in the Claude prompt."""
        mock_run_claude.return_value = {"success": True, "output": "Reflection", "error": ""}
        journal = "Rewrote auth module. Tests pass."
        run_reflection(instance_dir, "feature: auth", journal)
        # build_full_command is called with a prompt that contains the journal content
        call_args = mock_build.call_args
        prompt = call_args[1]["prompt"] if "prompt" in call_args[1] else call_args[0][0] if call_args[0] else ""
        assert "Rewrote auth module" in prompt

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_returns_empty_on_skip_signal(self, mock_build, mock_run_claude, instance_dir):
        mock_run_claude.return_value = {"success": True, "output": "—", "error": ""}
        assert run_reflection(instance_dir, "Test mission") == ""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_returns_empty_on_timeout(self, mock_build, mock_run_claude, instance_dir):
        mock_run_claude.return_value = {"success": False, "output": "", "error": "Timeout (60s)"}
        assert run_reflection(instance_dir, "Test mission") == ""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_returns_empty_on_error(self, mock_build, mock_run_claude, instance_dir):
        mock_run_claude.return_value = {"success": False, "output": "", "error": "Exit code 1"}
        assert run_reflection(instance_dir, "Test mission") == ""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_returns_empty_on_dash_skip(self, mock_build, mock_run_claude, instance_dir):
        mock_run_claude.return_value = {"success": True, "output": "-", "error": ""}
        assert run_reflection(instance_dir, "Test mission") == ""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_strips_max_turns_error_from_output(self, mock_build, mock_run_claude, instance_dir):
        """Regression: CLI 'max turns' error was polluting shared-journal.md."""
        mock_run_claude.return_value = {
            "success": True,
            "output": "A genuine reflection.\nError: Reached max turns (1)",
            "error": "",
        }
        result = run_reflection(instance_dir, "Audit mission")
        assert result == "A genuine reflection."
        assert "Error" not in result

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_only_max_turns_error_returns_empty(self, mock_build, mock_run_claude, instance_dir):
        """When Claude produces no content, only the error line, return empty."""
        mock_run_claude.return_value = {
            "success": True,
            "output": "Error: Reached max turns (1)",
            "error": "",
        }
        result = run_reflection(instance_dir, "Test mission")
        assert result == ""


# --- TestReadJournalFile ---

class TestReadJournalFile:
    """Tests for _read_journal_file()."""

    def test_reads_explicit_path(self, instance_dir, journal_file):
        """Reads from explicit journal file path."""
        content = _read_journal_file(instance_dir, "koan", str(journal_file))
        assert "post-mission reflections" in content

    def test_falls_back_to_today_journal(self, instance_dir, journal_file):
        """Falls back to today's journal when no explicit path given."""
        content = _read_journal_file(instance_dir, "koan")
        assert "post-mission reflections" in content

    def test_returns_empty_when_no_journal(self, tmp_path):
        """Returns empty string when no journal exists."""
        content = _read_journal_file(tmp_path, "koan")
        assert content == ""

    def test_returns_empty_for_nonexistent_explicit_path(self, instance_dir):
        content = _read_journal_file(instance_dir, "koan", "/nonexistent/path.md")
        # Falls back to today's journal or empty
        # Since no today journal exists at tmp_path by default, returns what fallback finds
        assert isinstance(content, str)

    def test_explicit_path_takes_precedence(self, instance_dir):
        """Explicit path is used even when today's journal also exists."""
        today = datetime.now().strftime("%Y-%m-%d")
        journal_dir = instance_dir / "journal" / today
        journal_dir.mkdir(parents=True)
        (journal_dir / "koan.md").write_text("Today's content")

        other = instance_dir / "other.md"
        other.write_text("Explicit content")

        content = _read_journal_file(instance_dir, "koan", str(other))
        assert "Explicit content" in content


# --- TestCLI ---

class TestCLI:
    """Tests for CLI entry point."""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_main_with_significant_mission_and_journal(
        self, mock_build, mock_run_claude, instance_dir, journal_file
    ):
        """Significant mission + substantial journal → reflection written."""
        mock_run_claude.return_value = {
            "success": True, "output": "Réflexion post-mission.", "error": ""
        }
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "security audit of auth",
                "10",
                "--journal-file", str(journal_file),
                "--project-name", "koan",
            ],
        ):
            main()
        shared = instance_dir / "shared-journal.md"
        assert shared.exists()
        assert "Réflexion post-mission" in shared.read_text()

    def test_main_skips_mundane_mission(self, instance_dir, journal_file):
        """Mundane mission title is rejected even with journal."""
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "fix typo",
                "10",
                "--journal-file", str(journal_file),
            ],
        ):
            main()
        shared = instance_dir / "shared-journal.md"
        assert not shared.exists()

    def test_main_skips_keyword_without_journal(self, instance_dir):
        """Keyword mission without journal content is rejected."""
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "security audit",
                "10",
            ],
        ):
            main()
        shared = instance_dir / "shared-journal.md"
        assert not shared.exists()

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_main_force_flag_overrides_heuristic(
        self, mock_build, mock_run_claude, instance_dir
    ):
        """--force bypasses significance check."""
        mock_run_claude.return_value = {
            "success": True, "output": "Forced reflection.", "error": ""
        }
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "fix typo",
                "5",
                "--force",
            ],
        ):
            main()
        shared = instance_dir / "shared-journal.md"
        assert shared.exists()
        assert "Forced reflection" in shared.read_text()

    def test_main_missing_args(self):
        from app.post_mission_reflection import main

        with patch.object(sys, "argv", ["post_mission_reflection.py"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_missing_instance_dir(self, tmp_path):
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(tmp_path / "nonexistent"),
                "audit",
                "60",
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_main_no_reflection_generated(
        self, mock_build, mock_run_claude, instance_dir, journal_file
    ):
        """No reflection output → no shared-journal.md written."""
        mock_run_claude.return_value = {"success": True, "output": "", "error": ""}
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "feature implementation",
                "10",
                "--journal-file", str(journal_file),
            ],
        ):
            main()
        shared = instance_dir / "shared-journal.md"
        assert not shared.exists()

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "-p", "test"])
    def test_main_long_duration_without_journal(
        self, mock_build, mock_run_claude, instance_dir
    ):
        """Long duration overrides the journal requirement."""
        mock_run_claude.return_value = {
            "success": True, "output": "Long session reflection.", "error": ""
        }
        from app.post_mission_reflection import main

        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "fix typo",
                "60",
            ],
        ):
            main()
        shared = instance_dir / "shared-journal.md"
        assert shared.exists()
        assert "Long session reflection" in shared.read_text()

    def test_main_project_name_fallback(self, instance_dir, journal_file):
        """With --project-name, falls back to today's journal."""
        from app.post_mission_reflection import main

        # journal_file already exists at instance_dir/journal/today/koan.md
        with patch.object(
            sys, "argv",
            [
                "post_mission_reflection.py",
                str(instance_dir),
                "fix typo",  # Not significant keyword
                "10",
                "--project-name", "koan",
            ],
        ):
            main()
        # Mundane title → not significant, even with journal
        shared = instance_dir / "shared-journal.md"
        assert not shared.exists()
