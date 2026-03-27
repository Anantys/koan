"""Tests for the /mission core skill — mission creation with --now flag."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext


def _make_ctx(args, instance_dir):
    """Create a minimal SkillContext for testing."""
    ctx = MagicMock(spec=SkillContext)
    ctx.args = args
    ctx.command_name = "mission"
    ctx.instance_dir = instance_dir
    return ctx


# ---------------------------------------------------------------------------
# /mission handler — --now flag integration
# ---------------------------------------------------------------------------

class TestMissionHandlerNowFlag:
    """Test that --now flag is parsed and passed as urgent=True."""

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix the bug"))
    def test_normal_mission_queued_at_bottom(self, _det, _proj, tmp_path):
        """Without --now, mission goes to bottom of queue."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing task\n\n## In Progress\n\n## Done\n"
        )

        from skills.core.mission.handler import handle
        ctx = _make_ctx("fix the bug", tmp_path)
        result = handle(ctx)

        assert "Mission received" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- existing task"
        assert lines[1].startswith("- fix the bug")

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix the bug"))
    def test_now_flag_queues_at_top(self, _det, _proj, tmp_path):
        """With --now, mission goes to top of queue."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing task\n\n## In Progress\n\n## Done\n"
        )

        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now fix the bug", tmp_path)
        result = handle(ctx)

        assert "priority" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- fix the bug")
        assert lines[1] == "- existing task"

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix --now the bug"))
    def test_now_flag_in_middle_of_first_five(self, _det, _proj, tmp_path):
        """--now in first 5 words still works."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing task\n\n## In Progress\n\n## Done\n"
        )

        from skills.core.mission.handler import handle
        ctx = _make_ctx("fix --now the bug", tmp_path)
        result = handle(ctx)

        assert "priority" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- fix the bug")

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "do something"))
    def test_now_flag_stripped_from_mission_text(self, _det, _proj, tmp_path):
        """--now should not appear in the mission entry."""
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now do something", tmp_path)
        result = handle(ctx)

        content = missions.read_text()
        assert "--now" not in content
        assert "- do something" in content

    def test_empty_args_shows_usage(self, tmp_path):
        from skills.core.mission.handler import handle
        ctx = _make_ctx("", tmp_path)
        result = handle(ctx)
        assert "Usage:" in result
        assert "--now" in result

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    def test_now_with_project_tag(self, _proj, tmp_path):
        """--now works with explicit [project:name] tag."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old task\n\n## In Progress\n\n## Done\n"
        )

        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now [project:koan] fix auth", tmp_path)
        result = handle(ctx)

        assert "priority" in result
        assert "project: koan" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- [project:koan] fix auth")
        assert lines[1] == "- old task"

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text")
    def test_now_with_project_autodetect(self, mock_detect, _proj, tmp_path):
        """--now works with auto-detected project name."""
        # After --now is stripped, "koan fix auth" is passed to detect_project_from_text
        mock_detect.return_value = ("koan", "fix auth")

        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old task\n\n## In Progress\n\n## Done\n"
        )

        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now koan fix auth", tmp_path)
        result = handle(ctx)

        assert "priority" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- [project:koan] fix auth")

# ---------------------------------------------------------------------------
# awake.py — handle_mission with --now
# ---------------------------------------------------------------------------

class TestAwakeHandleMissionNowFlag:
    """Test handle_mission() in awake.py also respects --now."""

    @patch("app.command_handlers.send_telegram")
    @patch("app.command_handlers.MISSIONS_FILE")
    def test_normal_mission_bottom(self, mock_file, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing\n\n## In Progress\n\n## Done\n"
        )
        mock_file.__fspath__ = lambda s: str(missions)
        # Patch MISSIONS_FILE to be the real path
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("fix something")

        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- existing"
        assert lines[1].startswith("- fix something")

    @patch("app.command_handlers.send_telegram")
    def test_now_flag_top(self, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("--now fix something")

        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- fix something")
        assert lines[1] == "- existing"

    @patch("app.command_handlers.send_telegram")
    def test_now_flag_stripped_from_text(self, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("--now deploy hotfix")

        content = missions.read_text()
        assert "--now" not in content
        assert "- deploy hotfix" in content

    @patch("app.command_handlers.send_telegram")
    def test_ack_message_includes_priority(self, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("--now urgent fix")

        ack = mock_send.call_args[0][0]
        assert "priority" in ack


# ---------------------------------------------------------------------------
# /mission handler — --now with multi-project (the original bug)
# ---------------------------------------------------------------------------

class TestMissionHandlerNowMultiProject:
    """--now should bypass the 'which project?' prompt in multi-project setups.

    Bug: before the fix, /mission --now fix something in a multi-project setup
    would stop at the project prompt, losing the --now flag entirely.
    """

    MULTI_PROJECTS = [("koan", "/path/koan"), ("backend", "/path/backend")]

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix the bug"))
    def test_now_skips_project_prompt_multi_project(self, _det, _proj, tmp_path):
        """--now bypasses 'Which project?' and inserts immediately."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old task\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now fix the bug", tmp_path)
        result = handle(ctx)

        assert "Mission received" in result
        assert "priority" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- fix the bug")
        assert lines[1] == "- old task"

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix the bug"))
    def test_without_now_asks_project_multi_project(self, _det, _proj, tmp_path):
        """Without --now, multi-project still asks 'Which project?'."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("fix the bug", tmp_path)
        result = handle(ctx)

        assert "Which project" in result
        assert "koan" in result
        assert "backend" in result
        # Mission was NOT inserted
        content = missions.read_text()
        assert "fix the bug" not in content

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    @patch("app.utils.detect_project_from_text")
    def test_now_with_autodetected_project_works(self, mock_detect, _proj, tmp_path):
        """--now with project auto-detection from first word still works."""
        mock_detect.return_value = ("koan", "fix auth crash")
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now koan fix auth crash", tmp_path)
        result = handle(ctx)

        assert "Mission received" in result
        assert "priority" in result
        assert "project: koan" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- [project:koan] fix auth crash")

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    def test_now_with_explicit_project_tag(self, _proj, tmp_path):
        """--now with [project:X] tag still works in multi-project."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now [project:backend] deploy hotfix", tmp_path)
        result = handle(ctx)

        assert "priority" in result
        assert "project: backend" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- [project:backend] deploy hotfix")

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix it"))
    def test_now_no_project_inserts_without_tag(self, _det, _proj, tmp_path):
        """--now without project: inserts mission without [project:X] tag."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("--now fix it", tmp_path)
        result = handle(ctx)

        content = missions.read_text()
        assert "- fix it" in content
        assert "[project:" not in content

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix it"))
    def test_single_project_no_prompt_regardless(self, _det, _proj, tmp_path):
        """Single project never asks 'Which project?' even without --now."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("fix it", tmp_path)
        result = handle(ctx)

        assert "Mission received" in result
        assert "Which project" not in result


# ---------------------------------------------------------------------------
# /mission handler — em dash variant (\u2014now) — the mobile keyboard bug
# ---------------------------------------------------------------------------

class TestMissionHandlerEmDashNow:
    """Mobile keyboards auto-correct -- to em dash (\u2014).

    /mission \u2014now project some mission should work the same as --now.
    """

    MULTI_PROJECTS = [("koan", "/path/koan"), ("backend", "/path/backend")]

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix the bug"))
    def test_em_dash_now_queues_at_top(self, _det, _proj, tmp_path):
        """\u2014now queues at top, same as --now."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing task\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("\u2014now fix the bug", tmp_path)
        result = handle(ctx)

        assert "priority" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- fix the bug")
        assert lines[1] == "- existing task"

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    @patch("app.utils.detect_project_from_text")
    def test_em_dash_now_with_project_autodetect(self, mock_detect, _proj, tmp_path):
        """\u2014now koan fix auth should detect project and queue at top."""
        mock_detect.return_value = ("koan", "fix auth")
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old task\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("\u2014now koan fix auth", tmp_path)
        result = handle(ctx)

        assert "Mission received" in result
        assert "priority" in result
        assert "project: koan" in result
        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- [project:koan] fix auth")

    @patch("app.utils.get_known_projects", return_value=MULTI_PROJECTS)
    @patch("app.utils.detect_project_from_text", return_value=(None, "fix the bug"))
    def test_em_dash_now_skips_project_prompt(self, _det, _proj, tmp_path):
        """\u2014now bypasses 'Which project?' in multi-project setups."""
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- old\n\n## In Progress\n\n## Done\n"
        )
        from skills.core.mission.handler import handle
        ctx = _make_ctx("\u2014now fix the bug", tmp_path)
        result = handle(ctx)

        assert "Mission received" in result
        assert "Which project" not in result

    @patch("app.utils.get_known_projects", return_value=[("koan", "/path")])
    @patch("app.utils.detect_project_from_text", return_value=(None, "do something"))
    def test_em_dash_stripped_from_mission_text(self, _det, _proj, tmp_path):
        """\u2014now should not appear in the stored mission."""
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        from skills.core.mission.handler import handle
        ctx = _make_ctx("\u2014now do something", tmp_path)
        result = handle(ctx)

        content = missions.read_text()
        assert "\u2014now" not in content
        assert "--now" not in content
        assert "- do something" in content


class TestAwakeHandleMissionEmDashNow:
    """Test handle_mission() in awake.py also respects \u2014now."""

    @patch("app.command_handlers.send_telegram")
    def test_em_dash_now_flag_top(self, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n- existing\n\n## In Progress\n\n## Done\n"
        )
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("\u2014now fix something")

        content = missions.read_text()
        lines = [l for l in content.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- fix something")
        assert lines[1] == "- existing"

    @patch("app.command_handlers.send_telegram")
    def test_em_dash_now_ack_includes_priority(self, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("\u2014now urgent fix")

        ack = mock_send.call_args[0][0]
        assert "priority" in ack

    @patch("app.command_handlers.send_telegram")
    def test_em_dash_now_stripped_from_stored_text(self, mock_send, tmp_path):
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        with patch("app.command_handlers.MISSIONS_FILE", missions):
            from app.command_handlers import handle_mission
            handle_mission("\u2014now deploy hotfix")

        content = missions.read_text()
        assert "\u2014now" not in content
        assert "--now" not in content
        assert "- deploy hotfix" in content
