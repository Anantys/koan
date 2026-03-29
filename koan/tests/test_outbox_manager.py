"""Tests for outbox_manager — mission-aware formatting."""

from pathlib import Path
from unittest.mock import patch

from app.outbox_manager import OutboxManager


class TestMissionAwareFormatting:
    """Phase 1: outbox uses fallback_format when a mission is running."""

    def _make_mgr(self, instance_dir: Path) -> OutboxManager:
        return OutboxManager(
            outbox_file=instance_dir / "outbox.md",
            instance_dir=instance_dir,
            conversation_history_file=instance_dir / "conversation-history.jsonl",
        )

    def test_fallback_format_when_mission_active(self, instance_dir):
        """When .koan-status says 'executing mission', skip Claude formatting."""
        koan_root = instance_dir.parent
        status_file = koan_root / ".koan-status"
        status_file.write_text("Run 1/5 — executing mission on my-project")

        mgr = self._make_mgr(instance_dir)
        result = mgr._format_message("## Mission complete\n- Did things")

        # Should be fallback-formatted (no markdown headers)
        assert "##" not in result
        assert "Mission complete" in result

    def test_fallback_format_when_skill_dispatch_active(self, instance_dir):
        """When .koan-status says 'skill dispatch', skip Claude formatting."""
        koan_root = instance_dir.parent
        status_file = koan_root / ".koan-status"
        status_file.write_text("Run 2/5 — skill dispatch on my-project")

        mgr = self._make_mgr(instance_dir)
        result = mgr._format_message("## Status update\nAll good")

        assert "##" not in result
        assert "Status update" in result

    @patch("app.outbox_manager.format_message", return_value="Formatted by Claude")
    def test_claude_format_when_no_mission(self, mock_fmt, instance_dir):
        """When no mission is active, use full Claude formatting."""
        # No .koan-status file → no mission active
        mgr = self._make_mgr(instance_dir)
        result = mgr._format_message("raw content")

        assert result == "Formatted by Claude"
        mock_fmt.assert_called_once()

    @patch("app.outbox_manager.format_message", return_value="Formatted by Claude")
    def test_claude_format_when_idle(self, mock_fmt, instance_dir):
        """When status is 'Idle', use full Claude formatting."""
        koan_root = instance_dir.parent
        status_file = koan_root / ".koan-status"
        status_file.write_text("Idle — sleeping 60s")

        mgr = self._make_mgr(instance_dir)
        result = mgr._format_message("raw content")

        assert result == "Formatted by Claude"
        mock_fmt.assert_called_once()

    def test_is_mission_active_handles_missing_file(self, instance_dir):
        """No crash when .koan-status doesn't exist."""
        mgr = self._make_mgr(instance_dir)
        assert mgr._is_mission_active() is False
