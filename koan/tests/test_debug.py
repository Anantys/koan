"""Tests for app.debug — debug logging module."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.debug import debug_log, reset


@pytest.fixture(autouse=True)
def _reset_debug():
    """Reset debug module cache before each test."""
    reset()
    yield
    reset()


class TestDebugLog:
    def test_disabled_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", return_value=False):
            debug_log("should not appear")
        assert not (tmp_path / ".koan-debug.log").exists()

    def test_enabled_writes_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", return_value=True):
            debug_log("hello debug")
            debug_log("second line")
        log_file = tmp_path / ".koan-debug.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "hello debug" in content
        assert "second line" in content
        # Each line has a timestamp prefix
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) == 2
        for line in lines:
            assert line.startswith("[")

    def test_noop_when_explicit_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", return_value=False):
            debug_log("nothing")
        assert not (tmp_path / ".koan-debug.log").exists()

    def test_reset_clears_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        # First: disabled
        with patch("app.config.get_debug_enabled", return_value=False):
            debug_log("skip")
        assert not (tmp_path / ".koan-debug.log").exists()

        # Reset and re-enable
        reset()
        with patch("app.config.get_debug_enabled", return_value=True):
            debug_log("now visible")
        assert (tmp_path / ".koan-debug.log").exists()
        assert "now visible" in (tmp_path / ".koan-debug.log").read_text()

    def test_handles_missing_koan_root(self, monkeypatch):
        monkeypatch.delenv("KOAN_ROOT", raising=False)
        with patch("app.config.get_debug_enabled", return_value=True):
            # Should not raise — gracefully disabled
            debug_log("no root")

    def test_handles_config_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", side_effect=ImportError("boom")):
            # Should not raise — falls back to disabled
            debug_log("config error")
        assert not (tmp_path / ".koan-debug.log").exists()

    def test_handles_os_error_from_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", side_effect=OSError("disk")):
            debug_log("os error")
        assert not (tmp_path / ".koan-debug.log").exists()

    def test_handles_value_error_from_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", side_effect=ValueError("bad")):
            debug_log("value error")
        assert not (tmp_path / ".koan-debug.log").exists()

    def test_enabled_but_empty_koan_root(self, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", "")
        with patch("app.config.get_debug_enabled", return_value=True):
            debug_log("no root dir")
        # Empty KOAN_ROOT disables debug even when config says enabled

    def test_write_error_silently_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", return_value=True):
            debug_log("init")  # triggers _init, sets _log_path
        with patch("builtins.open", side_effect=OSError("permission denied")):
            # Should not raise
            debug_log("write fails")

    def test_timestamp_format(self, tmp_path, monkeypatch):
        import re
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", return_value=True):
            debug_log("check format")
        content = (tmp_path / ".koan-debug.log").read_text().strip()
        # Expected: [YYYY-MM-DD HH:MM:SS] check format
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] check format$", content)

    def test_lazy_init_only_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        with patch("app.config.get_debug_enabled", return_value=True) as mock_cfg:
            debug_log("first")
            debug_log("second")
            debug_log("third")
            # _init called only on the first debug_log
            assert mock_cfg.call_count == 1
