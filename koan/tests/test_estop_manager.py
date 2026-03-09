"""Tests for estop_manager.py — e-stop state management."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.estop_manager import (
    ESTOP_SIGNAL_FILE,
    ESTOP_STATE_FILE,
    READONLY_TOOLS,
    EstopLevel,
    EstopState,
    activate_estop,
    deactivate_estop,
    get_estop_state,
    get_estop_tools,
    is_estopped,
    is_project_frozen,
    unfreeze_project,
)


# ---------------------------------------------------------------------------
# EstopLevel enum
# ---------------------------------------------------------------------------

class TestEstopLevel:
    def test_values(self):
        assert EstopLevel.FULL.value == "full"
        assert EstopLevel.READONLY.value == "readonly"
        assert EstopLevel.PROJECT_FREEZE.value == "project_freeze"

    def test_from_string(self):
        assert EstopLevel("full") == EstopLevel.FULL
        assert EstopLevel("readonly") == EstopLevel.READONLY
        assert EstopLevel("project_freeze") == EstopLevel.PROJECT_FREEZE

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            EstopLevel("invalid")


# ---------------------------------------------------------------------------
# EstopState dataclass
# ---------------------------------------------------------------------------

class TestEstopState:
    def test_to_dict(self):
        state = EstopState(
            level=EstopLevel.FULL,
            reason="test",
            timestamp=12345,
            frozen_projects=["proj1"],
            triggered_by="telegram",
        )
        d = state.to_dict()
        assert d["level"] == "full"
        assert d["reason"] == "test"
        assert d["timestamp"] == 12345
        assert d["frozen_projects"] == ["proj1"]
        assert d["triggered_by"] == "telegram"

    def test_from_dict(self):
        data = {
            "level": "readonly",
            "reason": "safety check",
            "timestamp": 99999,
            "frozen_projects": [],
            "triggered_by": "api",
        }
        state = EstopState.from_dict(data)
        assert state.level == EstopLevel.READONLY
        assert state.reason == "safety check"
        assert state.timestamp == 99999
        assert state.triggered_by == "api"

    def test_from_dict_defaults(self):
        state = EstopState.from_dict({})
        assert state.level == EstopLevel.FULL
        assert state.reason == ""
        assert state.timestamp == 0
        assert state.frozen_projects == []
        assert state.triggered_by == "telegram"

    def test_from_dict_invalid_level_defaults_to_full(self):
        state = EstopState.from_dict({"level": "bogus"})
        assert state.level == EstopLevel.FULL

    def test_roundtrip(self):
        original = EstopState(
            level=EstopLevel.PROJECT_FREEZE,
            reason="freeze proj",
            timestamp=55555,
            frozen_projects=["a", "b"],
            triggered_by="cli",
        )
        restored = EstopState.from_dict(original.to_dict())
        assert restored.level == original.level
        assert restored.reason == original.reason
        assert restored.timestamp == original.timestamp
        assert restored.frozen_projects == original.frozen_projects
        assert restored.triggered_by == original.triggered_by


# ---------------------------------------------------------------------------
# is_estopped
# ---------------------------------------------------------------------------

class TestIsEstopped:
    def test_no_file(self, tmp_path):
        assert is_estopped(str(tmp_path)) is False

    def test_file_exists(self, tmp_path):
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        assert is_estopped(str(tmp_path)) is True

    def test_directory_not_file(self, tmp_path):
        (tmp_path / ESTOP_SIGNAL_FILE).mkdir()
        assert is_estopped(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# get_estop_state
# ---------------------------------------------------------------------------

class TestGetEstopState:
    def test_not_estopped(self, tmp_path):
        assert get_estop_state(str(tmp_path)) is None

    def test_signal_only_no_state_file(self, tmp_path):
        """Signal file without state file → fail-safe to FULL."""
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        state = get_estop_state(str(tmp_path))
        assert state is not None
        assert state.level == EstopLevel.FULL
        assert "missing state file" in state.reason

    def test_corrupt_state_file(self, tmp_path):
        """Corrupt JSON → fail-safe to FULL."""
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        (tmp_path / ESTOP_STATE_FILE).write_text("not json {{{")
        state = get_estop_state(str(tmp_path))
        assert state is not None
        assert state.level == EstopLevel.FULL
        assert "corrupt state file" in state.reason

    def test_valid_state(self, tmp_path):
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        data = {
            "level": "readonly",
            "reason": "testing",
            "timestamp": 100,
            "frozen_projects": [],
            "triggered_by": "telegram",
        }
        (tmp_path / ESTOP_STATE_FILE).write_text(json.dumps(data))
        state = get_estop_state(str(tmp_path))
        assert state.level == EstopLevel.READONLY
        assert state.reason == "testing"
        assert state.timestamp == 100

    def test_valid_state_project_freeze(self, tmp_path):
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        data = {
            "level": "project_freeze",
            "reason": "freeze test",
            "timestamp": 200,
            "frozen_projects": ["proj1", "proj2"],
            "triggered_by": "telegram",
        }
        (tmp_path / ESTOP_STATE_FILE).write_text(json.dumps(data))
        state = get_estop_state(str(tmp_path))
        assert state.level == EstopLevel.PROJECT_FREEZE
        assert state.frozen_projects == ["proj1", "proj2"]


# ---------------------------------------------------------------------------
# activate_estop
# ---------------------------------------------------------------------------

class TestActivateEstop:
    def test_full_creates_both_files(self, tmp_path):
        state = activate_estop(str(tmp_path), EstopLevel.FULL, reason="emergency")
        assert (tmp_path / ESTOP_SIGNAL_FILE).exists()
        assert (tmp_path / ESTOP_STATE_FILE).exists()
        assert state.level == EstopLevel.FULL
        assert state.reason == "emergency"

    def test_readonly(self, tmp_path):
        state = activate_estop(str(tmp_path), EstopLevel.READONLY, reason="safety check")
        assert state.level == EstopLevel.READONLY
        data = json.loads((tmp_path / ESTOP_STATE_FILE).read_text())
        assert data["level"] == "readonly"

    def test_project_freeze(self, tmp_path):
        state = activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            reason="freeze", frozen_projects=["proj1"],
        )
        assert state.level == EstopLevel.PROJECT_FREEZE
        assert state.frozen_projects == ["proj1"]

    def test_project_freeze_merges(self, tmp_path):
        """Adding a second frozen project merges with existing list."""
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            reason="freeze", frozen_projects=["proj1"],
        )
        state = activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            reason="freeze more", frozen_projects=["proj2"],
        )
        assert sorted(state.frozen_projects) == ["proj1", "proj2"]

    def test_project_freeze_no_duplicates(self, tmp_path):
        """Re-freezing the same project doesn't duplicate it."""
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            reason="freeze", frozen_projects=["proj1"],
        )
        state = activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            reason="freeze again", frozen_projects=["proj1"],
        )
        assert state.frozen_projects == ["proj1"]

    def test_timestamp_auto_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.estop_manager.time.time", lambda: 9999)
        state = activate_estop(str(tmp_path), EstopLevel.FULL)
        assert state.timestamp == 9999

    def test_triggered_by(self, tmp_path):
        state = activate_estop(
            str(tmp_path), EstopLevel.FULL, triggered_by="cli",
        )
        assert state.triggered_by == "cli"

    def test_overwrite_different_level(self, tmp_path):
        """Activating a different level overwrites the previous one."""
        activate_estop(str(tmp_path), EstopLevel.READONLY, reason="first")
        state = activate_estop(str(tmp_path), EstopLevel.FULL, reason="escalate")
        assert state.level == EstopLevel.FULL
        assert state.reason == "escalate"

    def test_uses_atomic_write(self, tmp_path):
        # atomic_write is imported lazily inside activate_estop,
        # so patch at the source module (app.utils.atomic_write)
        from app.utils import atomic_write
        with patch("app.utils.atomic_write", side_effect=atomic_write) as mock_aw:
            activate_estop(str(tmp_path), EstopLevel.FULL)
            assert mock_aw.call_count == 2


# ---------------------------------------------------------------------------
# deactivate_estop
# ---------------------------------------------------------------------------

class TestDeactivateEstop:
    def test_removes_both_files(self, tmp_path):
        activate_estop(str(tmp_path), EstopLevel.FULL)
        assert is_estopped(str(tmp_path))
        deactivate_estop(str(tmp_path))
        assert not is_estopped(str(tmp_path))
        assert not (tmp_path / ESTOP_STATE_FILE).exists()

    def test_no_files_is_noop(self, tmp_path):
        # Should not raise
        deactivate_estop(str(tmp_path))

    def test_partial_files(self, tmp_path):
        """Only signal file exists — still cleans up."""
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        deactivate_estop(str(tmp_path))
        assert not (tmp_path / ESTOP_SIGNAL_FILE).exists()


# ---------------------------------------------------------------------------
# unfreeze_project
# ---------------------------------------------------------------------------

class TestUnfreezeProject:
    def test_not_estopped(self, tmp_path):
        assert unfreeze_project(str(tmp_path), "proj1") is None

    def test_not_project_freeze(self, tmp_path):
        """Unfreezing on a FULL estop does nothing."""
        activate_estop(str(tmp_path), EstopLevel.FULL)
        assert unfreeze_project(str(tmp_path), "proj1") is None

    def test_unfreeze_one_of_many(self, tmp_path):
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            frozen_projects=["proj1", "proj2", "proj3"],
        )
        state = unfreeze_project(str(tmp_path), "proj2")
        assert state is not None
        assert state.frozen_projects == ["proj1", "proj3"]
        assert is_estopped(str(tmp_path))

    def test_unfreeze_last_deactivates(self, tmp_path):
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            frozen_projects=["proj1"],
        )
        state = unfreeze_project(str(tmp_path), "proj1")
        assert state is None
        assert not is_estopped(str(tmp_path))

    def test_unfreeze_nonexistent_project(self, tmp_path):
        """Unfreezing a project not in the list keeps estop active."""
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            frozen_projects=["proj1"],
        )
        state = unfreeze_project(str(tmp_path), "proj_unknown")
        # proj1 still frozen, estop stays
        assert state is not None
        assert state.frozen_projects == ["proj1"]


# ---------------------------------------------------------------------------
# is_project_frozen
# ---------------------------------------------------------------------------

class TestIsProjectFrozen:
    def test_not_estopped(self, tmp_path):
        assert is_project_frozen(str(tmp_path), "proj1") is False

    def test_full_level(self, tmp_path):
        activate_estop(str(tmp_path), EstopLevel.FULL)
        assert is_project_frozen(str(tmp_path), "proj1") is False

    def test_frozen_project(self, tmp_path):
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            frozen_projects=["proj1", "proj2"],
        )
        assert is_project_frozen(str(tmp_path), "proj1") is True
        assert is_project_frozen(str(tmp_path), "proj2") is True
        assert is_project_frozen(str(tmp_path), "proj3") is False


# ---------------------------------------------------------------------------
# get_estop_tools
# ---------------------------------------------------------------------------

class TestGetEstopTools:
    def test_returns_readonly_set(self):
        tools = get_estop_tools()
        assert tools == ["Read", "Glob", "Grep"]

    def test_returns_new_list(self):
        """Each call returns a fresh copy."""
        tools1 = get_estop_tools()
        tools2 = get_estop_tools()
        assert tools1 is not tools2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_state_persists_across_reads(self, tmp_path):
        """State written by activate can be read back correctly."""
        activate_estop(
            str(tmp_path), EstopLevel.PROJECT_FREEZE,
            reason="test persistence",
            frozen_projects=["a", "b"],
            triggered_by="cli",
        )
        state = get_estop_state(str(tmp_path))
        assert state.level == EstopLevel.PROJECT_FREEZE
        assert state.reason == "test persistence"
        assert state.frozen_projects == ["a", "b"]
        assert state.triggered_by == "cli"

    def test_empty_json_state_file(self, tmp_path):
        """Empty state file → fail-safe to FULL."""
        (tmp_path / ESTOP_SIGNAL_FILE).touch()
        (tmp_path / ESTOP_STATE_FILE).write_text("")
        state = get_estop_state(str(tmp_path))
        assert state.level == EstopLevel.FULL

    def test_activate_deactivate_activate(self, tmp_path):
        """Full lifecycle: activate → deactivate → re-activate."""
        activate_estop(str(tmp_path), EstopLevel.FULL, reason="first")
        deactivate_estop(str(tmp_path))
        assert not is_estopped(str(tmp_path))
        activate_estop(str(tmp_path), EstopLevel.READONLY, reason="second")
        state = get_estop_state(str(tmp_path))
        assert state.level == EstopLevel.READONLY
        assert state.reason == "second"

    def test_frozen_projects_default_empty(self):
        state = EstopState(level=EstopLevel.FULL, reason="", timestamp=0)
        assert state.frozen_projects == []
