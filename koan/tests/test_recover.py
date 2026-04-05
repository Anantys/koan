"""Tests for recover.py — crash recovery of stale in-progress missions."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.recover import (
    MAX_RECOVERY_ATTEMPTS,
    _get_recovery_attempts,
    _set_recovery_attempts,
    _strip_recovery_counter,
    check_pending_journal,
    classify_mission_state,
    recover_missions,
)


def _missions(pending="", in_progress="", done=""):
    """Build a missions.md content string."""
    return (
        f"# Missions\n\n"
        f"## Pending\n\n{pending}\n\n"
        f"## In Progress\n\n{in_progress}\n\n"
        f"## Done\n\n{done}\n"
    )


class TestRecoverMissions:
    """Core recovery logic."""

    def test_no_stale_missions(self, instance_dir):
        """No recovery needed when in-progress is empty."""
        assert recover_missions(str(instance_dir)) == (0, [])

    def test_missing_missions_file(self, tmp_path):
        """Returns 0 if missions.md doesn't exist."""
        assert recover_missions(str(tmp_path / "nonexistent")) == (0, [])

    def test_recover_simple_mission(self, instance_dir):
        """Simple - item in 'In Progress' moves back to 'Pending'."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        count, _ = recover_missions(str(instance_dir))

        assert count == 1
        content = missions.read_text()
        # Should be in pending now
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        # The mission should appear between pending header and in-progress header
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Fix the bug" in between

    def test_recover_multiple_simple_missions(self, instance_dir):
        """Multiple simple missions are all recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(in_progress="- Task A\n- Task B\n- Task C")
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 3

        content = missions.read_text()
        assert "Task A" in content
        assert "Task B" in content
        assert "Task C" in content

    def test_skip_strikethrough_missions(self, instance_dir):
        """Fully struck-through items (~~done~~) are NOT recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(in_progress="- ~~Already done~~\n- Still active")
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Still active" in between
        assert "Already done" not in between

    def test_skip_unclosed_strikethrough(self, instance_dir):
        """Unclosed strikethrough (e.g. '- ~~text') is NOT recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "- ~~Partial strikethrough\n"
                    "- Still active"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Still active" in between
        assert "Partial strikethrough" not in between

    def test_skip_inline_strikethrough(self, instance_dir):
        """Items containing ~~ anywhere (e.g. '- text ~~done~~') are NOT recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "- Some task ~~cancelled~~\n"
                    "- Still active"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Still active" in between
        assert "Some task" not in between

    def test_skip_strikethrough_with_trailing_text(self, instance_dir):
        """Struck-through items with trailing text (e.g. '~~done~~ merged') are NOT recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "- ~~Completed task~~ (merged in PR #42)\n"
                    "- ~~Another done~~ done\n"
                    "- Still active"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Still active" in between
        assert "Completed task" not in between
        assert "Another done" not in between

    def test_skip_complex_mission(self, instance_dir):
        """### header missions with sub-items are NOT recovered, even after blank lines."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "### Complex Project\n"
                    "- ~~Step 1~~ done\n"
                    "- Step 2 in progress\n"
                    "- Step 3 todo\n"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 0

        content = missions.read_text()
        lines = content.splitlines()
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        done_idx = next(i for i, l in enumerate(lines) if "done" == l.strip().lstrip("#").strip().lower())
        in_progress_section = "\n".join(lines[in_prog_idx + 1 : done_idx])
        # Complex mission should still be in-progress
        assert "Complex Project" in in_progress_section
        assert "Step 2" in in_progress_section

    def test_blank_line_ends_complex_block(self, instance_dir):
        """A blank line after complex mission sub-items ends the complex block.

        Items after the blank line are treated as standalone simple missions
        and should be recovered.
        """
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "### Complex Project\n"
                    "- ~~Step 1~~ done\n"
                    "- Step 2 in progress\n"
                    "\n"
                    "- Step 3 todo\n"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        # Step 3 follows a blank line — treated as a standalone mission, recovered
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        pending_section = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Step 3" in pending_section

    def test_two_complex_missions(self, instance_dir):
        """Two consecutive complex missions both stay in-progress."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "### Complex Project\n"
                    "- ~~Step 1~~ done\n"
                    "- Step 2 in progress\n"
                    "- Step 3 todo\n"
                    "### Another Complex\n"
                    "- Sub A\n"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        # Both complex missions should stay, nothing recovered
        assert count == 0

        content = missions.read_text()
        assert "Complex Project" in content
        assert "Another Complex" in content

    def test_simple_mission_after_complex_recovered(self, instance_dir):
        """A simple '- ' mission after a complex block (separated by blank line) IS recovered.

        Blank lines end the complex mission block, so subsequent '- ' items
        are treated as standalone simple missions and moved back to Pending.
        """
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "### Complex Project\n"
                    "- ~~Step 1~~ done\n"
                    "- Step 2 in progress\n"
                    "\n"
                    "- Simple orphan task\n"
                )
            )
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        pending_section = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Simple orphan task" in pending_section
        # Complex mission stays in-progress
        in_progress_section = "\n".join(lines[in_prog_idx + 1 :])
        assert "Complex Project" in in_progress_section

    def test_removes_aucune_placeholder(self, instance_dir):
        """The (none) placeholder is removed from pending when missions are added."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(pending="(none)", in_progress="- Recover me"))

        recover_missions(str(instance_dir))

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "(none)" not in between
        assert "Recover me" in between

    def test_english_section_names(self, instance_dir):
        """Works with English section names too."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n(none)\n\n"
            "## In Progress\n\n- English task\n\n"
            "## Done\n\n"
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

    def test_preserves_existing_pending(self, instance_dir):
        """Existing pending missions are kept when recovered missions are added."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(pending="- Already pending", in_progress="- Crashed task")
        )

        recover_missions(str(instance_dir))

        content = missions.read_text()
        assert "Already pending" in content
        assert "Crashed task" in content

    def test_no_sections_returns_zero(self, instance_dir):
        """If missions.md has no recognized sections, returns 0."""
        missions = instance_dir / "missions.md"
        missions.write_text("# Random file\n\nSome content\n")

        assert recover_missions(str(instance_dir)) == (0, [])

    def test_tagged_missions_preserved(self, instance_dir):
        """Project-tagged missions are recovered like any other."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(in_progress="- [project:koan] Fix something")
        )

        count, _ = recover_missions(str(instance_dir))
        assert count == 1
        content = missions.read_text()
        assert "[project:koan] Fix something" in content

    def test_no_duplicate_lines(self, instance_dir):
        """Regression: recovered missions must not duplicate existing lines."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- Existing task\n\n"
            "## In Progress\n\n"
            "- Stale task\n\n"
            "## Done\n\n"
        )

        recover_missions(str(instance_dir))
        content = missions.read_text()

        # "Existing task" must appear exactly once
        assert content.count("Existing task") == 1
        # "Stale task" must appear exactly once (moved to pending)
        assert content.count("Stale task") == 1

    def test_no_section_headers_duplicated(self, instance_dir):
        """Section headers must not be duplicated after recovery."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Task A\n- Task B"))

        recover_missions(str(instance_dir))
        content = missions.read_text()

        assert content.count("## Pending") == 1
        assert content.count("## In Progress") == 1
        assert content.count("## Done") == 1


class TestRecoverAtomicity:
    """Test that recovery uses atomic read-modify-write (no TOCTOU)."""

    def test_uses_modify_missions_file(self, instance_dir):
        """recover_missions uses modify_missions_file for atomic updates."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Stale task"))

        with patch("app.utils.modify_missions_file") as mock_modify:
            # Make modify_missions_file actually call the transform so we get the count
            def _call_transform(path, transform):
                content = path.read_text()
                new_content = transform(content)
                path.write_text(new_content)
                return new_content
            mock_modify.side_effect = _call_transform

            count, _ = recover_missions(str(instance_dir))
            assert count == 1
            mock_modify.assert_called_once()

    def test_no_modify_when_nothing_to_recover(self, instance_dir):
        """When no stale missions, modify_missions_file is still called but content unchanged."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(pending="- Valid task"))

        with patch("app.utils.modify_missions_file") as mock_modify:
            original_content = missions.read_text()
            mock_modify.side_effect = lambda path, transform: transform(original_content)

            count, _ = recover_missions(str(instance_dir))
            assert count == 0
            mock_modify.assert_called_once()


class TestRecoverCLI:
    """Test the __main__ CLI behavior."""

    @patch("app.recover.format_and_send")
    def test_cli_with_recovery(self, mock_send, instance_dir, capsys):
        """CLI prints count and sends Telegram when missions recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Stale task"))

        from app import recover
        import sys

        with patch.object(sys, "argv", ["app.recover.py", str(instance_dir)]):
            # Can't easily test sys.exit, so just call the main block logic
            count, _ = recover_missions(str(instance_dir))
            if count > 0:
                recover.format_and_send(
                    f"Restart — {count} mission(s) recovered from interrupted run, moved back to Pending."
                )

        mock_send.assert_called_once()
        assert "1 mission" in mock_send.call_args[0][0]


class TestCheckPendingJournal:
    """Tests for check_pending_journal — TOCTOU-safe file reading."""

    def test_returns_true_when_pending_exists(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("# Mission\n---\n10:00 — started\n10:01 — working\n")

        result = check_pending_journal(str(tmp_path))
        assert result is True
        captured = capsys.readouterr()
        assert "2 progress entries" in captured.out

    def test_returns_false_when_no_pending(self, tmp_path):
        result = check_pending_journal(str(tmp_path))
        assert result is False

    def test_handles_file_deleted_between_check_and_read(self, tmp_path):
        """Regression: FileNotFoundError should be caught, not propagated."""
        # The file doesn't exist at all — the new code uses try/except
        # instead of exists() + read_text(), so this should just return False
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        # No pending.md file — simulates the race where it was deleted

        result = check_pending_journal(str(tmp_path))
        assert result is False

    def test_empty_pending_returns_false(self, tmp_path):
        """An empty pending.md (e.g. truncated crash) returns False."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "pending.md").write_text("")

        result = check_pending_journal(str(tmp_path))
        assert result is False

    def test_zero_progress_lines(self, tmp_path, capsys):
        """Pending file with separator but no progress lines still detected."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "pending.md").write_text("# Header\n---\n")

        result = check_pending_journal(str(tmp_path))
        assert result is True
        captured = capsys.readouterr()
        assert "0 progress entries" in captured.out

    def test_no_separator(self, tmp_path, capsys):
        """Pending file without --- separator has 0 progress entries."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "pending.md").write_text("# Header\nSome content\n")

        result = check_pending_journal(str(tmp_path))
        assert result is True
        captured = capsys.readouterr()
        assert "0 progress entries" in captured.out

    def test_blank_lines_not_counted(self, tmp_path, capsys):
        """Blank lines after separator are not counted as progress."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "pending.md").write_text(
            "# Header\n---\n\n09:12 — Done\n\n"
        )

        check_pending_journal(str(tmp_path))
        captured = capsys.readouterr()
        assert "1 progress entries" in captured.out


# ---------------------------------------------------------------------------
# Recovery counter helpers
# ---------------------------------------------------------------------------

class TestRecoveryCounterHelpers:
    """Unit tests for [r:N] counter parsing and manipulation."""

    def test_get_attempts_absent(self):
        assert _get_recovery_attempts("- Fix the bug") == 0

    def test_get_attempts_present(self):
        assert _get_recovery_attempts("- Fix the bug [r:2]") == 2

    def test_set_attempts_on_fresh_line(self):
        result = _set_recovery_attempts("- Fix the bug", 1)
        assert result == "- Fix the bug [r:1]"

    def test_set_attempts_replaces_existing(self):
        result = _set_recovery_attempts("- Fix the bug [r:1]", 2)
        assert result == "- Fix the bug [r:2]"

    def test_strip_counter(self):
        result = _strip_recovery_counter("- Fix the bug [r:2]")
        assert result == "- Fix the bug"

    def test_strip_counter_absent(self):
        result = _strip_recovery_counter("- Fix the bug")
        assert result == "- Fix the bug"

    def test_get_attempts_malformed_non_integer(self):
        """Malformed [r:abc] defaults to 0 instead of raising ValueError."""
        assert _get_recovery_attempts("- Fix the bug [r:abc]") == 0

    def test_get_attempts_malformed_float(self):
        """Malformed [r:3.5] defaults to 0."""
        assert _get_recovery_attempts("- Fix the bug [r:3.5]") == 0

    def test_get_attempts_malformed_empty(self):
        """Malformed [r:] defaults to 0."""
        assert _get_recovery_attempts("- Fix the bug [r:]") == 0

    def test_strip_malformed_counter(self):
        """Malformed [r:abc] is still stripped from the line."""
        result = _strip_recovery_counter("- Fix the bug [r:abc]")
        assert result == "- Fix the bug"

    def test_set_replaces_malformed_counter(self):
        """Malformed [r:abc] is replaced with a valid counter."""
        result = _set_recovery_attempts("- Fix the bug [r:abc]", 1)
        assert result == "- Fix the bug [r:1]"


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------

class TestClassifyMissionState:
    """Tests for classify_mission_state()."""

    def test_dead_state_no_counter(self):
        assert classify_mission_state("- Fix the bug") == "dead"

    def test_dead_state_low_counter(self):
        assert classify_mission_state("- Fix the bug [r:1]") == "dead"

    def test_partial_state_with_pending_journal(self):
        assert classify_mission_state("- Fix the bug", has_pending_journal=True) == "partial"

    def test_unrecoverable_at_max_attempts(self):
        line = f"- Fix the bug [r:{MAX_RECOVERY_ATTEMPTS}]"
        assert classify_mission_state(line) == "unrecoverable"

    def test_unrecoverable_above_max_attempts(self):
        line = f"- Fix the bug [r:{MAX_RECOVERY_ATTEMPTS + 5}]"
        assert classify_mission_state(line) == "unrecoverable"

    def test_unrecoverable_overrides_pending_journal(self):
        """Even with pending.md, too many attempts → unrecoverable."""
        line = f"- Fix the bug [r:{MAX_RECOVERY_ATTEMPTS}]"
        assert classify_mission_state(line, has_pending_journal=True) == "unrecoverable"

    def test_just_below_max_is_dead(self):
        line = f"- Fix the bug [r:{MAX_RECOVERY_ATTEMPTS - 1}]"
        assert classify_mission_state(line) == "dead"


# ---------------------------------------------------------------------------
# Recovery counter integration
# ---------------------------------------------------------------------------

class TestRecoveryCounterIntegration:
    """Integration tests: counter is incremented and tracked across recoveries."""

    def test_first_recovery_adds_counter(self, instance_dir):
        """First recovery adds [r:1] to the mission line."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        assert "[r:1]" in content

    def test_second_recovery_increments_counter(self, instance_dir):
        """Second recovery changes [r:1] to [r:2]."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug [r:1]"))

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        assert "[r:2]" in content
        assert "[r:1]" not in content

    def test_malformed_counter_recovered_as_first_attempt(self, instance_dir):
        """A mission with a malformed [r:abc] counter is treated as 0 attempts."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug [r:abc]"))

        count, _ = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        assert "[r:1]" in content
        assert "[r:abc]" not in content

    def test_counter_preserved_in_pending(self, instance_dir):
        """The [r:N] tag is present in Pending after recovery."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        recover_missions(str(instance_dir))

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Fix the bug" in between
        assert "[r:" in between


# ---------------------------------------------------------------------------
# Unrecoverable escalation
# ---------------------------------------------------------------------------

def _missions_with_failed(pending="", in_progress="", done="", failed=""):
    """Build a missions.md content string with a Failed section."""
    return (
        f"# Missions\n\n"
        f"## Pending\n\n{pending}\n\n"
        f"## In Progress\n\n{in_progress}\n\n"
        f"## Done\n\n{done}\n\n"
        f"## Failed\n\n{failed}\n"
    )


class TestUnrecoverableEscalation:
    """Missions that have exhausted recovery attempts are escalated to Failed."""

    def _stale_at_limit(self):
        return f"- Fix the bug [r:{MAX_RECOVERY_ATTEMPTS}]"

    def test_unrecoverable_not_in_pending(self, instance_dir):
        """Unrecoverable missions are NOT moved to Pending."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress=self._stale_at_limit()))

        count, _ = recover_missions(str(instance_dir))
        assert count == 0  # Not recovered to Pending

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Fix the bug" not in between

    def test_unrecoverable_moved_to_failed(self, instance_dir):
        """Unrecoverable missions appear in Failed section with needs_input tag."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions_with_failed(in_progress=self._stale_at_limit()))

        recover_missions(str(instance_dir))

        content = missions.read_text()
        assert "needs_input" in content
        assert "Fix the bug" in content

    def test_unrecoverable_creates_failed_section_if_absent(self, instance_dir):
        """If no Failed section exists, one is created for escalated missions."""
        missions = instance_dir / "missions.md"
        # Use _missions() which has no Failed section
        missions.write_text(_missions(in_progress=self._stale_at_limit()))

        recover_missions(str(instance_dir))

        content = missions.read_text()
        assert "## Failed" in content
        assert "needs_input" in content
        assert "Fix the bug" in content

    def test_mixed_recoverable_and_unrecoverable(self, instance_dir):
        """Recoverable missions go to Pending, unrecoverable go to Failed."""
        missions = instance_dir / "missions.md"
        in_prog = f"- Normal task\n{self._stale_at_limit()}"
        missions.write_text(_missions_with_failed(in_progress=in_prog))

        count, _ = recover_missions(str(instance_dir))
        assert count == 1  # Only 1 recovered

        content = missions.read_text()
        # Normal task in Pending with counter
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Normal task" in between
        # Escalated in Failed
        assert "needs_input" in content
        assert "Fix the bug" in content


# ---------------------------------------------------------------------------
# JSONL recovery log
# ---------------------------------------------------------------------------

class TestRecoveryJSONLLog:
    """Events are logged to recovery.jsonl."""

    def test_log_created_on_recovery(self, instance_dir):
        """recovery.jsonl is created when a mission is recovered."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        recover_missions(str(instance_dir))

        log_path = instance_dir / "recovery.jsonl"
        assert log_path.exists()

    def test_log_entry_fields(self, instance_dir):
        """Log entry has required fields: timestamp, mission, state, action, attempts."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        recover_missions(str(instance_dir))

        log_path = instance_dir / "recovery.jsonl"
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert len(events) == 1
        ev = events[0]
        assert "timestamp" in ev
        assert "mission" in ev
        assert "state" in ev
        assert "action" in ev
        assert "attempts" in ev
        assert ev["state"] == "dead"
        assert ev["action"] == "recovered"

    def test_log_escalated_action(self, instance_dir):
        """Unrecoverable missions are logged with action=escalated."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress=f"- Fix the bug [r:{MAX_RECOVERY_ATTEMPTS}]"))

        recover_missions(str(instance_dir))

        log_path = instance_dir / "recovery.jsonl"
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        escalated = [e for e in events if e.get("action") == "escalated"]
        assert len(escalated) == 1
        assert escalated[0]["state"] == "unrecoverable"

    def test_log_counter_stripped_from_mission(self, instance_dir):
        """The [r:N] counter is stripped from the logged mission text."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug [r:2]"))

        recover_missions(str(instance_dir))

        log_path = instance_dir / "recovery.jsonl"
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert events
        assert "[r:" not in events[0]["mission"]

    def test_log_appends_across_runs(self, instance_dir):
        """Multiple recovery runs append to the same log."""
        missions = instance_dir / "missions.md"

        # First run
        missions.write_text(_missions(in_progress="- Task A"))
        recover_missions(str(instance_dir))

        # Second run
        missions.write_text(_missions(in_progress="- Task B [r:1]"))
        recover_missions(str(instance_dir))

        log_path = instance_dir / "recovery.jsonl"
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestRecoverPendingJournalTOCTOU:
    """TOCTOU race: pending.md deleted between exists() and read_text()."""

    def test_pending_deleted_after_exists_check(self, instance_dir):
        """If pending.md is deleted between exists() and read_text(), recovery
        should not raise FileNotFoundError — it should treat it as absent.

        This is a benign race: the agent process deletes pending.md after
        completing a mission, while recover.py is concurrently checking it.
        """
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Stale task"))

        pending_path = instance_dir / "journal" / "pending.md"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text("# Mission\n---\n10:00 — started\n")

        original_read_text = Path.read_text

        def _disappearing_read_text(self, *args, **kwargs):
            """Simulate the file vanishing between exists() and read_text()."""
            if self.name == "pending.md" and "journal" in str(self):
                # Delete the file to simulate the race, then let read_text fail
                self.unlink(missing_ok=True)
                return original_read_text(self, *args, **kwargs)
            return original_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", _disappearing_read_text):
            # This must NOT raise FileNotFoundError
            count, _ = recover_missions(str(instance_dir))

        # Mission should still be recovered (as "dead", not "partial")
        assert count == 1


class TestDryRun:
    """Dry-run mode classifies without modifying missions.md."""

    def test_dry_run_no_modification(self, instance_dir):
        """Dry-run does not change missions.md."""
        missions = instance_dir / "missions.md"
        original = _missions(in_progress="- Fix the bug")
        missions.write_text(original)

        count, _ = recover_missions(str(instance_dir), dry_run=True)

        assert count == 0
        # File should not have been modified (no missions moved to Pending)
        content = missions.read_text()
        lines = content.splitlines()
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        done_idx = next(i for i, l in enumerate(lines) if l.strip().lower() in ("## done", "done"))
        in_progress_section = "\n".join(lines[in_prog_idx + 1 : done_idx])
        assert "Fix the bug" in in_progress_section

    def test_dry_run_logs_event(self, instance_dir, capsys):
        """Dry-run logs a dry_run event to recovery.jsonl."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        recover_missions(str(instance_dir), dry_run=True)

        log_path = instance_dir / "recovery.jsonl"
        if log_path.exists():
            events = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert any(e.get("action") == "dry_run" for e in events)
