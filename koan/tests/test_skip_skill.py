"""Tests for the /skip core skill -- abort current in-progress mission."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


class TestSkipHandler:
    """Test the skip skill handler directly."""

    def _make_ctx(self, tmp_path, args=""):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)
        return SkillContext(
            koan_root=tmp_path,
            instance_dir=instance_dir,
            command_name="skip",
            args=args,
        )

    def test_creates_skip_signal_file(self, tmp_path):
        from skills.core.skip.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        skip_file = tmp_path / ".koan-skip"
        assert skip_file.exists()
        assert "skip" in skip_file.read_text().lower()
        assert "Skip requested" in result

    def test_response_mentions_failed(self, tmp_path):
        from skills.core.skip.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "Failed" in result

    def test_overwrites_existing_skip_file(self, tmp_path):
        from skills.core.skip.handler import handle

        skip_file = tmp_path / ".koan-skip"
        skip_file.write_text("old")
        ctx = self._make_ctx(tmp_path)
        handle(ctx)
        assert skip_file.exists()


class TestSkipSignalConstant:
    """Test that SKIP_FILE is properly defined in signals."""

    def test_skip_file_constant_exists(self):
        from app.signals import SKIP_FILE

        assert SKIP_FILE == ".koan-skip"


class TestSkipSkillRegistry:
    """Test that /skip is discoverable in the skill registry."""

    def test_skip_resolves_in_registry(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("skip")
        assert skill is not None
        assert skill.name == "skip"

    def test_skip_has_missions_group(self):
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("skip")
        assert skill is not None
        assert skill.group == "missions"


class TestSkipCommandRouting:
    """Test that /skip routes correctly via awake command handling."""

    @patch("app.command_handlers.send_telegram")
    def test_skip_routes_via_skill(self, mock_send, tmp_path):
        from app.command_handlers import handle_command

        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", tmp_path / "instance"):
            (tmp_path / "instance").mkdir(exist_ok=True)
            handle_command("/skip")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "Skip requested" in output

    @patch("app.command_handlers.send_telegram")
    def test_skip_appears_in_help_missions(self, mock_send, tmp_path):
        """Verify /skip is included in /help missions group output."""
        from app.command_handlers import _handle_help_detail

        _handle_help_detail("missions")
        mock_send.assert_called_once()
        help_text = mock_send.call_args[0][0]
        assert "/skip" in help_text
