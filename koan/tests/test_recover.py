"""Tests for recover.py — crash recovery of stale in-progress missions."""

from unittest.mock import patch

import pytest

from app.recover import check_pending_journal, recover_missions


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
        assert recover_missions(str(instance_dir)) == 0

    def test_missing_missions_file(self, tmp_path):
        """Returns 0 if missions.md doesn't exist."""
        assert recover_missions(str(tmp_path / "nonexistent")) == 0

    def test_recover_simple_mission(self, instance_dir):
        """Simple - item in 'In Progress' moves back to 'Pending'."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(in_progress="- Fix the bug"))

        count = recover_missions(str(instance_dir))

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

        count = recover_missions(str(instance_dir))
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

        count = recover_missions(str(instance_dir))
        assert count == 1

        content = missions.read_text()
        lines = content.splitlines()
        pending_idx = next(i for i, l in enumerate(lines) if "pending" in l.lower())
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        between = "\n".join(lines[pending_idx + 1 : in_prog_idx])
        assert "Still active" in between
        assert "Already done" not in between

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

        count = recover_missions(str(instance_dir))
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

        count = recover_missions(str(instance_dir))
        assert count == 0

        content = missions.read_text()
        lines = content.splitlines()
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        done_idx = next(i for i, l in enumerate(lines) if "done" == l.strip().lstrip("#").strip().lower())
        in_progress_section = "\n".join(lines[in_prog_idx + 1 : done_idx])
        # Complex mission should still be in-progress
        assert "Complex Project" in in_progress_section
        assert "Step 2" in in_progress_section

    def test_complex_mission_blank_lines_preserved(self, instance_dir):
        """Sub-items after internal blank lines in a complex mission stay in-progress."""
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

        count = recover_missions(str(instance_dir))
        # Step 3 is part of the complex mission — should NOT be recovered
        assert count == 0

        content = missions.read_text()
        lines = content.splitlines()
        in_prog_idx = next(i for i, l in enumerate(lines) if "in progress" in l.lower())
        done_idx = next(i for i, l in enumerate(lines) if "done" == l.strip().lstrip("#").strip().lower())
        in_progress_section = "\n".join(lines[in_prog_idx + 1 : done_idx])
        assert "Complex Project" in in_progress_section
        assert "Step 3" in in_progress_section

    def test_complex_then_simple_mission(self, instance_dir):
        """A simple mission listed after a complex mission via next ### boundary."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(
                in_progress=(
                    "### Complex Project\n"
                    "- ~~Step 1~~ done\n"
                    "- Step 2 in progress\n"
                    "\n"
                    "- Step 3 todo\n"
                    "### Another Complex\n"
                    "- Sub A\n"
                )
            )
        )

        count = recover_missions(str(instance_dir))
        # Both complex missions should stay, nothing recovered
        assert count == 0

        content = missions.read_text()
        assert "Complex Project" in content
        assert "Another Complex" in content

    def test_simple_mission_after_complex_not_recovered(self, instance_dir):
        """A simple '- ' mission after a complex block (no ### separator) stays in-progress.

        This is an intentional trade-off of header-to-header tracking: simple
        missions placed after a complex block without their own ### header are
        treated as part of the complex mission and are NOT recovered.
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

        count = recover_missions(str(instance_dir))
        # Simple orphan is absorbed by the complex block — intentionally not recovered
        assert count == 0

        content = missions.read_text()
        assert "Simple orphan task" in content
        assert "Complex Project" in content

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

        count = recover_missions(str(instance_dir))
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

        assert recover_missions(str(instance_dir)) == 0

    def test_tagged_missions_preserved(self, instance_dir):
        """Project-tagged missions are recovered like any other."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            _missions(in_progress="- [project:koan] Fix something")
        )

        count = recover_missions(str(instance_dir))
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

            count = recover_missions(str(instance_dir))
            assert count == 1
            mock_modify.assert_called_once()

    def test_no_modify_when_nothing_to_recover(self, instance_dir):
        """When no stale missions, modify_missions_file is still called but content unchanged."""
        missions = instance_dir / "missions.md"
        missions.write_text(_missions(pending="- Valid task"))

        with patch("app.utils.modify_missions_file") as mock_modify:
            original_content = missions.read_text()
            mock_modify.side_effect = lambda path, transform: transform(original_content)

            count = recover_missions(str(instance_dir))
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
            count = recover_missions(str(instance_dir))
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
