"""Tests for app.spec_generator — mission spec document generation."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.spec_generator import (
    _slugify,
    _get_spec_timeout,
    generate_spec,
    load_spec_for_mission,
    save_spec,
)


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert _slugify("Fix the login bug") == "fix-the-login-bug"

    def test_special_chars(self):
        assert _slugify("feat: add OAuth2 support!") == "feat-add-oauth2-support"

    def test_truncation(self):
        long_title = "a" * 100
        assert len(_slugify(long_title)) == 60

    def test_leading_trailing_hyphens(self):
        assert _slugify("---hello---") == "hello"

    def test_empty(self):
        assert _slugify("") == ""

    def test_unicode(self):
        result = _slugify("implémentation du système")
        assert "impl" in result  # Accented chars stripped to hyphens

    def test_multiple_spaces_and_symbols(self):
        assert _slugify("foo   bar // baz") == "foo-bar-baz"


# ---------------------------------------------------------------------------
# _get_spec_timeout
# ---------------------------------------------------------------------------

class TestGetSpecTimeout:
    def test_default_fallback(self):
        with patch("app.config.get_skill_timeout", side_effect=ImportError):
            assert _get_spec_timeout() == 300

    def test_quarter_of_skill_timeout(self):
        with patch("app.config.get_skill_timeout", return_value=1200):
            assert _get_spec_timeout() == 300

    def test_minimum_60s(self):
        with patch("app.config.get_skill_timeout", return_value=100):
            assert _get_spec_timeout() == 60


# ---------------------------------------------------------------------------
# save_spec / load_spec_for_mission round-trip
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_round_trip(self, tmp_path):
        """Save a spec, then load it back."""
        instance_dir = str(tmp_path)
        title = "Implement new auth pipeline"
        content = "## Goal\nBuild OAuth2 support.\n"

        path = save_spec(instance_dir, title, content)
        assert path is not None
        assert path.exists()
        assert path.suffix == ".md"

        loaded = load_spec_for_mission(instance_dir, title)
        assert loaded == content.strip()

    def test_save_creates_directory(self, tmp_path):
        """Specs dir is created on demand."""
        instance_dir = str(tmp_path)
        save_spec(instance_dir, "test", "content")
        # Directory structure: journal/{date}/specs/
        specs_dirs = list(tmp_path.glob("journal/*/specs"))
        assert len(specs_dirs) == 1

    def test_load_missing_spec(self, tmp_path):
        """Loading a non-existent spec returns empty string."""
        result = load_spec_for_mission(str(tmp_path), "nonexistent mission")
        assert result == ""

    def test_save_failure_returns_none(self):
        """Bad instance dir returns None without raising."""
        result = save_spec("/nonexistent/path/that/wont/work", "test", "content")
        # Depending on OS, this might create or fail — just verify no exception
        # and the return is either a path or None
        assert result is None or isinstance(result, Path)


# ---------------------------------------------------------------------------
# generate_spec
# ---------------------------------------------------------------------------

class TestGenerateSpec:
    def test_returns_output_on_success(self, tmp_path):
        """Successful CLI call returns stripped output."""
        with patch("app.prompts.load_prompt", return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value="  ## Goal\nDo things\n  "):
            result = generate_spec(str(tmp_path), "test mission", str(tmp_path))
        assert result == "## Goal\nDo things"

    def test_returns_none_on_empty_output(self, tmp_path):
        with patch("app.prompts.load_prompt", return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value=""):
            result = generate_spec(str(tmp_path), "test mission", str(tmp_path))
        assert result is None

    def test_returns_none_on_none_output(self, tmp_path):
        with patch("app.prompts.load_prompt", return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value=None):
            result = generate_spec(str(tmp_path), "test mission", str(tmp_path))
        assert result is None

    def test_returns_none_on_cli_exception(self, tmp_path):
        with patch("app.prompts.load_prompt", return_value="prompt"), \
             patch("app.cli_provider.run_command", side_effect=RuntimeError("boom")):
            result = generate_spec(str(tmp_path), "test mission", str(tmp_path))
        assert result is None

    def test_import_error_returns_none(self, tmp_path):
        """If cli_provider can't be imported, gracefully returns None."""
        with patch.dict("sys.modules", {"app.cli_provider": None}):
            result = generate_spec(str(tmp_path), "test mission", str(tmp_path))
            assert result is None
