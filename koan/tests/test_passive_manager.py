"""Tests for passive_manager.py — passive mode state management."""

import json
import os
import time

import pytest


class TestPassiveState:
    """Test PassiveState dataclass."""

    def test_expires_at_with_duration(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.expires_at == 4600

    def test_expires_at_indefinite(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=0, reason="manual")
        assert state.expires_at is None

    def test_is_expired_before_expiry(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.is_expired(now=2000) is False

    def test_is_expired_after_expiry(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.is_expired(now=5000) is True

    def test_is_expired_at_exact_boundary(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.is_expired(now=4600) is True

    def test_is_expired_indefinite_never_expires(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=0, reason="manual")
        assert state.is_expired(now=999999999) is False

    def test_remaining_seconds_with_duration(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.remaining_seconds(now=2000) == 2600

    def test_remaining_seconds_when_expired(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.remaining_seconds(now=5000) == 0

    def test_remaining_seconds_indefinite(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=0, reason="manual")
        assert state.remaining_seconds(now=999999) == -1

    def test_remaining_display_hours_and_minutes(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=18000, reason="manual")
        assert state.remaining_display(now=1000) == "5h00m"

    def test_remaining_display_minutes_only(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.remaining_display(now=3100) == "25m"

    def test_remaining_display_expired(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=3600, reason="manual")
        assert state.remaining_display(now=5000) == "expired"

    def test_remaining_display_indefinite(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=1000, duration=0, reason="manual")
        assert state.remaining_display(now=999999) == "indefinite"

    def test_remaining_display_mixed(self):
        from app.passive_manager import PassiveState

        state = PassiveState(activated_at=0, duration=9000, reason="manual")
        assert state.remaining_display(now=0) == "2h30m"


class TestIsPassive:
    """Test is_passive convenience function."""

    def test_not_passive_when_no_file(self, tmp_path):
        from app.passive_manager import is_passive

        assert is_passive(str(tmp_path)) is False

    def test_passive_when_active(self, tmp_path):
        from app.passive_manager import create_passive, is_passive

        create_passive(str(tmp_path))
        assert is_passive(str(tmp_path)) is True

    def test_not_passive_when_expired(self, tmp_path):
        from app.passive_manager import is_passive

        now = int(time.time())
        data = {"activated_at": now - 7200, "duration": 3600, "reason": "manual"}
        (tmp_path / ".koan-passive").write_text(json.dumps(data))
        assert is_passive(str(tmp_path)) is False


class TestGetPassiveState:
    """Test get_passive_state function."""

    def test_returns_none_when_no_file(self, tmp_path):
        from app.passive_manager import get_passive_state

        assert get_passive_state(str(tmp_path)) is None

    def test_reads_passive_state(self, tmp_path):
        from app.passive_manager import get_passive_state

        data = {"activated_at": 1000, "duration": 3600, "reason": "manual"}
        (tmp_path / ".koan-passive").write_text(json.dumps(data))

        state = get_passive_state(str(tmp_path))
        assert state is not None
        assert state.activated_at == 1000
        assert state.duration == 3600
        assert state.reason == "manual"

    def test_reads_indefinite_state(self, tmp_path):
        from app.passive_manager import get_passive_state

        data = {"activated_at": 1000, "duration": 0, "reason": "start_passive"}
        (tmp_path / ".koan-passive").write_text(json.dumps(data))

        state = get_passive_state(str(tmp_path))
        assert state is not None
        assert state.duration == 0

    def test_returns_none_on_invalid_json(self, tmp_path):
        from app.passive_manager import get_passive_state

        (tmp_path / ".koan-passive").write_text("not json")
        assert get_passive_state(str(tmp_path)) is None

    def test_returns_none_on_empty_file(self, tmp_path):
        from app.passive_manager import get_passive_state

        (tmp_path / ".koan-passive").write_text("")
        assert get_passive_state(str(tmp_path)) is None

    def test_defaults_missing_fields(self, tmp_path):
        from app.passive_manager import get_passive_state

        data = {"activated_at": 5000}
        (tmp_path / ".koan-passive").write_text(json.dumps(data))

        state = get_passive_state(str(tmp_path))
        assert state is not None
        assert state.duration == 0  # default indefinite
        assert state.reason == ""


class TestCreatePassive:
    """Test create_passive function."""

    def test_creates_passive_file(self, tmp_path):
        from app.passive_manager import create_passive

        state = create_passive(str(tmp_path), duration=7200, reason="testing")
        assert (tmp_path / ".koan-passive").exists()
        assert state.duration == 7200
        assert state.reason == "testing"

    def test_creates_indefinite_by_default(self, tmp_path):
        from app.passive_manager import create_passive

        state = create_passive(str(tmp_path))
        assert state.duration == 0
        assert state.reason == "manual"

    def test_file_contains_valid_json(self, tmp_path):
        from app.passive_manager import create_passive

        create_passive(str(tmp_path), duration=3600)
        data = json.loads((tmp_path / ".koan-passive").read_text())
        assert "activated_at" in data
        assert data["duration"] == 3600
        assert data["reason"] == "manual"

    def test_overwrites_existing_passive(self, tmp_path):
        from app.passive_manager import create_passive, get_passive_state

        create_passive(str(tmp_path), duration=3600, reason="first")
        create_passive(str(tmp_path), duration=7200, reason="second")
        state = get_passive_state(str(tmp_path))
        assert state.duration == 7200
        assert state.reason == "second"


class TestRemovePassive:
    """Test remove_passive function."""

    def test_removes_passive_file(self, tmp_path):
        from app.passive_manager import create_passive, remove_passive

        create_passive(str(tmp_path))
        remove_passive(str(tmp_path))
        assert not (tmp_path / ".koan-passive").exists()

    def test_noop_when_no_file(self, tmp_path):
        from app.passive_manager import remove_passive

        remove_passive(str(tmp_path))  # Should not raise


class TestCheckPassive:
    """Test check_passive function with auto-cleanup."""

    def test_returns_state_when_active(self, tmp_path):
        from app.passive_manager import check_passive, create_passive

        create_passive(str(tmp_path), duration=3600)
        state = check_passive(str(tmp_path))
        assert state is not None
        assert state.duration == 3600

    def test_returns_state_when_indefinite(self, tmp_path):
        from app.passive_manager import check_passive, create_passive

        create_passive(str(tmp_path))  # indefinite
        state = check_passive(str(tmp_path))
        assert state is not None
        assert state.duration == 0

    def test_returns_none_when_no_file(self, tmp_path):
        from app.passive_manager import check_passive

        assert check_passive(str(tmp_path)) is None

    def test_returns_none_and_cleans_up_when_expired(self, tmp_path):
        from app.passive_manager import check_passive

        now = int(time.time())
        data = {"activated_at": now - 7200, "duration": 3600, "reason": "manual"}
        passive_file = tmp_path / ".koan-passive"
        passive_file.write_text(json.dumps(data))

        result = check_passive(str(tmp_path))
        assert result is None
        assert not passive_file.exists()

    def test_indefinite_never_auto_cleans(self, tmp_path):
        from app.passive_manager import check_passive

        data = {"activated_at": 1, "duration": 0, "reason": "start_passive"}
        passive_file = tmp_path / ".koan-passive"
        passive_file.write_text(json.dumps(data))

        result = check_passive(str(tmp_path))
        assert result is not None
        assert passive_file.exists()


class TestPassiveSkillHandler:
    """Test the /passive and /active skill handlers."""

    def _make_ctx(self, tmp_path, command_name="passive", args=""):
        class FakeCtx:
            pass

        ctx = FakeCtx()
        ctx.koan_root = tmp_path
        ctx.instance_dir = tmp_path / "instance"
        ctx.command_name = command_name
        ctx.args = args
        return ctx

    def test_passive_activates_indefinite(self, tmp_path):
        from skills.core.passive.handler import handle

        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "Passive mode ON" in result
        assert (tmp_path / ".koan-passive").exists()

    def test_passive_with_duration(self, tmp_path):
        from skills.core.passive.handler import handle

        ctx = self._make_ctx(tmp_path, args="4h")
        result = handle(ctx)
        assert "Passive mode ON" in result
        assert "4h00m" in result

    def test_passive_with_invalid_duration(self, tmp_path):
        from skills.core.passive.handler import handle

        ctx = self._make_ctx(tmp_path, args="xyz")
        result = handle(ctx)
        assert "Invalid duration" in result
        assert not (tmp_path / ".koan-passive").exists()

    def test_passive_already_passive_no_args(self, tmp_path):
        from app.passive_manager import create_passive
        from skills.core.passive.handler import handle

        create_passive(str(tmp_path))
        ctx = self._make_ctx(tmp_path)
        result = handle(ctx)
        assert "Already" in result

    def test_passive_already_passive_with_duration_overrides(self, tmp_path):
        from app.passive_manager import create_passive, get_passive_state
        from skills.core.passive.handler import handle

        create_passive(str(tmp_path))
        ctx = self._make_ctx(tmp_path, args="2h")
        result = handle(ctx)
        assert "Passive mode ON" in result
        state = get_passive_state(str(tmp_path))
        assert state.duration == 7200

    def test_active_deactivates(self, tmp_path):
        from app.passive_manager import create_passive
        from skills.core.passive.handler import handle

        create_passive(str(tmp_path))
        ctx = self._make_ctx(tmp_path, command_name="active")
        result = handle(ctx)
        assert "Back to work" in result
        assert not (tmp_path / ".koan-passive").exists()

    def test_active_when_not_passive(self, tmp_path):
        from skills.core.passive.handler import handle

        ctx = self._make_ctx(tmp_path, command_name="active")
        result = handle(ctx)
        assert "Already active" in result


class TestStatusHandlerPassiveIntegration:
    """Test passive mode in /status output."""

    def _make_ctx(self, tmp_path):
        class FakeCtx:
            pass

        ctx = FakeCtx()
        ctx.koan_root = tmp_path
        ctx.instance_dir = tmp_path / "instance"
        ctx.command_name = "status"
        ctx.args = ""
        os.makedirs(ctx.instance_dir, exist_ok=True)
        return ctx

    def test_status_shows_passive_when_active(self, tmp_path):
        from app.passive_manager import create_passive
        from skills.core.status.handler import _handle_status

        create_passive(str(tmp_path))
        ctx = self._make_ctx(tmp_path)
        result = _handle_status(ctx)
        assert "Passive" in result
        assert "read-only" in result

    def test_status_shows_active_when_not_passive(self, tmp_path):
        from skills.core.status.handler import _handle_status

        ctx = self._make_ctx(tmp_path)
        result = _handle_status(ctx)
        assert "Active" in result
        assert "Passive" not in result

    def test_status_shows_timed_passive(self, tmp_path):
        from app.passive_manager import create_passive
        from skills.core.status.handler import _handle_status

        create_passive(str(tmp_path), duration=7200)
        ctx = self._make_ctx(tmp_path)
        result = _handle_status(ctx)
        assert "Passive" in result
        assert "remaining" in result
