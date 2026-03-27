"""Tests for auto_update.py — automatic update checker."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.auto_update import (
    _load_auto_update_config,
    is_auto_update_enabled,
    get_check_interval,
    check_for_updates,
    perform_auto_update,
    reset_check_cache,
)
from app.update_manager import UpdateResult


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the check cache before each test."""
    reset_check_cache()
    yield
    reset_check_cache()


def _patch_config(config_dict):
    """Patch load_config at its source module (lazy import target)."""
    return patch("app.utils.load_config", return_value=config_dict)


class TestLoadAutoUpdateConfig:
    """Tests for config loading with defaults."""

    def test_defaults_when_no_config(self):
        with _patch_config({}):
            config = _load_auto_update_config()
        assert config == {"enabled": False, "check_interval": 10, "notify": True}

    def test_enabled_from_config(self):
        with _patch_config({"auto_update": {"enabled": True, "check_interval": 5}}):
            config = _load_auto_update_config()
        assert config["enabled"] is True
        assert config["check_interval"] == 5

    def test_non_dict_section_treated_as_empty(self):
        with _patch_config({"auto_update": "invalid"}):
            config = _load_auto_update_config()
        assert config["enabled"] is False

    def test_config_load_failure_returns_defaults(self):
        with patch("app.utils.load_config", side_effect=Exception("boom")):
            config = _load_auto_update_config()
        assert config["enabled"] is False
        assert config["check_interval"] == 10

    def test_notify_defaults_to_true(self):
        with _patch_config({"auto_update": {"enabled": True}}):
            config = _load_auto_update_config()
        assert config["notify"] is True

    def test_notify_can_be_disabled(self):
        with _patch_config({"auto_update": {"enabled": True, "notify": False}}):
            config = _load_auto_update_config()
        assert config["notify"] is False


class TestIsAutoUpdateEnabled:
    """Tests for the enabled check."""

    def test_disabled_by_default(self):
        with _patch_config({}):
            assert is_auto_update_enabled() is False

    def test_enabled_when_configured(self):
        with _patch_config({"auto_update": {"enabled": True}}):
            assert is_auto_update_enabled() is True


class TestGetCheckInterval:
    """Tests for the interval getter."""

    def test_default_interval(self):
        with _patch_config({}):
            assert get_check_interval() == 10

    def test_custom_interval(self):
        with _patch_config({"auto_update": {"check_interval": 3}}):
            assert get_check_interval() == 3


class TestCheckForUpdates:
    """Tests for the lightweight update check."""

    def test_no_remote_returns_none(self):
        with patch("app.auto_update._find_upstream_remote", return_value=None):
            result = check_for_updates("/fake/root")
        assert result is None

    def test_fetch_failure_returns_none(self):
        mock_result = MagicMock(returncode=1, stderr="network error")
        with patch("app.auto_update._find_upstream_remote", return_value="upstream"), \
             patch("app.auto_update._run_git", return_value=mock_result):
            result = check_for_updates("/fake/root")
        assert result is None

    def test_returns_commit_count(self):
        def mock_git(args, cwd):
            if args[0] == "fetch":
                return MagicMock(returncode=0)
            if args[0] == "rev-list":
                return MagicMock(returncode=0, stdout="3\n")
            return MagicMock(returncode=1, stderr="")

        with patch("app.auto_update._find_upstream_remote", return_value="upstream"), \
             patch("app.auto_update._run_git", side_effect=mock_git):
            result = check_for_updates("/fake/root")
        assert result == 3

    def test_returns_zero_when_up_to_date(self):
        def mock_git(args, cwd):
            if args[0] == "fetch":
                return MagicMock(returncode=0)
            if args[0] == "rev-list":
                return MagicMock(returncode=0, stdout="0\n")
            return MagicMock(returncode=1, stderr="")

        with patch("app.auto_update._find_upstream_remote", return_value="upstream"), \
             patch("app.auto_update._run_git", side_effect=mock_git):
            result = check_for_updates("/fake/root")
        assert result == 0

    def test_cache_prevents_rapid_checks(self):
        """Second call within cache window returns 0 without git ops."""
        call_count = 0

        def mock_git(args, cwd):
            nonlocal call_count
            call_count += 1
            if args[0] == "fetch":
                return MagicMock(returncode=0)
            if args[0] == "rev-list":
                return MagicMock(returncode=0, stdout="5\n")
            return MagicMock(returncode=1, stderr="")

        with patch("app.auto_update._find_upstream_remote", return_value="upstream"), \
             patch("app.auto_update._run_git", side_effect=mock_git):
            first = check_for_updates("/fake/root")
            second = check_for_updates("/fake/root")

        assert first == 5
        assert second == 0  # cached, no git call
        assert call_count == 2  # only fetch + rev-list from first call

    def test_rev_list_failure_returns_none(self):
        def mock_git(args, cwd):
            if args[0] == "fetch":
                return MagicMock(returncode=0)
            return MagicMock(returncode=1, stderr="bad ref")

        with patch("app.auto_update._find_upstream_remote", return_value="upstream"), \
             patch("app.auto_update._run_git", side_effect=mock_git):
            result = check_for_updates("/fake/root")
        assert result is None

    def test_invalid_rev_list_output_returns_none(self):
        def mock_git(args, cwd):
            if args[0] == "fetch":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stdout="not-a-number\n")

        with patch("app.auto_update._find_upstream_remote", return_value="upstream"), \
             patch("app.auto_update._run_git", side_effect=mock_git):
            result = check_for_updates("/fake/root")
        assert result is None


class TestPerformAutoUpdate:
    """Tests for the full auto-update flow."""

    def test_disabled_returns_false(self):
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": False, "check_interval": 10, "notify": True,
        }):
            assert perform_auto_update("/fake", "/fake/instance") is False

    def test_no_updates_returns_false(self):
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), patch("app.auto_update.check_for_updates", return_value=0):
            assert perform_auto_update("/fake", "/fake/instance") is False

    def test_update_success_triggers_restart(self):
        update_result = UpdateResult(
            success=True, old_commit="abc", new_commit="def",
            commits_pulled=3, stashed=False,
        )
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), \
             patch("app.auto_update.check_for_updates", return_value=3), \
             patch("app.update_manager.pull_upstream", return_value=update_result), \
             patch("app.notify.format_and_send") as mock_notify, \
             patch("app.restart_manager.request_restart") as mock_restart, \
             patch("app.pause_manager.remove_pause") as mock_unpause:
            result = perform_auto_update("/fake", "/fake/instance")

        assert result is True
        mock_restart.assert_called_once_with("/fake")
        mock_unpause.assert_called_once_with("/fake")
        # Should have sent 2 notifications: before and after
        assert mock_notify.call_count == 2

    def test_pull_failure_returns_false(self):
        update_result = UpdateResult(
            success=False, old_commit="abc", new_commit="abc",
            commits_pulled=0, error="merge conflict",
        )
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), \
             patch("app.auto_update.check_for_updates", return_value=3), \
             patch("app.update_manager.pull_upstream", return_value=update_result), \
             patch("app.notify.format_and_send"):
            result = perform_auto_update("/fake", "/fake/instance")

        assert result is False

    def test_no_notify_when_disabled(self):
        update_result = UpdateResult(
            success=True, old_commit="abc", new_commit="def",
            commits_pulled=3, stashed=False,
        )
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": False,
        }), \
             patch("app.auto_update.check_for_updates", return_value=3), \
             patch("app.update_manager.pull_upstream", return_value=update_result), \
             patch("app.restart_manager.request_restart"), \
             patch("app.pause_manager.remove_pause"), \
             patch("app.notify.format_and_send") as mock_notify:
            result = perform_auto_update("/fake", "/fake/instance")

        assert result is True
        mock_notify.assert_not_called()

    def test_stashed_warning_in_notification(self):
        update_result = UpdateResult(
            success=True, old_commit="abc", new_commit="def",
            commits_pulled=1, stashed=True,
        )
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), \
             patch("app.auto_update.check_for_updates", return_value=1), \
             patch("app.update_manager.pull_upstream", return_value=update_result), \
             patch("app.notify.format_and_send") as mock_notify, \
             patch("app.restart_manager.request_restart"), \
             patch("app.pause_manager.remove_pause"):
            perform_auto_update("/fake", "/fake/instance")

        # Last call should include stash warning
        last_call_msg = mock_notify.call_args_list[-1][0][0]
        assert "auto-stashed" in last_call_msg

    def test_notification_failure_does_not_block_update(self):
        update_result = UpdateResult(
            success=True, old_commit="abc", new_commit="def",
            commits_pulled=2, stashed=False,
        )
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), \
             patch("app.auto_update.check_for_updates", return_value=2), \
             patch("app.update_manager.pull_upstream", return_value=update_result), \
             patch("app.notify.format_and_send", side_effect=Exception("telegram down")), \
             patch("app.restart_manager.request_restart") as mock_restart, \
             patch("app.pause_manager.remove_pause"):
            result = perform_auto_update("/fake", "/fake/instance")

        assert result is True
        mock_restart.assert_called_once()

    def test_check_returns_none_does_not_update(self):
        """check_for_updates returning None (error) should not trigger update."""
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), patch("app.auto_update.check_for_updates", return_value=None):
            assert perform_auto_update("/fake", "/fake/instance") is False

    def test_pull_success_but_no_change(self):
        """pull_upstream succeeds but changed=False (race condition)."""
        update_result = UpdateResult(
            success=True, old_commit="abc", new_commit="abc",
            commits_pulled=0, stashed=False,
        )
        with patch("app.auto_update._load_auto_update_config", return_value={
            "enabled": True, "check_interval": 10, "notify": True,
        }), \
             patch("app.auto_update.check_for_updates", return_value=1), \
             patch("app.update_manager.pull_upstream", return_value=update_result), \
             patch("app.notify.format_and_send"):
            result = perform_auto_update("/fake", "/fake/instance")

        assert result is False


class TestConfigValidator:
    """Tests that auto_update is properly registered in config schema."""

    def test_auto_update_is_valid_key(self):
        from app.config_validator import validate_config
        warnings = validate_config({"auto_update": {"enabled": True}})
        assert not any("unrecognized" in msg for _, msg in warnings)

    def test_auto_update_bad_type_warns(self):
        from app.config_validator import validate_config
        warnings = validate_config({"auto_update": {"enabled": "yes"}})
        assert any("bool" in msg for _, msg in warnings)

    def test_auto_update_unknown_subkey_warns(self):
        from app.config_validator import validate_config
        warnings = validate_config({"auto_update": {"typo_key": True}})
        assert any("unrecognized" in msg for _, msg in warnings)
