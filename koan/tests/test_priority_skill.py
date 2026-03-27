"""Tests for the /priority core skill — mission queue reordering."""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Handler tests (direct handler invocation)
# ---------------------------------------------------------------------------

class TestPriorityHandler:
    """Test the priority skill handler directly."""

    def _make_ctx(self, tmp_path, missions_content=None, args=""):
        """Create a SkillContext with optional missions.md."""
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)
        if missions_content is not None:
            (instance_dir / "missions.md").write_text(missions_content)
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance_dir,
            command_name="priority",
            args=args,
        )

    SAMPLE = textwrap.dedent("""\
        # Missions

        ## Pending

        - first task
        - second task
        - third task

        ## In Progress

        ## Done
    """)

    def test_bare_shows_queue_with_hint(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE)
        result = handle(ctx)
        assert "PENDING" in result
        assert "1." in result
        assert "first task" in result
        assert "Usage" in result

    def test_bare_empty_queue(self, tmp_path):
        from skills.core.priority.handler import handle

        missions = "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        ctx = self._make_ctx(tmp_path, missions)
        result = handle(ctx)
        assert "empty" in result.lower()
        assert "Usage" in result

    def test_bare_no_file(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "empty" in result.lower()

    def test_move_to_top(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE, args="3")
        result = handle(ctx)
        assert "third task" in result
        assert "⬆️" in result

        # Verify file was rewritten
        content = (tmp_path / "instance" / "missions.md").read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- third task"

    def test_move_to_position(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE, args="3 2")
        result = handle(ctx)
        assert "third task" in result
        assert "🔀" in result
        assert "position 2" in result

    def test_invalid_number(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE, args="abc")
        result = handle(ctx)
        assert "Invalid" in result

    def test_invalid_target(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE, args="1 xyz")
        result = handle(ctx)
        assert "Invalid" in result

    def test_out_of_range(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE, args="10")
        result = handle(ctx)
        assert "Invalid position" in result

    def test_same_position(self, tmp_path):
        from skills.core.priority.handler import handle

        ctx = self._make_ctx(tmp_path, self.SAMPLE, args="2 2")
        result = handle(ctx)
        assert "already at" in result

    def test_project_tags_preserved(self, tmp_path):
        from skills.core.priority.handler import handle

        missions = textwrap.dedent("""\
            # Missions

            ## Pending

            - [project:koan] first
            - [project:web] second
            - third

            ## In Progress

            ## Done
        """)
        ctx = self._make_ctx(tmp_path, missions, args="2 1")
        result = handle(ctx)
        assert "second" in result

        content = (tmp_path / "instance" / "missions.md").read_text()
        assert "[project:web]" in content


# ---------------------------------------------------------------------------
# Integration: command routing via awake.py
# ---------------------------------------------------------------------------

class TestPriorityCommandRouting:
    """Test that /priority routes to the priority skill via awake."""

    @patch("app.command_handlers.send_telegram")
    def test_priority_routes_via_skill(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- first\n- second\n- third\n\n"
            "## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/priority 3")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "third" in output

    @patch("app.command_handlers.send_telegram")
    def test_priority_bare_shows_queue(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- task A\n\n"
            "## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/priority")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "task A" in output
        assert "Usage" in output

    @patch("app.command_handlers.send_telegram")
    def test_priority_appears_in_help(self, mock_send, tmp_path):
        """Verify /priority is included in /help missions group output."""
        from app.command_handlers import _handle_help_detail

        _handle_help_detail("missions")
        mock_send.assert_called_once()
        help_text = mock_send.call_args[0][0]
        assert "/priority" in help_text
