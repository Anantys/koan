"""Tests for the /abort core skill -- abort current in-progress mission."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


class TestAbortHandler:
    """Test the abort skill handler directly."""

    def _make_ctx(self, tmp_path, args=""):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance_dir,
            command_name="abort",
            args=args,
        )

    def test_creates_abort_signal_file(self, tmp_path):
        from skills.core.abort.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        abort_file = tmp_path / ".koan-abort"
        assert abort_file.exists()
        assert "abort" in abort_file.read_text().lower()
        assert "Abort requested" in result

    def test_response_mentions_failed(self, tmp_path):
        from skills.core.abort.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "Failed" in result

    def test_overwrites_existing_abort_file(self, tmp_path):
        from skills.core.abort.handler import handle

        abort_file = tmp_path / ".koan-abort"
        abort_file.write_text("old")
        ctx = self._make_ctx(tmp_path)
        handle(ctx)
        assert abort_file.exists()


class TestAbortSignalConstant:
    """Test that ABORT_FILE is properly defined in signals."""

    def test_abort_file_constant_exists(self):
        from app.signals import ABORT_FILE

        assert ABORT_FILE == ".koan-abort"


class TestAbortSkillRegistry:
    """Test that /abort is discoverable in the skill registry."""

    def test_abort_resolves_in_registry(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("abort")
        assert skill is not None
        assert skill.name == "abort"

    def test_abort_has_missions_group(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("abort")
        assert skill is not None
        assert skill.group == "missions"


class TestAbortCommandRouting:
    """Test that /abort routes correctly via awake command handling."""

    @patch("app.command_handlers.send_telegram")
    def test_abort_routes_via_skill(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path / "instance"):
            (tmp_path / "instance").mkdir(exist_ok=True)
            handle_command("/abort")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "Abort requested" in output

    @patch("app.command_handlers.send_telegram")
    def test_abort_appears_in_help_missions(self, mock_send, tmp_path):
        """Verify /abort is included in /help missions group output."""
        from app.command_handlers import _handle_help_detail

        _handle_help_detail("missions")
        mock_send.assert_called_once()
        help_text = mock_send.call_args[0][0]
        assert "/abort" in help_text
