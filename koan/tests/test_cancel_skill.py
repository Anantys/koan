"""Tests for the /cancel core skill — cancel pending missions."""

import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# missions.py: list_pending / cancel_pending_mission
# ---------------------------------------------------------------------------

CANCEL_CONTENT = (
    "# Missions\n\n"
    "## Pending\n\n"
    "- [project:koan] fix auth bug\n"
    "- add dark mode\n"
    "- [project:koan] refactor tests\n\n"
    "## In Progress\n\n"
    "- doing stuff\n\n"
    "## Done\n"
)


class TestListPending:
    def test_returns_pending_missions(self):
        from app.missions import list_pending

        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- fix auth bug\n"
            "- add dark mode\n"
            "- refactor tests\n\n"
            "## In Progress\n\n"
            "- doing stuff\n"
        )
        result = list_pending(content)
        assert len(result) == 3
        assert "- fix auth bug" in result

    def test_empty_pending(self):
        from app.missions import list_pending

        content = "# Missions\n\n## Pending\n\n## In Progress\n\n"
        assert list_pending(content) == []

    def test_english_headers(self):
        from app.missions import list_pending

        content = "# Missions\n\n## Pending\n\n- task one\n\n## In Progress\n\n"
        result = list_pending(content)
        assert len(result) == 1
        assert "- task one" in result


class TestCancelPendingMission:
    def test_cancel_by_number(self):
        from app.missions import cancel_pending_mission

        new_content, cancelled = cancel_pending_mission(CANCEL_CONTENT, "2")
        assert "dark mode" in cancelled
        assert "- add dark mode" not in new_content
        assert "- [project:koan] fix auth bug" in new_content
        assert "- [project:koan] refactor tests" in new_content

    def test_cancel_by_number_first(self):
        from app.missions import cancel_pending_mission

        new_content, cancelled = cancel_pending_mission(CANCEL_CONTENT, "1")
        assert "fix auth bug" in cancelled
        assert "- [project:koan] fix auth bug" not in new_content
        assert "- add dark mode" in new_content

    def test_cancel_by_number_last(self):
        from app.missions import cancel_pending_mission

        new_content, cancelled = cancel_pending_mission(CANCEL_CONTENT, "3")
        assert "refactor tests" in cancelled
        assert "- [project:koan] refactor tests" not in new_content
        assert "- add dark mode" in new_content

    def test_cancel_by_keyword(self):
        from app.missions import cancel_pending_mission

        new_content, cancelled = cancel_pending_mission(CANCEL_CONTENT, "dark mode")
        assert "dark mode" in cancelled
        assert "- add dark mode" not in new_content

    def test_cancel_by_keyword_case_insensitive(self):
        from app.missions import cancel_pending_mission

        _, cancelled = cancel_pending_mission(CANCEL_CONTENT, "DARK MODE")
        assert "dark mode" in cancelled

    def test_cancel_by_keyword_partial(self):
        from app.missions import cancel_pending_mission

        _, cancelled = cancel_pending_mission(CANCEL_CONTENT, "auth")
        assert "fix auth bug" in cancelled

    def test_cancel_number_out_of_range(self):
        from app.missions import cancel_pending_mission

        with pytest.raises(ValueError, match="Mission #10 not found"):
            cancel_pending_mission(CANCEL_CONTENT, "10")

    def test_cancel_number_zero(self):
        from app.missions import cancel_pending_mission

        with pytest.raises(ValueError, match="Mission #0 not found"):
            cancel_pending_mission(CANCEL_CONTENT, "0")

    def test_cancel_keyword_no_match(self):
        from app.missions import cancel_pending_mission

        with pytest.raises(ValueError, match="No pending mission matching"):
            cancel_pending_mission(CANCEL_CONTENT, "nonexistent")

    def test_cancel_empty_pending(self):
        from app.missions import cancel_pending_mission

        content = "# Missions\n\n## Pending\n\n## In Progress\n\n"
        with pytest.raises(ValueError, match="No pending missions"):
            cancel_pending_mission(content, "1")

    def test_cancel_preserves_other_sections(self):
        from app.missions import cancel_pending_mission

        new_content, _ = cancel_pending_mission(CANCEL_CONTENT, "1")
        assert "## In Progress" in new_content
        assert "- doing stuff" in new_content
        assert "## Done" in new_content

    def test_cancel_with_continuation_lines(self):
        from app.missions import cancel_pending_mission

        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- fix auth bug\n"
            "  with extra details\n"
            "- add dark mode\n\n"
            "## In Progress\n\n"
        )
        new_content, cancelled = cancel_pending_mission(content, "1")
        assert "fix auth bug" in cancelled
        assert "fix auth bug" not in new_content
        assert "extra details" not in new_content
        assert "- add dark mode" in new_content


# ---------------------------------------------------------------------------
# Handler tests (direct handler invocation)
# ---------------------------------------------------------------------------

class TestCancelHandler:
    """Test the cancel skill handler directly."""

    def _make_ctx(self, tmp_path, missions_content=None, args=""):
        """Create a SkillContext with optional missions.md."""
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)
        if missions_content is not None:
            (instance_dir / "missions.md").write_text(missions_content)
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance_dir,
            command_name="cancel",
            args=args,
        )

    MISSIONS = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- [project:koan] fix auth bug\n"
        "- add dark mode\n"
        "- refactor tests\n\n"
        "## In Progress\n\n"
        "## Done\n"
    )

    def test_empty_args_shows_list(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS)
        result = handle(ctx)
        assert "1." in result
        assert "2." in result
        assert "3." in result
        assert "/cancel" in result

    def test_empty_args_no_missions(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(
            tmp_path,
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n",
        )
        result = handle(ctx)
        assert "No pending" in result

    def test_empty_args_no_file(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "No pending" in result

    def test_cancel_by_number(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS, args="2")
        result = handle(ctx)
        assert "dark mode" in result
        assert "cancelled" in result.lower()
        # File updated
        content = (tmp_path / "instance" / "missions.md").read_text()
        assert "- add dark mode" not in content
        assert "fix auth bug" in content

    def test_cancel_by_keyword(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS, args="auth")
        result = handle(ctx)
        assert "auth" in result.lower()
        content = (tmp_path / "instance" / "missions.md").read_text()
        assert "fix auth bug" not in content

    def test_cancel_no_match(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS, args="nonexistent")
        result = handle(ctx)
        assert "No pending mission matching" in result

    def test_cancel_number_out_of_range(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS, args="99")
        result = handle(ctx)
        assert "not found" in result.lower() or "#99" in result

    def test_cancel_strips_project_tag_in_display(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS, args="1")
        result = handle(ctx)
        assert "[project:koan]" not in result
        assert "fix auth bug" in result

    def test_cancel_shows_project_prefix(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS, args="1")
        result = handle(ctx)
        assert "[koan]" in result

    def test_list_strips_project_tag_in_display(self, tmp_path):
        from skills.core.cancel.handler import handle

        ctx = self._make_ctx(tmp_path, self.MISSIONS)
        result = handle(ctx)
        assert "[project:koan]" not in result
        assert "[koan]" in result
        assert "fix auth bug" in result


# ---------------------------------------------------------------------------
# _clean_display helper
# ---------------------------------------------------------------------------

class TestCleanMissionDisplay:
    """Tests for shared clean_mission_display() in missions.py."""

    def test_strip_dash_prefix(self):
        from app.missions import clean_mission_display
        assert clean_mission_display("- fix the bug") == "fix the bug"

    def test_strip_project_tag(self):
        from app.missions import clean_mission_display
        result = clean_mission_display("- [project:koan] fix parser")
        assert result == "[koan] fix parser"

    def test_strip_projet_tag(self):
        from app.missions import clean_mission_display
        result = clean_mission_display("- [projet:webapp] add feature")
        assert result == "[webapp] add feature"

    def test_no_tag(self):
        from app.missions import clean_mission_display
        assert clean_mission_display("- simple task") == "simple task"

    def test_truncation(self):
        from app.missions import clean_mission_display
        long = "- " + "a" * 200
        result = clean_mission_display(long)
        assert result.endswith("...")
        assert len(result) == 120

    def test_custom_max_length(self):
        from app.missions import clean_mission_display
        text = "- " + "b" * 100
        result = clean_mission_display(text, max_length=50)
        assert len(result) == 50
        assert result.endswith("...")


# ---------------------------------------------------------------------------
# Integration: command routing via awake.py
# ---------------------------------------------------------------------------

class TestCancelAliasRegistry:
    """Test that /remove and /clear aliases resolve to the cancel skill."""

    def test_remove_alias_resolves_to_cancel(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("remove")
        assert skill is not None
        assert skill.name == "cancel"

    def test_clear_alias_resolves_to_cancel(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("clear")
        assert skill is not None
        assert skill.name == "cancel"


class TestCancelCommandRouting:
    """Test that /cancel routes to the cancel skill via awake."""

    @patch("app.command_handlers.send_telegram")
    def test_cancel_routes_via_skill(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- test mission\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/cancel")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "1." in output
        assert "test mission" in output

    @patch("app.command_handlers.send_telegram")
    def test_cancel_with_number_routes(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- task A\n- task B\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/cancel 1")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "task A" in output
        assert "cancelled" in output.lower()

    @patch("app.command_handlers.send_telegram")
    def test_cancel_with_keyword_routes(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- fix auth bug\n- add dark mode\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/cancel dark")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "dark mode" in output

    @patch("app.command_handlers.send_telegram")
    def test_remove_alias_routes_to_cancel(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- task A\n- task B\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/remove")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "1." in output
        assert "task A" in output

    @patch("app.command_handlers.send_telegram")
    def test_remove_alias_cancels_by_number(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- task A\n- task B\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/remove 1")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "task A" in output
        assert "cancelled" in output.lower()

    @patch("app.command_handlers.send_telegram")
    def test_clear_alias_routes_to_cancel(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- task X\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/clear")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "1." in output
        assert "task X" in output

    @patch("app.command_handlers.send_telegram")
    def test_clear_alias_cancels_by_keyword(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- fix auth\n- add dark mode\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path), \
             patch("app.command_handlers.MISSIONS_FILE", missions_file):
            handle_command("/clear auth")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "auth" in output.lower()
        assert "cancelled" in output.lower()

    @patch("app.command_handlers.send_telegram")
    def test_cancel_appears_in_help(self, mock_send, tmp_path):
        """Verify /cancel is included in /help missions group output."""
        from app.command_handlers import _handle_help_detail

        _handle_help_detail("missions")
        mock_send.assert_called_once()
        help_text = mock_send.call_args[0][0]
        assert "/cancel" in help_text
