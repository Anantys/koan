"""Tests for the /update command (hardcoded in command_handlers, writes CYCLE_FILE)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.signals import CYCLE_FILE


class TestUpdateCommand:
    """Tests for /update via hardcoded command handler."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.atomic_write")
    def test_update_writes_cycle_file(self, mock_write, mock_send):
        from app.command_handlers import handle_command
        handle_command("/update")
        # Should write the cycle file
        args = mock_write.call_args[0]
        assert str(args[0]).endswith(CYCLE_FILE)
        assert args[1] == "CYCLE"

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.atomic_write")
    def test_update_sends_confirmation(self, mock_write, mock_send):
        from app.command_handlers import handle_command
        handle_command("/update")
        mock_send.assert_called_once()
        assert "update" in mock_send.call_args[0][0].lower() or "Update" in mock_send.call_args[0][0]

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.atomic_write")
    def test_upgrade_alias_works(self, mock_write, mock_send):
        from app.command_handlers import handle_command
        handle_command("/upgrade")
        args = mock_write.call_args[0]
        assert str(args[0]).endswith(CYCLE_FILE)

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.atomic_write")
    def test_update_does_not_dispatch_skill(self, mock_write, mock_send):
        """Update is hardcoded, not dispatched via skill system."""
        from app.command_handlers import handle_command
        with patch("app.command_handlers._dispatch_skill") as mock_dispatch:
            handle_command("/update")
        mock_dispatch.assert_not_called()


class TestSkillRegistration:
    """Tests that /restart is a standalone skill separate from /update."""

    def test_restart_skill_exists(self):
        skill_md = Path(__file__).parent.parent / "skills" / "core" / "restart" / "SKILL.md"
        assert skill_md.exists()

    def test_restart_handler_exists(self):
        handler = Path(__file__).parent.parent / "skills" / "core" / "restart" / "handler.py"
        assert handler.exists()

    def test_restart_skill_discoverable(self):
        from app.skills import build_registry
        registry = build_registry()
        restart_skill = registry.find_by_command("restart")
        assert restart_skill is not None
        assert restart_skill.name == "restart"

    def test_update_is_not_a_skill(self):
        """Update is hardcoded, not a skill — registry should NOT find it."""
        from app.skills import build_registry
        registry = build_registry()
        assert registry.find_by_command("update") is None
