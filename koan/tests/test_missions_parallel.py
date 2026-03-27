"""Tests for parallel session extensions in missions.py.

Verifies pick_missions(), start_mission_parallel(), complete/fail_mission_by_session(),
and backward compatibility with existing single-agent functions.
"""

import pytest
from unittest.mock import patch

from app.missions import (
    pick_missions,
    start_mission_parallel,
    complete_mission_by_session,
    fail_mission_by_session,
    count_in_progress,
    parse_sections,
    start_mission,
    complete_mission,
    fail_mission,
    _extract_session_id,
    _strip_session_tag,
    DEFAULT_SKELETON,
)


MULTI_PROJECT_CONTENT = (
    "# Missions\n\n"
    "## Pending\n\n"
    "- [project:alpha] Fix auth bug\n"
    "- [project:beta] Add logging\n"
    "- [project:alpha] Update tests\n"
    "- [project:gamma] Refactor API\n"
    "- [project:beta] Fix CSS\n\n"
    "## In Progress\n\n"
    "## Done\n\n"
    "## Failed\n"
)

SIMPLE_CONTENT = (
    "# Missions\n\n"
    "## Pending\n\n"
    "- Fix the bug\n"
    "- Add feature\n"
    "- Write docs\n\n"
    "## In Progress\n\n"
    "## Done\n\n"
    "## Failed\n"
)


class TestPickMissions:
    def test_pick_one(self):
        missions = pick_missions(SIMPLE_CONTENT, n=1)
        assert len(missions) == 1
        assert "Fix the bug" in missions[0]

    def test_pick_multiple(self):
        missions = pick_missions(SIMPLE_CONTENT, n=3)
        assert len(missions) == 3

    def test_pick_more_than_available(self):
        missions = pick_missions(SIMPLE_CONTENT, n=10)
        assert len(missions) == 3

    def test_pick_zero(self):
        assert pick_missions(SIMPLE_CONTENT, n=0) == []

    def test_pick_from_empty(self):
        assert pick_missions(DEFAULT_SKELETON, n=3) == []

    def test_project_diversity(self):
        """Should prefer different projects over same project."""
        missions = pick_missions(MULTI_PROJECT_CONTENT, n=3)
        assert len(missions) == 3
        # Should pick from 3 different projects (alpha, beta, gamma)
        # rather than 2 from alpha + 1 from beta
        texts = " ".join(missions)
        # At least check we got missions from different projects
        projects = set()
        for m in missions:
            if "alpha" in m:
                projects.add("alpha")
            elif "beta" in m:
                projects.add("beta")
            elif "gamma" in m:
                projects.add("gamma")
        assert len(projects) == 3

    def test_exclude_projects(self):
        missions = pick_missions(
            MULTI_PROJECT_CONTENT, n=5, exclude_projects=["alpha"],
        )
        for m in missions:
            assert "alpha" not in m.lower() or "project:alpha" not in m

    def test_skips_strikethrough(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- ~~Done already~~\n"
            "- Active mission\n\n"
            "## In Progress\n\n## Done\n"
        )
        missions = pick_missions(content, n=2)
        assert len(missions) == 1
        assert "Active mission" in missions[0]


class TestStartMissionParallel:
    def test_moves_to_in_progress(self):
        result = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 1
        assert "sess1" in sections["in_progress"][0]
        assert "Fix the bug" in sections["in_progress"][0]
        assert len(sections["pending"]) == 2  # Two remaining

    def test_does_not_flush_existing(self):
        """Multiple missions can be in progress simultaneously."""
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        content = start_mission_parallel(content, "Add feature", "sess2")
        sections = parse_sections(content)
        assert len(sections["in_progress"]) == 2

    def test_session_tag_added(self):
        result = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "abc123")
        sections = parse_sections(result)
        in_progress = sections["in_progress"][0]
        assert "[session:abc123]" in in_progress

    def test_started_timestamp_added(self):
        result = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "s1")
        sections = parse_sections(result)
        assert "▶" in sections["in_progress"][0]

    def test_not_found_returns_unchanged(self):
        result = start_mission_parallel(SIMPLE_CONTENT, "Nonexistent", "s1")
        assert result == SIMPLE_CONTENT


class TestCompleteMissionBySession:
    def test_completes_by_session_id(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        content = start_mission_parallel(content, "Add feature", "sess2")
        result = complete_mission_by_session(content, "sess1")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 1
        assert "sess2" in sections["in_progress"][0]
        assert len(sections["done"]) >= 1
        # Done entry should not contain session tag
        done_text = sections["done"][0]
        assert "session:" not in done_text
        assert "Fix the bug" in done_text
        assert "✅" in done_text

    def test_nonexistent_session_unchanged(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        result = complete_mission_by_session(content, "nonexistent")
        assert result == content

    def test_preserves_other_sessions(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        content = start_mission_parallel(content, "Add feature", "sess2")
        content = start_mission_parallel(content, "Write docs", "sess3")
        result = complete_mission_by_session(content, "sess2")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 2
        session_ids = [_extract_session_id(s) for s in sections["in_progress"]]
        assert "sess1" in session_ids
        assert "sess3" in session_ids


class TestFailMissionBySession:
    def test_fails_by_session_id(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        result = fail_mission_by_session(content, "sess1")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert len(sections["failed"]) >= 1
        assert "Fix the bug" in sections["failed"][0]
        assert "❌" in sections["failed"][0]

    def test_nonexistent_session_unchanged(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "sess1")
        result = fail_mission_by_session(content, "nonexistent")
        assert result == content


class TestCountInProgress:
    def test_empty(self):
        assert count_in_progress(DEFAULT_SKELETON) == 0

    def test_single(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "s1")
        assert count_in_progress(content) == 1

    def test_multiple(self):
        content = start_mission_parallel(SIMPLE_CONTENT, "Fix the bug", "s1")
        content = start_mission_parallel(content, "Add feature", "s2")
        assert count_in_progress(content) == 2


class TestSessionTagHelpers:
    def test_extract_session_id(self):
        assert _extract_session_id("- [session:abc] Fix bug") == "abc"
        assert _extract_session_id("- Fix bug") == ""

    def test_strip_session_tag(self):
        assert _strip_session_tag("[session:abc] Fix bug") == "Fix bug"
        assert _strip_session_tag("Fix bug") == "Fix bug"


class TestBackwardCompatibility:
    """Verify that existing single-agent functions still work correctly."""

    def test_start_mission_still_flushes(self):
        """start_mission() in single-agent mode still flushes stale in-progress."""
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New mission\n\n"
            "## In Progress\n\n"
            "- Stale mission\n\n"
            "## Done\n\n"
            "## Failed\n"
        )
        result = start_mission(content, "New mission")
        sections = parse_sections(result)
        # Old in-progress should be flushed to done
        assert len(sections["in_progress"]) == 1
        assert "New mission" in sections["in_progress"][0]
        assert any("Stale mission" in d for d in sections["done"])

    def test_complete_mission_still_works(self):
        content = start_mission(SIMPLE_CONTENT, "Fix the bug")
        result = complete_mission(content, "Fix the bug")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert any("Fix the bug" in d for d in sections["done"])

    def test_fail_mission_still_works(self):
        content = start_mission(SIMPLE_CONTENT, "Fix the bug")
        result = fail_mission(content, "Fix the bug")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert any("Fix the bug" in d for d in sections["failed"])
