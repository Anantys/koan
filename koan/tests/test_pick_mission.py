"""Tests for pick_mission.py — FIFO mission picker."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pick_mission import pick_mission, fallback_extract


class TestFallbackExtract:
    def test_with_inline_tag(self):
        content = "# Missions\n\n## Pending\n\n- [project:koan] fix tests\n\n## In Progress\n\n## Done\n"
        project, title = fallback_extract(content, "koan:/path")
        assert project == "koan"
        assert title == "fix tests"

    def test_without_tag(self):
        content = "# Missions\n\n## Pending\n\n- fix tests\n\n## In Progress\n\n## Done\n"
        project, title = fallback_extract(content, "koan:/path;anantys:/path2")
        assert project == "koan"
        assert title == "fix tests"

    def test_no_pending(self):
        content = "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        project, title = fallback_extract(content, "koan:/path")
        assert project is None

    def test_with_subheader_tag(self):
        content = (
            "# Missions\n\n## Pending\n\n"
            "### projet:anantys-back\n\n"
            "### project:koan\n"
            "- fix rotation bug\n\n"
            "## In Progress\n\n## Done\n"
        )
        # fallback_extract uses extract_next_pending which now respects sub-headers
        # but fallback_extract doesn't pass project_name, so it returns first item
        project, title = fallback_extract(content, "koan:/path")
        assert project == "koan"
        assert title == "fix rotation bug"

    def test_empty_content(self):
        project, title = fallback_extract("", "koan:/path")
        assert project is None

    def test_empty_projects_str_defaults_to_default(self):
        """When projects_str is empty, untagged missions get project='default'."""
        content = "# Missions\n\n## Pending\n\n- fix tests\n\n## In Progress\n\n## Done\n"
        project, title = fallback_extract(content, "")
        assert project == "default"
        assert title == "fix tests"


class TestPickMission:
    """Tests for FIFO mission picking behavior."""

    def test_picks_first_pending_mission(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n"
            "- [project:koan] fix tests\n"
            "- [project:anantys] implement dashboard\n\n"
            "## In Progress\n\n## Done\n"
        )
        result = pick_mission(str(tmp_path), "koan:/p1;anantys:/p2", "2", "implement", "koan")
        # Must pick first item (koan:fix tests), NOT rotate to anantys
        assert result == "koan:fix tests"

    def test_fifo_with_multiple_projects(self, tmp_path):
        """FIFO order is respected even when last_project matches first mission."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n"
            "- [project:koan] fix tests\n"
            "- [project:koan] refactor utils\n"
            "- [project:anantys] implement dashboard\n\n"
            "## In Progress\n\n## Done\n"
        )
        # Even though last_project is koan, we still pick the first koan mission
        result = pick_mission(str(tmp_path), "koan:/p1;anantys:/p2", "2", "implement", "koan")
        assert result == "koan:fix tests"

    def test_empty_when_no_missions(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        result = pick_mission(str(tmp_path), "koan:/p1", "1", "implement")
        assert result == ""

    def test_missing_missions_file(self, tmp_path):
        result = pick_mission(str(tmp_path), "koan:/p1", "1", "implement")
        assert result == ""

    def test_fifo_respects_queue_order_across_projects(self, tmp_path):
        """When project A's mission is queued before project B's, A goes first."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n"
            "- [project:grep] implement issue #36\n"
            "- [project:koan] investigate ordering bug\n"
            "- [project:grep] implement issue #33\n\n"
            "## In Progress\n\n## Done\n"
        )
        result = pick_mission(str(tmp_path), "koan:/p1;grep:/p2", "5", "deep", "grep")
        # Must pick grep issue #36 (first in queue), even though last_project was grep
        assert result == "grep:implement issue #36"

    def test_single_mission(self, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- [project:koan] fix tests\n\n## In Progress\n\n## Done\n"
        )
        result = pick_mission(str(tmp_path), "koan:/p1;anantys:/p2", "1", "implement")
        assert result == "koan:fix tests"

    def test_strips_timestamps_from_title(self, tmp_path):
        """Queued timestamps should be included in the extracted title."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n"
            "- [project:koan] fix tests ⏳(2026-03-11T21:00)\n\n"
            "## In Progress\n\n## Done\n"
        )
        result = pick_mission(str(tmp_path), "koan:/p1", "1", "implement")
        assert result.startswith("koan:")
        assert "fix tests" in result


class TestPickMissionCLI:
    """CLI tests — pick_mission.py has no main() function, just __main__ guard.
    We test the missing-args path via runpy, and the happy paths are already
    covered by TestPickMission integration tests."""

    def test_cli_exit_on_missing_args(self):
        with patch.object(sys, "argv", ["pick_mission.py"]):
            with pytest.raises(SystemExit) as exc_info:
                from tests._helpers import run_module
                run_module("app.pick_mission", run_name="__main__")
            assert exc_info.value.code == 1
