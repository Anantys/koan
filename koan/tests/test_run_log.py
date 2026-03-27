"""Tests for app.run_log — colored logging module."""

import os
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test: _init_colors
# ---------------------------------------------------------------------------

class TestInitColors:
    def test_tty_enables_colors(self, monkeypatch):
        monkeypatch.delenv("KOAN_FORCE_COLOR", raising=False)
        import app.run_log as mod
        mod._COLORS = {}
        with patch.object(mod.sys.stdout, "isatty", return_value=True):
            mod._init_colors()
        assert mod._COLORS.get("reset") == "\033[0m"
        assert mod._COLORS.get("red") == "\033[31m"

    def test_no_tty_disables_colors(self, monkeypatch):
        monkeypatch.delenv("KOAN_FORCE_COLOR", raising=False)
        import app.run_log as mod
        mod._COLORS = {}
        with patch.object(mod.sys.stdout, "isatty", return_value=False):
            mod._init_colors()
        assert mod._COLORS.get("reset") == ""
        assert mod._COLORS.get("red") == ""

    def test_force_color_overrides_tty(self, monkeypatch):
        monkeypatch.setenv("KOAN_FORCE_COLOR", "1")
        import app.run_log as mod
        mod._COLORS = {}
        with patch.object(mod.sys.stdout, "isatty", return_value=False):
            mod._init_colors()
        assert mod._COLORS.get("reset") == "\033[0m"


# ---------------------------------------------------------------------------
# Test: log
# ---------------------------------------------------------------------------

class TestLog:
    def test_outputs_category_and_message(self, capsys):
        from app.run_log import log, _init_colors
        _init_colors()
        log("koan", "hello world")
        out = capsys.readouterr().out
        assert "[koan]" in out
        assert "hello world" in out

    def test_unknown_category_uses_white(self, capsys):
        from app.run_log import log, _init_colors
        _init_colors()
        log("custom_cat", "msg")
        out = capsys.readouterr().out
        assert "[custom_cat]" in out

    def test_lazy_init(self):
        """log() should auto-initialize colors if empty."""
        import app.run_log as mod
        mod._COLORS = {}
        mod.log("test", "auto-init")
        assert mod._COLORS  # Should be populated now


# ---------------------------------------------------------------------------
# Test: _styled
# ---------------------------------------------------------------------------

class TestStyled:
    def test_applies_styles(self):
        import app.run_log as mod
        mod._COLORS = {}
        mod._init_colors()
        result = mod._styled("text", "bold", "cyan")
        assert "text" in result

    def test_empty_colors(self):
        """With no TTY, styled text should still contain the text."""
        import app.run_log as mod
        mod._COLORS = {k: "" for k in [
            "reset", "bold", "dim", "red", "green", "yellow",
            "blue", "magenta", "cyan", "white",
        ]}
        result = mod._styled("hello", "bold", "cyan")
        assert "hello" in result


# ---------------------------------------------------------------------------
# Test: bold_cyan, bold_green
# ---------------------------------------------------------------------------

class TestBoldHelpers:
    def test_bold_cyan(self):
        from app.run_log import bold_cyan, _init_colors
        _init_colors()
        result = bold_cyan("test")
        assert "test" in result

    def test_bold_green(self):
        from app.run_log import bold_green, _init_colors
        _init_colors()
        result = bold_green("test")
        assert "test" in result


# ---------------------------------------------------------------------------
# Test: _reset_terminal
# ---------------------------------------------------------------------------

class TestResetTerminal:
    def test_writes_reset_sequence(self, capsys):
        from app.run_log import _reset_terminal
        _reset_terminal()
        out = capsys.readouterr().out
        assert "\033[0m" in out

    def test_handles_os_error(self):
        """Should not raise when stdout is gone."""
        from app.run_log import _reset_terminal
        with patch("app.run_log.sys.stdout") as mock_stdout:
            mock_stdout.write.side_effect = OSError("fd closed")
            _reset_terminal()  # Should not raise


# ---------------------------------------------------------------------------
# Test: _CATEGORY_COLORS
# ---------------------------------------------------------------------------

class TestCategoryColors:
    def test_all_categories_defined(self):
        from app.run_log import _CATEGORY_COLORS
        expected = {"koan", "error", "init", "health", "git", "github",
                    "mission", "quota", "pause", "warning", "warn"}
        assert expected.issubset(set(_CATEGORY_COLORS.keys()))

    def test_warning_and_warn_both_exist(self):
        from app.run_log import _CATEGORY_COLORS
        assert "warning" in _CATEGORY_COLORS
        assert "warn" in _CATEGORY_COLORS
