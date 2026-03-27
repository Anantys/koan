"""Tests for app.bridge_state — shared module-level state for the messaging bridge."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestMigrateHistoryFile:
    """Tests for _migrate_history_file()."""

    def test_migrates_old_to_new(self, tmp_path, monkeypatch):
        """When old file exists and new doesn't, rename old -> new."""
        old = tmp_path / "telegram-history.jsonl"
        new = tmp_path / "conversation-history.jsonl"
        old.write_text('{"msg": "hello"}\n')

        monkeypatch.setattr("app.bridge_state.INSTANCE_DIR", tmp_path)

        from app.bridge_state import _migrate_history_file
        result = _migrate_history_file()

        assert result == new
        assert new.exists()
        assert not old.exists()
        assert new.read_text() == '{"msg": "hello"}\n'

    def test_skips_when_new_exists(self, tmp_path, monkeypatch):
        """When new file already exists, don't migrate (idempotent)."""
        old = tmp_path / "telegram-history.jsonl"
        new = tmp_path / "conversation-history.jsonl"
        old.write_text("old content")
        new.write_text("new content")

        monkeypatch.setattr("app.bridge_state.INSTANCE_DIR", tmp_path)

        from app.bridge_state import _migrate_history_file
        result = _migrate_history_file()

        assert result == new
        assert old.exists()  # Old file left untouched
        assert new.read_text() == "new content"

    def test_returns_new_path_when_no_old(self, tmp_path, monkeypatch):
        """When neither file exists, returns the new path."""
        monkeypatch.setattr("app.bridge_state.INSTANCE_DIR", tmp_path)

        from app.bridge_state import _migrate_history_file
        result = _migrate_history_file()

        assert result == tmp_path / "conversation-history.jsonl"

    def test_returns_old_on_rename_failure(self, tmp_path, monkeypatch):
        """When rename fails, returns old path as fallback."""
        old = tmp_path / "telegram-history.jsonl"
        old.write_text("data")

        monkeypatch.setattr("app.bridge_state.INSTANCE_DIR", tmp_path)

        from app.bridge_state import _migrate_history_file

        # Make rename fail by patching the old Path object
        with patch.object(Path, "rename", side_effect=OSError("permission denied")):
            result = _migrate_history_file()

        assert result == old


class TestResolveDefaultProjectPath:
    """Tests for _resolve_default_project_path()."""

    @patch("app.utils.get_known_projects", return_value=[("proj1", "/path/to/proj1")])
    def test_returns_first_project_path(self, mock_projects):
        """Returns the path of the first known project."""
        from app.bridge_state import _resolve_default_project_path
        assert _resolve_default_project_path() == "/path/to/proj1"

    @patch("app.utils.get_known_projects", return_value=[])
    def test_returns_empty_when_no_projects(self, mock_projects):
        """Returns empty string when no projects are configured."""
        from app.bridge_state import _resolve_default_project_path
        assert _resolve_default_project_path() == ""

    @patch("app.utils.get_known_projects", side_effect=Exception("config broken"))
    def test_returns_empty_on_error(self, mock_projects):
        """Returns empty string on any exception (defensive)."""
        from app.bridge_state import _resolve_default_project_path
        assert _resolve_default_project_path() == ""

    @patch("app.utils.get_known_projects", return_value=[
        ("alpha", "/a"), ("beta", "/b"), ("gamma", "/c"),
    ])
    def test_returns_first_of_multiple(self, mock_projects):
        """With multiple projects, returns only the first."""
        from app.bridge_state import _resolve_default_project_path
        assert _resolve_default_project_path() == "/a"


class TestSkillRegistry:
    """Tests for _get_registry() and _reset_registry()."""

    def test_reset_clears_registry(self):
        """_reset_registry() sets the singleton to None."""
        import app.bridge_state as bs
        bs._skill_registry = "something"
        bs._reset_registry()
        assert bs._skill_registry is None

    @patch("app.bridge_state.build_registry")
    def test_get_registry_creates_on_first_call(self, mock_build, tmp_path, monkeypatch):
        """_get_registry() builds a registry on first access."""
        import app.bridge_state as bs
        bs._reset_registry()

        # Ensure INSTANCE_DIR/skills doesn't exist (no extra dirs)
        monkeypatch.setattr(bs, "INSTANCE_DIR", tmp_path)

        mock_registry = MagicMock()
        mock_build.return_value = mock_registry

        result = bs._get_registry()
        assert result is mock_registry
        mock_build.assert_called_once_with([])

        # Cleanup
        bs._reset_registry()

    @patch("app.bridge_state.build_registry")
    def test_get_registry_caches(self, mock_build, tmp_path, monkeypatch):
        """_get_registry() returns cached instance on second call."""
        import app.bridge_state as bs
        bs._reset_registry()
        monkeypatch.setattr(bs, "INSTANCE_DIR", tmp_path)

        mock_registry = MagicMock()
        mock_build.return_value = mock_registry

        result1 = bs._get_registry()
        result2 = bs._get_registry()

        assert result1 is result2
        assert mock_build.call_count == 1  # Only built once

        bs._reset_registry()

    @patch("app.bridge_state.build_registry")
    def test_get_registry_with_instance_skills(self, mock_build, tmp_path, monkeypatch):
        """_get_registry() includes instance/skills/ when present."""
        import app.bridge_state as bs
        bs._reset_registry()

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        monkeypatch.setattr(bs, "INSTANCE_DIR", tmp_path)

        mock_registry = MagicMock()
        mock_build.return_value = mock_registry

        bs._get_registry()

        # Should pass the instance skills dir as extra
        mock_build.assert_called_once()
        call_args = mock_build.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0] == skills_dir

        bs._reset_registry()

    @patch("app.bridge_state.build_registry")
    def test_get_registry_invalidates_on_mtime_change(self, mock_build, tmp_path, monkeypatch):
        """_get_registry() rebuilds when skills directory mtime changes."""
        import app.bridge_state as bs
        bs._reset_registry()
        monkeypatch.setattr(bs, "INSTANCE_DIR", tmp_path)

        mock_registry_1 = MagicMock()
        mock_registry_2 = MagicMock()
        mock_build.side_effect = [mock_registry_1, mock_registry_2]

        # First call builds the registry
        result1 = bs._get_registry()
        assert result1 is mock_registry_1
        assert mock_build.call_count == 1

        # Second call returns cached (same mtime)
        result2 = bs._get_registry()
        assert result2 is mock_registry_1
        assert mock_build.call_count == 1

        # Simulate skills directory change by bumping the stored mtime
        bs._skill_registry_mtime -= 1.0

        # Third call detects mtime change and rebuilds
        result3 = bs._get_registry()
        assert result3 is mock_registry_2
        assert mock_build.call_count == 2

        bs._reset_registry()


class TestModuleLevelConstants:
    """Tests for module-level constant derivation."""

    def test_koan_root_is_path(self):
        """KOAN_ROOT should be a Path object."""
        from app.bridge_state import KOAN_ROOT
        assert isinstance(KOAN_ROOT, Path)

    def test_instance_dir_under_koan_root(self):
        """INSTANCE_DIR should be KOAN_ROOT / 'instance'."""
        from app.bridge_state import KOAN_ROOT, INSTANCE_DIR
        assert INSTANCE_DIR == KOAN_ROOT / "instance"

    def test_missions_file_path(self):
        """MISSIONS_FILE should be under INSTANCE_DIR."""
        from app.bridge_state import INSTANCE_DIR, MISSIONS_FILE
        assert MISSIONS_FILE == INSTANCE_DIR / "missions.md"

    def test_outbox_file_path(self):
        """OUTBOX_FILE should be under INSTANCE_DIR."""
        from app.bridge_state import INSTANCE_DIR, OUTBOX_FILE
        assert OUTBOX_FILE == INSTANCE_DIR / "outbox.md"

    def test_poll_interval_is_int(self):
        """POLL_INTERVAL should be an integer."""
        from app.bridge_state import POLL_INTERVAL
        assert isinstance(POLL_INTERVAL, int)

    def test_chat_timeout_is_int(self):
        """CHAT_TIMEOUT should be an integer."""
        from app.bridge_state import CHAT_TIMEOUT
        assert isinstance(CHAT_TIMEOUT, int)

    def test_topics_file_path(self):
        """TOPICS_FILE should be a known filename."""
        from app.bridge_state import INSTANCE_DIR, TOPICS_FILE
        assert TOPICS_FILE == INSTANCE_DIR / "previous-discussions-topics.json"
