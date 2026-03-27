"""Tests for the startup delay feature (#1039).

The startup delay prevents the race condition where a mission starts
before the Telegram bridge can process a /pause command sent right
after ``make start``.
"""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.signals import PAUSE_FILE, STOP_FILE, SHUTDOWN_FILE, RESTART_FILE


@pytest.fixture
def koan_root(tmp_path):
    """Create a minimal koan root directory."""
    (tmp_path / "instance").mkdir()
    return str(tmp_path)


class TestStartupDelay:
    """Tests for app.run._startup_delay()."""

    def _import_fn(self):
        from app.run import _startup_delay
        return _startup_delay

    def test_default_delay_waits(self, koan_root):
        """With default config, the delay waits approximately 30s."""
        fn = self._import_fn()
        with patch("app.utils.load_config", return_value={}), \
             patch("time.sleep") as mock_sleep:
            fn(koan_root)

        # Should have slept in 2s ticks for ~30s
        total = sum(call.args[0] for call in mock_sleep.call_args_list)
        assert total == pytest.approx(30, abs=1)

    def test_zero_delay_skips(self, koan_root):
        """startup_delay: 0 disables the delay entirely."""
        fn = self._import_fn()
        with patch("app.utils.load_config", return_value={"startup_delay": 0}), \
             patch("time.sleep") as mock_sleep:
            fn(koan_root)

        mock_sleep.assert_not_called()

    def test_negative_delay_skips(self, koan_root):
        """Negative values are treated like zero."""
        fn = self._import_fn()
        with patch("app.utils.load_config", return_value={"startup_delay": -5}), \
             patch("time.sleep") as mock_sleep:
            fn(koan_root)

        mock_sleep.assert_not_called()

    def test_custom_delay(self, koan_root):
        """Custom startup_delay value is respected."""
        fn = self._import_fn()
        with patch("app.utils.load_config", return_value={"startup_delay": 10}), \
             patch("time.sleep") as mock_sleep:
            fn(koan_root)

        total = sum(call.args[0] for call in mock_sleep.call_args_list)
        assert total == pytest.approx(10, abs=1)

    def test_already_paused_skips(self, koan_root):
        """If .koan-pause exists at startup, skip the delay."""
        fn = self._import_fn()
        Path(koan_root, PAUSE_FILE).touch()

        with patch("app.utils.load_config", return_value={}), \
             patch("time.sleep") as mock_sleep:
            fn(koan_root)

        mock_sleep.assert_not_called()

    def test_pause_signal_interrupts(self, koan_root):
        """If .koan-pause appears during the delay, it stops early."""
        fn = self._import_fn()
        call_count = 0

        def sleep_then_pause(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                Path(koan_root, PAUSE_FILE).touch()

        with patch("app.utils.load_config", return_value={"startup_delay": 30}), \
             patch("time.sleep", side_effect=sleep_then_pause):
            fn(koan_root)

        # Should have stopped after 3 ticks (6s), not the full 30s
        assert call_count == 3

    def test_stop_signal_interrupts(self, koan_root):
        """If .koan-stop appears during the delay, it stops early."""
        fn = self._import_fn()
        call_count = 0

        def sleep_then_stop(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                Path(koan_root, STOP_FILE).touch()

        with patch("app.utils.load_config", return_value={"startup_delay": 30}), \
             patch("time.sleep", side_effect=sleep_then_stop):
            fn(koan_root)

        assert call_count == 2

    def test_shutdown_signal_interrupts(self, koan_root):
        """If .koan-shutdown appears during the delay, it stops early."""
        fn = self._import_fn()
        call_count = 0

        def sleep_then_shutdown(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                Path(koan_root, SHUTDOWN_FILE).touch()

        with patch("app.utils.load_config", return_value={"startup_delay": 30}), \
             patch("time.sleep", side_effect=sleep_then_shutdown):
            fn(koan_root)

        assert call_count == 1

    def test_restart_signal_interrupts(self, koan_root):
        """If .koan-restart appears during the delay, it stops early."""
        fn = self._import_fn()
        call_count = 0

        def sleep_then_restart(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                Path(koan_root, RESTART_FILE).touch()

        with patch("app.utils.load_config", return_value={"startup_delay": 30}), \
             patch("time.sleep", side_effect=sleep_then_restart):
            fn(koan_root)

        assert call_count == 2

    def test_logs_delay_start_and_end(self, koan_root):
        """Verify that log messages are emitted for delay start and end."""
        fn = self._import_fn()
        with patch("app.utils.load_config", return_value={"startup_delay": 4}), \
             patch("time.sleep"), \
             patch("app.run.log") as mock_log:
            fn(koan_root)

        messages = [call.args[1] for call in mock_log.call_args_list]
        assert any("waiting 4s" in m for m in messages)
        assert any("complete" in m.lower() for m in messages)

    def test_logs_skip_when_paused(self, koan_root):
        """When already paused, log that the delay is skipped."""
        fn = self._import_fn()
        Path(koan_root, PAUSE_FILE).touch()

        with patch("app.utils.load_config", return_value={}), \
             patch("time.sleep"), \
             patch("app.run.log") as mock_log:
            fn(koan_root)

        messages = [call.args[1] for call in mock_log.call_args_list]
        assert any("skipping" in m.lower() for m in messages)
