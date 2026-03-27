"""Integration tests for passive mode in iteration_manager."""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")

from app.iteration_manager import _check_passive, plan_iteration

PROJECTS_LIST = [
    ("koan", "/path/to/koan"),
    ("backend", "/path/to/backend"),
]


@pytest.fixture
def instance_dir(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "journal").mkdir()
    (inst / "memory" / "global").mkdir(parents=True)
    (inst / "memory" / "projects").mkdir(parents=True)
    return inst


@pytest.fixture
def koan_root(tmp_path):
    root = tmp_path / "koan-root"
    root.mkdir()
    return root


@pytest.fixture
def usage_state(tmp_path):
    return tmp_path / "usage_state.json"


class TestCheckPassive:
    """Test _check_passive helper."""

    def test_returns_none_when_not_passive(self, koan_root):
        assert _check_passive(str(koan_root)) is None

    def test_returns_state_when_passive(self, koan_root):
        from app.passive_manager import create_passive

        create_passive(str(koan_root))
        state = _check_passive(str(koan_root))
        assert state is not None


class TestPlanIterationPassive:
    """Test that plan_iteration returns passive_wait when passive mode is active."""

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix auth bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_passive_blocks_mission_execution(
        self, mock_refresh, mock_pick, instance_dir, koan_root, usage_state
    ):
        """Even with a pending mission, passive mode returns passive_wait."""
        from app.passive_manager import create_passive

        create_passive(str(koan_root))
        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20%\n"
        )

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "passive_wait"
        assert result["passive_remaining"] == "indefinite"
        assert result["mission_title"] == ""  # mission not started

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    def test_passive_blocks_autonomous_mode(
        self, mock_refresh, mock_pick, instance_dir, koan_root, usage_state
    ):
        """With no missions, passive mode still returns passive_wait (no exploration)."""
        from app.passive_manager import create_passive

        create_passive(str(koan_root))
        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20%\n"
        )

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "passive_wait"

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix auth bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_not_passive_allows_mission(
        self, mock_refresh, mock_pick, instance_dir, koan_root, usage_state
    ):
        """Without passive mode, missions proceed normally."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20%\n"
        )

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "mission"
        assert result["mission_title"] == "Fix auth bug"

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_passive_timed_shows_remaining(
        self, mock_refresh, mock_pick, instance_dir, koan_root, usage_state
    ):
        """Timed passive shows remaining time in result."""
        from app.passive_manager import create_passive

        create_passive(str(koan_root), duration=7200)
        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20%\n"
        )

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "passive_wait"
        assert "h" in result["passive_remaining"]
