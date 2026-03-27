"""Tests for app.mission_complexity — dual-heuristic spec gate."""

import pytest

from app.mission_complexity import (
    COMPLEXITY_KEYWORDS,
    DEFAULT_COMPLEXITY_THRESHOLD,
    _strip_project_tag,
    is_complex_mission,
)


# ---------------------------------------------------------------------------
# _strip_project_tag
# ---------------------------------------------------------------------------

class TestStripProjectTag:
    def test_strips_simple_tag(self):
        assert _strip_project_tag("[project:koan] do something") == "do something"

    def test_no_tag(self):
        assert _strip_project_tag("do something") == "do something"

    def test_empty(self):
        assert _strip_project_tag("") == ""

    def test_tag_only(self):
        assert _strip_project_tag("[project:foo]") == ""

    def test_tag_with_extra_spaces(self):
        assert _strip_project_tag("[project:bar]   text here") == "text here"

    def test_non_project_brackets(self):
        assert _strip_project_tag("[other:tag] text") == "[other:tag] text"


# ---------------------------------------------------------------------------
# is_complex_mission — basic gates
# ---------------------------------------------------------------------------

class TestIsComplexMission:
    def test_empty_title(self):
        assert is_complex_mission("") is False

    def test_none_title(self):
        assert is_complex_mission(None) is False

    def test_skill_mission_rejected(self):
        """Skill commands (starting with /) are never complex."""
        assert is_complex_mission("/plan something big to refactor the feature") is False

    def test_skill_with_project_tag(self):
        assert is_complex_mission("[project:koan] /review https://example.com") is False

    def test_short_title_rejected(self):
        """Titles below threshold length are not complex, even with keywords."""
        assert is_complex_mission("implement this") is False

    def test_long_title_without_keywords(self):
        """Long titles without complexity keywords are not complex."""
        title = "Fix the bug where the login button does not work correctly on mobile devices when rotating"
        assert len(title) >= DEFAULT_COMPLEXITY_THRESHOLD
        assert is_complex_mission(title) is False

    def test_complex_mission_detected(self):
        """Title with keyword AND sufficient length triggers complexity."""
        title = "Implement a new authentication pipeline that integrates with the external OAuth provider service"
        assert len(title) >= DEFAULT_COMPLEXITY_THRESHOLD
        assert is_complex_mission(title) is True

    def test_project_tag_stripped_before_check(self):
        """Project tag length should not count toward the threshold."""
        # Build a title that's long enough only with the tag
        short_body = "implement x"  # < threshold
        tagged = f"[project:koan] {short_body}"
        assert is_complex_mission(tagged) is False

    def test_project_tag_with_complex_body(self):
        """Project tag is stripped; complexity is evaluated on the body only."""
        body = "Refactor the entire notification system to support multiple channels with fallback routing"
        tagged = f"[project:koan] {body}"
        assert is_complex_mission(tagged) is True


# ---------------------------------------------------------------------------
# Keyword coverage
# ---------------------------------------------------------------------------

class TestKeywordCoverage:
    @pytest.mark.parametrize("keyword", COMPLEXITY_KEYWORDS)
    def test_each_keyword_triggers(self, keyword):
        """Every keyword in the list should trigger when combined with sufficient length."""
        padding = "x" * max(0, DEFAULT_COMPLEXITY_THRESHOLD - len(keyword) - 20)
        title = f"We need to {keyword} the {padding} in the codebase thoroughly"
        assert len(title) >= DEFAULT_COMPLEXITY_THRESHOLD, f"Title too short: {len(title)}"
        assert is_complex_mission(title) is True, f"Keyword '{keyword}' did not trigger"

    def test_keyword_case_insensitive(self):
        title = "IMPLEMENT a new FEATURE for the complete MIGRATION of the data pipeline across services"
        assert is_complex_mission(title) is True

    def test_keyword_as_substring(self):
        """Keywords match as substrings (e.g., 'implementation' contains 'implement')."""
        title = "The implementation of the new system architecture requires careful redesign of core services"
        assert is_complex_mission(title) is True


# ---------------------------------------------------------------------------
# Config threshold override
# ---------------------------------------------------------------------------

class TestConfigThreshold:
    def test_custom_threshold_from_config(self, monkeypatch):
        """Config can lower the threshold to detect shorter complex missions."""
        monkeypatch.setattr(
            "app.mission_complexity._get_complexity_threshold",
            lambda: 20,
        )
        assert is_complex_mission("implement this thing") is True

    def test_high_threshold_rejects(self, monkeypatch):
        """Config can raise the threshold to reject otherwise-complex missions."""
        monkeypatch.setattr(
            "app.mission_complexity._get_complexity_threshold",
            lambda: 500,
        )
        title = "Implement a complete migration of the system architecture"
        assert is_complex_mission(title) is False
