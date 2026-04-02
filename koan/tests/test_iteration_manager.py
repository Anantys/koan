"""Tests for app.iteration_manager — per-iteration planning."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")

from app.iteration_manager import (
    AutonomousDecision,
    FilterResult,
    _MODE_DOWNGRADE,
    _check_focus,
    _check_schedule,
    _decide_autonomous_action,
    _downgrade_if_unaffordable,
    _fallback_mission_extract,
    _filter_exploration_projects,
    _get_known_project_names,
    _get_project_by_index,
    _get_usage_decision,
    _inject_recurring,
    _make_result,
    _pick_mission,
    _refresh_usage,
    _resolve_project_path,
    _select_random_exploration_project,
    _should_contemplate,
    plan_iteration,
)
from app.loop_manager import resolve_focus_area


# === Helper fixtures ===


@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory."""
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "journal").mkdir()
    (inst / "memory" / "global").mkdir(parents=True)
    (inst / "memory" / "projects").mkdir(parents=True)
    return inst


@pytest.fixture
def koan_root(tmp_path):
    """Create a KOAN_ROOT directory."""
    root = tmp_path / "koan-root"
    root.mkdir()
    return root


@pytest.fixture
def usage_state(tmp_path):
    """Create a usage state file path."""
    return tmp_path / "usage_state.json"


PROJECTS_STR = "koan:/path/to/koan;backend:/path/to/backend;webapp:/path/to/webapp"
PROJECTS_LIST = [("koan", "/path/to/koan"), ("backend", "/path/to/backend"), ("webapp", "/path/to/webapp")]


# === Tests: _resolve_project_path ===


class TestResolveProjectPath:

    def test_finds_existing_project(self):
        assert _resolve_project_path("koan", PROJECTS_LIST) == ("koan", "/path/to/koan")
        assert _resolve_project_path("backend", PROJECTS_LIST) == ("backend", "/path/to/backend")
        assert _resolve_project_path("webapp", PROJECTS_LIST) == ("webapp", "/path/to/webapp")

    def test_returns_none_for_unknown(self):
        assert _resolve_project_path("unknown", PROJECTS_LIST) is None

    def test_empty_projects_list(self):
        assert _resolve_project_path("koan", []) is None

    def test_single_project(self):
        assert _resolve_project_path("only", [("only", "/single/path")]) == ("only", "/single/path")

    def test_case_insensitive_match(self):
        """Project name matching should be case-insensitive."""
        assert _resolve_project_path("Koan", PROJECTS_LIST) == ("koan", "/path/to/koan")
        assert _resolve_project_path("BACKEND", PROJECTS_LIST) == ("backend", "/path/to/backend")
        assert _resolve_project_path("WebApp", PROJECTS_LIST) == ("webapp", "/path/to/webapp")


class TestGetProjectByIndex:

    def test_first_project(self):
        name, path = _get_project_by_index(PROJECTS_LIST, 0)
        assert name == "koan"
        assert path == "/path/to/koan"

    def test_second_project(self):
        name, path = _get_project_by_index(PROJECTS_LIST, 1)
        assert name == "backend"
        assert path == "/path/to/backend"

    def test_index_clamped_high(self):
        name, path = _get_project_by_index(PROJECTS_LIST, 99)
        assert name == "webapp"  # Last project

    def test_index_clamped_low(self):
        name, path = _get_project_by_index(PROJECTS_LIST, -1)
        assert name == "koan"  # First project

    def test_empty_projects(self):
        name, path = _get_project_by_index([], 0)
        assert name == "default"


class TestGetKnownProjectNames:

    def test_extracts_sorted_names(self):
        names = _get_known_project_names(PROJECTS_LIST)
        assert names == ["backend", "koan", "webapp"]

    def test_single_project(self):
        names = _get_known_project_names([("solo", "/path")])
        assert names == ["solo"]

    def test_empty_list(self):
        names = _get_known_project_names([])
        assert names == []


# === Tests: resolve_focus_area ===


class TestResolveFocusArea:

    def test_mission_mode(self):
        assert resolve_focus_area("deep", has_mission=True) == "Execute assigned mission"

    def test_review_mode(self):
        result = resolve_focus_area("review", has_mission=False)
        assert "review" in result.lower() or "READ-ONLY" in result

    def test_implement_mode(self):
        result = resolve_focus_area("implement", has_mission=False)
        assert "implementation" in result.lower() or "implement" in result.lower()

    def test_deep_mode(self):
        result = resolve_focus_area("deep", has_mission=False)
        assert "deep" in result.lower() or "refactoring" in result.lower()

    def test_wait_mode(self):
        result = resolve_focus_area("wait", has_mission=False)
        assert "pause" in result.lower() or "exhausted" in result.lower()

    def test_unknown_mode(self):
        result = resolve_focus_area("unknown", has_mission=False)
        assert "General" in result


# === Tests: _refresh_usage ===


class TestRefreshUsage:

    @patch("app.usage_estimator.cmd_refresh")
    def test_refreshes_on_first_run(self, mock_refresh, tmp_path):
        """Count=0 (first run or after auto-resume) must still refresh.

        Critical for the budget exhaustion fix: after auto-resume, count
        resets to 0 but stale usage.md must be cleared.
        """
        state = tmp_path / "usage_state.json"
        usage_md = tmp_path / "usage.md"
        _refresh_usage(state, usage_md, count=0)
        mock_refresh.assert_called_once_with(state, usage_md)

    @patch("app.usage_estimator.cmd_refresh")
    def test_calls_refresh_after_first_run(self, mock_refresh, tmp_path):
        state = tmp_path / "usage_state.json"
        usage_md = tmp_path / "usage.md"
        _refresh_usage(state, usage_md, count=1)
        mock_refresh.assert_called_once_with(state, usage_md)

    def test_handles_refresh_error_gracefully(self, tmp_path):
        """Errors in refresh don't crash the iteration."""
        with patch("app.usage_estimator.cmd_refresh", side_effect=OSError("boom")):
            # Should not raise
            _refresh_usage(tmp_path / "state", tmp_path / "usage.md", count=1)


# === Tests: _downgrade_if_unaffordable ===


class TestDowngradeIfUnaffordable:

    def _make_tracker(self, tmp_path, session_pct, runs):
        """Create a UsageTracker with known session usage."""
        from app.usage_tracker import UsageTracker
        usage_md = tmp_path / "usage.md"
        usage_md.write_text(
            f"Session (5hr) : {session_pct}% (reset in 2h)\n"
            f"Weekly (7 day) : 10% (Resets in 5d)\n"
        )
        return UsageTracker(usage_md, runs)

    def test_no_downgrade_when_affordable(self, tmp_path):
        """Deep mode stays deep when budget is ample."""
        tracker = self._make_tracker(tmp_path, session_pct=20, runs=5)
        assert _downgrade_if_unaffordable(tracker, "deep") == "deep"

    def test_deep_downgrades_to_implement(self, tmp_path):
        """Deep is too expensive but implement fits."""
        # 80% used, 10% safety → 10% remaining
        # 10 runs at 80% → avg cost 8%/run → deep=16% > 10%, implement=8% ≤ 10%
        tracker = self._make_tracker(tmp_path, session_pct=80, runs=10)
        assert _downgrade_if_unaffordable(tracker, "deep") == "implement"

    def test_deep_downgrades_to_review(self, tmp_path):
        """Both deep and implement too expensive, review fits."""
        # 87% used → 3% remaining, 20 runs → avg 4.35%/run
        # deep=8.7%, implement=4.35% > 3%, review=2.175% ≤ 3%
        tracker = self._make_tracker(tmp_path, session_pct=87, runs=20)
        assert _downgrade_if_unaffordable(tracker, "deep") == "review"

    def test_all_unaffordable_falls_to_wait(self, tmp_path):
        """When nothing is affordable, mode becomes wait."""
        # 95% used → -5% remaining (clamped to 0)
        tracker = self._make_tracker(tmp_path, session_pct=95, runs=5)
        assert _downgrade_if_unaffordable(tracker, "deep") == "wait"

    def test_review_stays_review(self, tmp_path):
        """Review mode with enough budget stays review."""
        tracker = self._make_tracker(tmp_path, session_pct=50, runs=10)
        assert _downgrade_if_unaffordable(tracker, "review") == "review"

    def test_wait_passthrough(self, tmp_path):
        """Wait mode is not in downgrade chain — passes through unchanged."""
        tracker = self._make_tracker(tmp_path, session_pct=95, runs=5)
        assert _downgrade_if_unaffordable(tracker, "wait") == "wait"

    def test_mode_downgrade_chain(self):
        """Verify the downgrade chain is complete."""
        assert _MODE_DOWNGRADE == {
            "deep": "implement",
            "implement": "review",
            "review": "wait",
        }


# === Tests: _get_usage_decision ===


class TestGetUsageDecision:

    def test_returns_fallback_on_missing_file(self, tmp_path):
        result = _get_usage_decision(tmp_path / "nonexistent.md", 0, PROJECTS_STR)
        assert result["mode"] in ("wait", "review", "implement", "deep")
        assert isinstance(result["available_pct"], int)
        assert isinstance(result["display_lines"], list)

    def test_parses_usage_file(self, tmp_path):
        usage_md = tmp_path / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 2h30m)\n"
            "Weekly (7 day) : 20% (Resets in 5d)\n"
        )
        result = _get_usage_decision(usage_md, 3, PROJECTS_STR)
        assert result["mode"] == "deep"  # 70% available (100-30-safety)
        assert result["available_pct"] >= 50
        assert len(result["display_lines"]) == 2
        assert "Session" in result["display_lines"][0]
        assert "Weekly" in result["display_lines"][1]
        assert result.get("tracker_error") is None

    def test_high_usage_returns_wait(self, tmp_path):
        usage_md = tmp_path / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 97% (reset in 1h)\n"
            "Weekly (7 day) : 50% (Resets in 3d)\n"
        )
        result = _get_usage_decision(usage_md, 5, PROJECTS_STR)
        assert result["mode"] == "wait"

    @patch("app.usage_tracker.UsageTracker", side_effect=ValueError("tracker crash"))
    def test_tracker_error_falls_back_to_review_mode(self, mock_tracker, tmp_path):
        """When the usage tracker crashes, fallback to 'review' (read-only) not 'implement'."""
        usage_md = tmp_path / "usage.md"
        usage_md.write_text("Session (5hr) : 50%\n")
        result = _get_usage_decision(usage_md, 3, PROJECTS_STR)
        assert result["mode"] == "review"
        assert result["available_pct"] == 0
        assert "safe fallback" in result["reason"].lower() or "tracker error" in result["reason"].lower()
        assert result["tracker_error"] == "tracker crash"

    @patch("app.usage_tracker.UsageTracker", side_effect=ImportError("missing module"))
    def test_tracker_error_surfaces_import_error(self, mock_tracker, tmp_path):
        """ImportError in tracker also populates tracker_error for operator notification."""
        usage_md = tmp_path / "usage.md"
        usage_md.write_text("Session (5hr) : 50%\n")
        result = _get_usage_decision(usage_md, 3, PROJECTS_STR)
        assert result["mode"] == "review"
        assert result["tracker_error"] == "missing module"

    def test_medium_usage_returns_implement(self, tmp_path):
        usage_md = tmp_path / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 60% (reset in 2h)\n"
            "Weekly (7 day) : 40% (Resets in 4d)\n"
        )
        result = _get_usage_decision(usage_md, 3, PROJECTS_STR)
        assert result["mode"] == "implement"  # 30% available

    def test_can_afford_run_downgrades_mode(self, tmp_path):
        """When decide_mode picks deep but can_afford_run says no, mode is downgraded."""
        usage_md = tmp_path / "usage.md"
        # 50% used, 2 runs → avg cost 25%/run → deep=50% > 40% available → downgrade
        # decide_mode returns "deep" (40% available ≥ 40 threshold)
        # but can_afford_run("deep") = 25*2.0=50 > 40 → downgrade to implement
        # can_afford_run("implement") = 25*1.0=25 ≤ 40 → implement fits
        usage_md.write_text(
            "Session (5hr) : 50% (reset in 3h)\n"
            "Weekly (7 day) : 10% (Resets in 5d)\n"
        )
        result = _get_usage_decision(usage_md, 2, PROJECTS_STR)
        assert result["mode"] == "implement"


# === Tests: _inject_recurring ===


class TestInjectRecurring:

    def test_returns_empty_when_no_recurring_file(self, instance_dir):
        result = _inject_recurring(instance_dir)
        assert result == []

    @patch("app.recurring.check_and_inject", return_value=["test daily task"])
    def test_returns_injected_descriptions(self, mock_inject, instance_dir):
        (instance_dir / "recurring.json").write_text("{}")
        result = _inject_recurring(instance_dir)
        assert result == ["test daily task"]

    def test_handles_error_gracefully(self, instance_dir):
        (instance_dir / "recurring.json").write_text("{}")
        with patch("app.recurring.check_and_inject", side_effect=OSError("boom")):
            result = _inject_recurring(instance_dir)
            assert result == []


# === Tests: _fallback_mission_extract ===


class TestFallbackMissionExtract:

    def test_no_missions_file(self, tmp_path):
        """Returns (None, None) when missions.md doesn't exist."""
        inst = tmp_path / "instance"
        inst.mkdir()
        project, title = _fallback_mission_extract(inst, PROJECTS_STR, "test context")
        assert project is None
        assert title is None

    def test_no_pending_missions(self, tmp_path):
        """Returns (None, None) when no pending missions."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text("# Missions\n\n## Pending\n\n## Done\n")
        project, title = _fallback_mission_extract(inst, PROJECTS_STR, "test context")
        assert project is None
        assert title is None

    @patch("app.pick_mission.fallback_extract", return_value=("koan", "Fix bug"))
    def test_extracts_pending_mission(self, mock_extract, tmp_path):
        """Extracts mission when pending count > 0."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n- [project:koan] Fix bug\n\n## Done\n"
        )
        project, title = _fallback_mission_extract(inst, PROJECTS_STR, "test context")
        assert project == "koan"
        assert title == "Fix bug"

    @patch("app.pick_mission.fallback_extract", return_value=(None, None))
    def test_fallback_extract_fails(self, mock_extract, tmp_path):
        """Returns (None, None) when fallback_extract fails to find a mission."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n- [project:koan] Fix bug\n\n## Done\n"
        )
        project, title = _fallback_mission_extract(inst, PROJECTS_STR, "test context")
        assert project is None
        assert title is None

    @patch("app.pick_mission.fallback_extract", side_effect=OSError("boom"))
    def test_handles_import_error(self, mock_extract, tmp_path):
        """Returns (None, None) on exception from fallback_extract."""
        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n- [project:koan] Fix bug\n\n## Done\n"
        )
        project, title = _fallback_mission_extract(inst, PROJECTS_STR, "test context")
        assert project is None
        assert title is None


# === Tests: _make_result ===


class TestMakeResult:

    def test_returns_all_keys(self):
        """Result dict contains all required keys."""
        result = _make_result(
            action="mission",
            project_name="koan",
            project_path="/path/to/koan",
            mission_title="Fix the bug",
            autonomous_mode="implement",
            focus_area="code quality",
            available_pct=50,
            decision_reason="medium budget",
            display_lines=["line1"],
            recurring_injected=[],
        )
        expected_keys = {
            "action", "project_name", "project_path", "mission_title",
            "autonomous_mode", "focus_area", "available_pct", "decision_reason",
            "display_lines", "recurring_injected", "focus_remaining",
            "passive_remaining", "schedule_mode", "error", "tracker_error",
        }
        assert set(result.keys()) == expected_keys

    def test_defaults(self):
        """Default values are applied correctly."""
        result = _make_result(
            action="autonomous",
            project_name="koan",
            autonomous_mode="deep",
            available_pct=80,
            decision_reason="high budget",
            display_lines=[],
            recurring_injected=[],
        )
        assert result["project_path"] == ""
        assert result["mission_title"] == ""
        assert result["focus_area"] == ""
        assert result["focus_remaining"] is None
        assert result["schedule_mode"] == "normal"
        assert result["error"] is None

    def test_overrides(self):
        """Custom values override defaults."""
        result = _make_result(
            action="focus_wait",
            project_name="koan",
            project_path="/koan",
            autonomous_mode="implement",
            available_pct=30,
            decision_reason="focus active",
            display_lines=[],
            recurring_injected=[],
            focus_remaining="2h 30m",
            schedule_mode="work",
            error="something went wrong",
        )
        assert result["focus_remaining"] == "2h 30m"
        assert result["schedule_mode"] == "work"
        assert result["error"] == "something went wrong"

    def test_none_project_path_becomes_empty(self):
        """None project_path is coerced to empty string."""
        result = _make_result(
            action="error",
            project_name="unknown",
            project_path=None,
            autonomous_mode="implement",
            available_pct=50,
            decision_reason="test",
            display_lines=[],
            recurring_injected=[],
        )
        assert result["project_path"] == ""


# === Tests: _pick_mission ===


class TestPickMission:

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix the bug")
    def test_returns_project_and_title(self, mock_pick):
        project, title = _pick_mission(Path("/instance"), PROJECTS_STR, 1, "deep", "")
        assert project == "koan"
        assert title == "Fix the bug"

    @patch("app.pick_mission.pick_mission", return_value="")
    def test_returns_none_for_autonomous(self, mock_pick):
        project, title = _pick_mission(Path("/instance"), PROJECTS_STR, 1, "deep", "")
        assert project is None
        assert title is None

    @patch("app.pick_mission.pick_mission", side_effect=OSError("boom"))
    def test_handles_error_gracefully(self, mock_pick):
        project, title = _pick_mission(Path("/instance"), PROJECTS_STR, 1, "deep", "")
        assert project is None
        assert title is None

    @patch("app.pick_mission.pick_mission", return_value="backend:Deploy v2.1")
    def test_parses_colon_in_title(self, mock_pick):
        project, title = _pick_mission(Path("/instance"), PROJECTS_STR, 1, "deep", "")
        assert project == "backend"
        assert title == "Deploy v2.1"


# === Tests: _should_contemplate ===


class TestShouldContemplate:

    @patch("random.randint", return_value=5)
    def test_contemplates_when_roll_succeeds(self, mock_rand):
        assert _should_contemplate("deep", False, 10) is True

    @patch("random.randint", return_value=15)
    def test_skips_when_roll_fails(self, mock_rand):
        assert _should_contemplate("deep", False, 10) is False

    def test_skips_in_wait_mode(self):
        assert _should_contemplate("wait", False, 10) is False

    def test_skips_in_review_mode(self):
        assert _should_contemplate("review", False, 10) is False

    def test_skips_when_focus_active(self):
        assert _should_contemplate("deep", True, 50) is False

    @patch("random.randint", return_value=5)
    def test_schedule_deep_hours_boosts_chance(self, mock_rand):
        """During deep hours, contemplative chance is tripled."""
        from app.schedule_manager import ScheduleState
        schedule = ScheduleState(in_deep_hours=True, in_work_hours=False)
        # base chance 10 → adjusted to 30, roll of 5 < 30 → True
        assert _should_contemplate("deep", False, 10, schedule) is True

    @patch("random.randint", return_value=5)
    def test_schedule_work_hours_zeroes_chance(self, mock_rand):
        """During work hours, contemplative chance is zero."""
        from app.schedule_manager import ScheduleState
        schedule = ScheduleState(in_deep_hours=False, in_work_hours=True)
        # chance becomes 0, roll of 5 >= 0 → False
        assert _should_contemplate("deep", False, 10, schedule) is False

    @patch("random.randint", return_value=5)
    def test_schedule_none_unchanged(self, mock_rand):
        """When schedule_state is None, chance is unchanged."""
        # base chance 10, roll of 5 < 10 → True
        assert _should_contemplate("deep", False, 10, None) is True


# === Tests: _check_focus ===


class TestCheckFocus:

    def test_returns_none_when_module_missing(self):
        """When focus_manager isn't available, returns None gracefully."""
        # _check_focus has try/except — if focus_manager doesn't exist, returns None
        with patch.dict("sys.modules", {"app.focus_manager": None}):
            assert _check_focus("/koan-root") is None

    def test_returns_none_when_not_active(self):
        """When focus_manager's check_focus returns None, so does _check_focus."""
        mock_module = MagicMock()
        mock_module.check_focus.return_value = None
        with patch.dict("sys.modules", {"app.focus_manager": mock_module}):
            assert _check_focus("/koan-root") is None

    def test_returns_state_when_active(self):
        """When focus_manager's check_focus returns a state, _check_focus returns it."""
        mock_state = MagicMock()
        mock_module = MagicMock()
        mock_module.check_focus.return_value = mock_state
        with patch.dict("sys.modules", {"app.focus_manager": mock_module}):
            assert _check_focus("/koan-root") is mock_state


# === Tests: _check_schedule ===


class TestCheckSchedule:

    def test_returns_state_when_configured(self):
        """Returns a ScheduleState when schedule is configured."""
        from app.schedule_manager import ScheduleState
        mock_state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        with patch("app.schedule_manager.get_current_schedule", return_value=mock_state):
            result = _check_schedule()
            assert result is not None
            assert result.mode == "deep"

    def test_returns_normal_state_when_unconfigured(self):
        """Returns state (normal) when schedule has no windows configured."""
        from app.schedule_manager import ScheduleState
        mock_state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        with patch("app.schedule_manager.get_current_schedule", return_value=mock_state):
            result = _check_schedule()
            assert result is not None
            assert result.mode == "normal"

    def test_returns_none_on_import_error(self):
        """Returns None gracefully when module is unavailable."""
        with patch("app.schedule_manager.get_current_schedule", side_effect=ImportError):
            result = _check_schedule()
            assert result is None


# === Tests: plan_iteration (integration) ===


class TestPlanIteration:

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix auth bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_mission_mode(self, mock_refresh, mock_pick, instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

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
        assert result["project_name"] == "koan"
        assert result["project_path"] == "/path/to/koan"
        assert result["mission_title"] == "Fix auth bug"
        assert result["error"] is None
        assert result["tracker_error"] is None

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix auth bug")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.usage_tracker.UsageTracker", side_effect=ValueError("budget DB corrupted"))
    def test_tracker_error_propagates_to_plan_result(self, mock_tracker, mock_refresh, mock_pick,
                                                      instance_dir, koan_root, usage_state):
        """When UsageTracker crashes, tracker_error surfaces in the plan result for notification."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 50%\n")

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
        assert result["autonomous_mode"] == "review"
        assert result["tracker_error"] == "budget DB corrupted"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule", return_value=None)
    @patch("random.randint", return_value=99)  # No contemplation
    def test_autonomous_mode(self, mock_rand, mock_schedule, mock_focus, mock_refresh, mock_pick,
                             instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "autonomous"
        assert result["mission_title"] == ""
        assert result["autonomous_mode"] == "deep"
        assert result["error"] is None

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule", return_value=None)
    @patch("random.randint", return_value=3)  # Contemplation triggers (< 10%)
    def test_contemplative_mode(self, mock_rand, mock_schedule, mock_focus, mock_refresh, mock_pick,
                                instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "contemplative"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus")
    def test_focus_wait_mode(self, mock_focus, mock_refresh, mock_pick,
                             instance_dir, koan_root, usage_state):
        mock_state = MagicMock()
        mock_state.remaining_display.return_value = "2h remaining"
        mock_focus.return_value = mock_state

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "focus_wait"
        assert result["focus_remaining"] == "2h remaining"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("random.randint", return_value=99)  # No contemplation
    def test_schedule_wait_mode(self, mock_rand, mock_schedule, mock_focus,
                                mock_refresh, mock_pick,
                                instance_dir, koan_root, usage_state):
        """When work_hours are active and no mission, returns schedule_wait."""
        from app.schedule_manager import ScheduleState
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=True)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "schedule_wait"
        assert result["schedule_mode"] == "work"

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix auth bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_schedule_does_not_block_missions(self, mock_refresh, mock_pick,
                                              instance_dir, koan_root, usage_state):
        """Work hours schedule doesn't block queued missions."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        # Even though work hours would suppress exploration, missions still run
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

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    def test_wait_pause_mode(self, mock_focus, mock_refresh, mock_pick,
                             instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 97% (reset in 1h)\nWeekly (7 day) : 50% (Resets in 3d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=5,
            count=4,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "wait_pause"
        assert result["autonomous_mode"] == "wait"

    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    def test_wait_mode_skips_exploration_filter(
        self, mock_focus, mock_refresh, mock_pick, mock_filter,
        instance_dir, koan_root, usage_state,
    ):
        """Wait mode should return wait_pause without calling
        _filter_exploration_projects — avoids wasted gh API calls."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 97% (reset in 1h)\nWeekly (7 day) : 50% (Resets in 3d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=5,
            count=4,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "wait_pause"
        assert result["autonomous_mode"] == "wait"
        # The key assertion: _filter_exploration_projects must NOT be called
        mock_filter.assert_not_called()

    @patch("app.pick_mission.pick_mission", return_value="unknown_project:Fix thing")
    @patch("app.usage_estimator.cmd_refresh")
    def test_unknown_project_error(self, mock_refresh, mock_pick,
                                   instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "error"
        assert "unknown_project" in result["error"]
        assert "backend" in result["error"]
        assert "koan" in result["error"]

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix it")
    @patch("app.usage_estimator.cmd_refresh")
    def test_first_run_always_refreshes_usage(self, mock_refresh, mock_pick,
                                               instance_dir, koan_root, usage_state):
        """Count=0 must still refresh — critical after auto-resume."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=1,
            count=0,
            projects=PROJECTS_LIST,
            last_project="",
            usage_state_path=str(usage_state),
        )

        mock_refresh.assert_called_once()

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix it")
    @patch("app.usage_estimator.cmd_refresh")
    def test_recurring_injection_runs(self, mock_refresh, mock_pick,
                                      instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        # Create a recurring.json to trigger injection
        (instance_dir / "recurring.json").write_text("{}")

        with patch("app.recurring.check_and_inject", return_value=["daily: health check"]) as mock_inject:
            result = plan_iteration(
                instance_dir=str(instance_dir),
                koan_root=str(koan_root),
                run_num=2,
                count=1,
                projects=PROJECTS_LIST,
                last_project="koan",
                usage_state_path=str(usage_state),
            )

        assert result["recurring_injected"] == ["daily: health check"]
        mock_inject.assert_called_once()

    @patch("app.pick_mission.pick_mission", return_value="koan:Task with: colon in title")
    @patch("app.usage_estimator.cmd_refresh")
    def test_mission_title_with_colon(self, mock_refresh, mock_pick,
                                      instance_dir, koan_root, usage_state):
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["mission_title"] == "Task with: colon in title"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_focus_checked_once_for_autonomous(self, mock_rand, mock_focus,
                                               mock_refresh, mock_pick,
                                               instance_dir, koan_root, usage_state):
        """Focus is checked exactly once (not twice for contemplate + focus_wait)."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        mock_focus.assert_called_once()


# === Tests: _decide_autonomous_action ===


class TestDecideAutonomousAction:
    """Tests for the extracted autonomous decision priority chain."""

    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=3)  # < 10 → contemplation triggers
    def test_contemplative_wins_first(self, mock_rand, mock_focus):
        """Contemplative has highest priority in the chain."""
        result = _decide_autonomous_action("deep", "/tmp/root", None, 10)
        assert result.action == "contemplative"
        assert result.focus_remaining is None

    @patch("app.iteration_manager._check_focus")
    @patch("random.randint", return_value=99)  # No contemplation
    def test_focus_wait_when_focus_active(self, mock_rand, mock_focus):
        """Focus wait triggers when focus is active and contemplation skipped."""
        mock_state = MagicMock()
        mock_state.remaining_display.return_value = "3h remaining"
        mock_focus.return_value = mock_state

        result = _decide_autonomous_action("deep", "/tmp/root", None, 10)
        assert result.action == "focus_wait"
        assert result.focus_remaining == "3h remaining"

    @patch("app.iteration_manager._check_focus")
    @patch("random.randint", return_value=99)
    def test_focus_remaining_unknown_on_error(self, mock_rand, mock_focus):
        """Focus remaining falls back to 'unknown' on display error."""
        mock_state = MagicMock()
        mock_state.remaining_display.side_effect = ValueError("bad state")
        mock_focus.return_value = mock_state

        result = _decide_autonomous_action("deep", "/tmp/root", None, 10)
        assert result.action == "focus_wait"
        assert result.focus_remaining == "unknown"

    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_schedule_wait_during_work_hours(self, mock_rand, mock_focus):
        """Schedule wait triggers during work hours when no focus."""
        from app.schedule_manager import ScheduleState
        schedule = ScheduleState(in_deep_hours=False, in_work_hours=True)

        result = _decide_autonomous_action("deep", "/tmp/root", schedule, 10)
        assert result.action == "schedule_wait"
        assert result.focus_remaining is None

    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_autonomous_default(self, mock_rand, mock_focus):
        """Autonomous is the default when no higher-priority action matches."""
        result = _decide_autonomous_action("deep", "/tmp/root", None, 10)
        assert result.action == "autonomous"
        assert result.focus_remaining is None

    @patch("app.iteration_manager._check_focus")
    @patch("random.randint", return_value=3)  # Would contemplate if focus inactive
    def test_focus_suppresses_contemplation(self, mock_rand, mock_focus):
        """Focus active suppresses contemplation (_should_contemplate checks focus)."""
        mock_state = MagicMock()
        mock_state.remaining_display.return_value = "1h"
        mock_focus.return_value = mock_state

        result = _decide_autonomous_action("deep", "/tmp/root", None, 10)
        assert result.action == "focus_wait"

    @patch("app.iteration_manager._check_focus")
    @patch("random.randint", return_value=99)
    def test_focus_beats_schedule(self, mock_rand, mock_focus):
        """Focus wait wins over schedule wait when both would trigger."""
        from app.schedule_manager import ScheduleState
        schedule = ScheduleState(in_deep_hours=False, in_work_hours=True)
        mock_state = MagicMock()
        mock_state.remaining_display.return_value = "2h"
        mock_focus.return_value = mock_state

        result = _decide_autonomous_action("deep", "/tmp/root", schedule, 10)
        assert result.action == "focus_wait"

    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_returns_namedtuple(self, mock_rand, mock_focus):
        """Result is an AutonomousDecision namedtuple."""
        result = _decide_autonomous_action("deep", "/tmp/root", None, 10)
        assert isinstance(result, AutonomousDecision)
        assert result == AutonomousDecision(action="autonomous", focus_remaining=None)


# === Tests: Deep hours mode capping ===


class TestDeepHoursModeCap:
    """Tests for deep_hours schedule capping the autonomous mode."""

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("0-8", ""))
    @patch("random.randint", return_value=99)  # No contemplation
    def test_deep_capped_outside_deep_hours(
        self, mock_rand, mock_sched_config, mock_schedule, mock_focus,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """Budget 'deep' is capped to 'implement' when outside configured deep_hours."""
        from app.schedule_manager import ScheduleState
        # 11 AM: outside deep_hours 0-8
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 20% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "autonomous"
        assert result["autonomous_mode"] == "implement"
        assert "capped from deep" in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("0-8", ""))
    @patch("random.randint", return_value=99)
    def test_deep_allowed_during_deep_hours(
        self, mock_rand, mock_sched_config, mock_schedule, mock_focus,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """Budget 'deep' stays 'deep' when inside configured deep_hours."""
        from app.schedule_manager import ScheduleState
        # 3 AM: inside deep_hours 0-8
        mock_schedule.return_value = ScheduleState(in_deep_hours=True, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 20% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "autonomous"
        assert result["autonomous_mode"] == "deep"
        assert "capped" not in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("", ""))
    @patch("random.randint", return_value=99)
    def test_deep_allowed_when_no_deep_hours_configured(
        self, mock_rand, mock_sched_config, mock_schedule, mock_focus,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """Without deep_hours config, 'deep' budget mode is uncapped (backward compat)."""
        from app.schedule_manager import ScheduleState
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 20% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["autonomous_mode"] == "deep"
        assert "capped" not in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="koan:Fix auth bug")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("0-8", ""))
    def test_cap_applies_to_mission_mode_too(
        self, mock_sched_config, mock_schedule,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """Mode cap applies even when a mission is assigned (affects prompt)."""
        from app.schedule_manager import ScheduleState
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 20% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

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
        assert result["autonomous_mode"] == "implement"
        assert "capped from deep" in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("0-8", ""))
    @patch("random.randint", return_value=99)
    def test_cap_reason_includes_schedule_context(
        self, mock_rand, mock_sched_config, mock_schedule, mock_focus,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """Capped decision_reason explains the schedule constraint."""
        from app.schedule_manager import ScheduleState
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 20% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert "outside deep_hours schedule" in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("0-8", ""))
    @patch("random.randint", return_value=99)
    def test_implement_mode_not_capped(
        self, mock_rand, mock_sched_config, mock_schedule, mock_focus,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """Implement mode (from budget) is not affected by schedule cap."""
        from app.schedule_manager import ScheduleState
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        # 55% session + 10% margin → 35% remaining → implement mode
        # Use count=10 so avg cost (5.5%/run) stays affordable for implement
        usage_md.write_text("Session (5hr) : 55% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=10,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["autonomous_mode"] == "implement"
        assert "capped" not in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule")
    @patch("app.schedule_manager.get_schedule_config", return_value=("0-8", ""))
    @patch("random.randint", return_value=99)
    def test_schedule_mode_reflects_cap(
        self, mock_rand, mock_sched_config, mock_schedule, mock_focus,
        mock_refresh, mock_pick, instance_dir, koan_root, usage_state,
    ):
        """The schedule_mode in result reflects the actual schedule state."""
        from app.schedule_manager import ScheduleState
        mock_schedule.return_value = ScheduleState(in_deep_hours=False, in_work_hours=False)

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 20% (reset in 3h)\nWeekly (7 day) : 10% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["schedule_mode"] == "normal"


# === Tests: _filter_exploration_projects ===


class TestFilterExplorationProjects:

    def test_returns_all_when_no_config(self, koan_root):
        """No projects.yaml → all projects returned (exploration enabled by default)."""
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        assert result.projects == PROJECTS_LIST
        assert result.pr_limited == []

    def test_filters_disabled_projects(self, koan_root):
        """Projects with exploration: false are excluded."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
  backend:
    path: /path/to/backend
    exploration: false
  webapp:
    path: /path/to/webapp
""")
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        names = [name for name, _ in result.projects]
        assert "koan" in names
        assert "webapp" in names
        assert "backend" not in names

    def test_returns_empty_when_all_disabled(self, koan_root):
        """All projects disabled → empty list."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    exploration: false
  backend:
    path: /path/to/backend
    exploration: false
  webapp:
    path: /path/to/webapp
    exploration: false
""")
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        assert result.projects == []

    def test_returns_all_when_all_enabled(self, koan_root):
        """All projects enabled → full list returned."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    exploration: true
  backend:
    path: /path/to/backend
  webapp:
    path: /path/to/webapp
""")
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        assert len(result.projects) == 3

    def test_graceful_fallback_on_invalid_yaml(self, koan_root):
        """Invalid YAML → returns all projects (graceful fallback)."""
        (koan_root / "projects.yaml").write_text("not: valid: [yaml")
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        assert result.projects == PROJECTS_LIST

    def test_defaults_section_applies(self, koan_root):
        """Defaults section exploration: false applies to all unless overridden."""
        (koan_root / "projects.yaml").write_text("""
defaults:
  exploration: false
projects:
  koan:
    path: /path/to/koan
    exploration: true
  backend:
    path: /path/to/backend
  webapp:
    path: /path/to/webapp
""")
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        names = [name for name, _ in result.projects]
        assert names == ["koan"]


    def test_config_load_error_logs_to_stderr(self, koan_root, capsys):
        """When load_projects_config raises, error is logged to stderr."""
        with patch("app.projects_config.load_projects_config", side_effect=ValueError("bad yaml")):
            result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        assert result.projects == PROJECTS_LIST  # Fail-open
        captured = capsys.readouterr()
        assert "bad yaml" in captured.err


# === Tests: _filter_exploration_projects with PR limits ===


class TestFilterExplorationProjectsPrLimit:

    def setup_method(self):
        """Clear the PR count cache between tests."""
        from app.github import _pr_count_cache
        _pr_count_cache.clear()

    @pytest.fixture(autouse=True)
    def _mock_batch(self):
        """Disable batch GraphQL so tests exercise the sequential fallback path."""
        with patch("app.github.batch_count_open_prs", return_value={}):
            yield

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=3)
    def test_under_limit_included(self, mock_count, mock_user, koan_root):
        """Project under PR limit is included."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        assert result.pr_limited == []

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=10)
    def test_at_limit_excluded(self, mock_count, mock_user, koan_root):
        """Project at PR limit is excluded."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=15)
    def test_over_limit_excluded(self, mock_count, mock_user, koan_root):
        """Project over PR limit is excluded."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=-1)
    def test_gh_error_treats_as_pr_limited(self, mock_count, mock_user, koan_root):
        """gh failure returns -1 → conservative, project treated as PR-limited."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert "koan" in result.pr_limited

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_no_github_url_included(self, mock_count, mock_user, koan_root):
        """Project with max_open_prs but no github_url — no gh call made."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_open_prs: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        assert result.pr_limited == []
        mock_count.assert_not_called()

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_zero_limit_means_unlimited(self, mock_count, mock_user, koan_root):
        """max_open_prs: 0 means unlimited — no gh call made."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 0
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        mock_count.assert_not_called()

    @patch("app.github.get_gh_username", return_value="")
    @patch("app.github.count_open_prs")
    def test_no_author_skips_pr_checks(self, mock_count, mock_user, koan_root):
        """Empty author → all PR limit checks skipped, projects included."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        mock_count.assert_not_called()

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_mixed_projects(self, mock_count, mock_user, koan_root):
        """Mix of limited and unlimited projects."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
  backend:
    path: /path/to/backend
    github_url: owner/backend
    max_open_prs: 3
  webapp:
    path: /path/to/webapp
""")
        # koan: 4 open (under 5), backend: 3 open (at 3)
        mock_count.side_effect = lambda repo, author, **kw: (
            4 if "koan" in repo else 3
        )
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        names = [name for name, _ in result.projects]
        assert "koan" in names
        assert "webapp" in names  # No limit set
        assert "backend" not in names
        assert result.pr_limited == ["backend"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=10)
    def test_pr_limited_field_populated(self, mock_count, mock_user, koan_root):
        """pr_limited contains names of all PR-limited projects."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
  backend:
    path: /path/to/backend
    github_url: owner/backend
    max_open_prs: 3
  webapp:
    path: /path/to/webapp
    github_url: owner/webapp
    max_open_prs: 2
""")
        result = _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        assert result.projects == []
        assert sorted(result.pr_limited) == ["backend", "koan", "webapp"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=5)
    def test_exploration_false_checked_before_pr_limit(self, mock_count, mock_user, koan_root):
        """exploration: false is checked before PR limit — no gh call for disabled."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    exploration: false
    github_url: owner/koan
    max_open_prs: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == []
        mock_count.assert_not_called()

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=1)
    def test_defaults_section_max_open_prs(self, mock_count, mock_user, koan_root):
        """Defaults section max_open_prs applies to all projects."""
        (koan_root / "projects.yaml").write_text("""
defaults:
  max_open_prs: 1
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_github_urls_checked_for_pr_count(self, mock_count, mock_user, koan_root):
        """PRs are counted across all github_urls, not just primary github_url."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
    github_urls:
    - owner/koan
    - upstream/koan
""")
        # Fork has 1, upstream has 6 → total 7, over limit of 5
        mock_count.side_effect = lambda repo, author, **kw: (
            1 if repo == "owner/koan" else 6
        )
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_github_urls_under_limit_included(self, mock_count, mock_user, koan_root):
        """PRs summed across github_urls under limit → project included."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 10
    github_urls:
    - owner/koan
    - upstream/koan
""")
        # Fork has 2, upstream has 3 → total 5, under limit of 10
        mock_count.side_effect = lambda repo, author, **kw: (
            2 if repo == "owner/koan" else 3
        )
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        assert result.pr_limited == []

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_github_urls_only_no_primary(self, mock_count, mock_user, koan_root):
        """Only github_urls present (no github_url) → still checks PRs."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_open_prs: 3
    github_urls:
    - upstream/koan
""")
        mock_count.return_value = 5
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_github_urls_partial_error_uses_valid_counts(self, mock_count, mock_user, koan_root):
        """One URL errors (-1), another returns valid count → uses valid count."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 3
    github_urls:
    - owner/koan
    - upstream/koan
""")
        # Fork errors, upstream has 5
        mock_count.side_effect = lambda repo, author, **kw: (
            -1 if repo == "owner/koan" else 5
        )
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=-1)
    def test_github_urls_all_errors_treats_as_pr_limited(self, mock_count, mock_user, koan_root):
        """All github_urls return errors → conservative, project treated as PR-limited."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 3
    github_urls:
    - owner/koan
    - upstream/koan
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert "koan" in result.pr_limited

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs")
    def test_github_urls_deduped(self, mock_count, mock_user, koan_root):
        """Duplicate URLs in github_url + github_urls are deduplicated."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
    github_urls:
    - owner/koan
""")
        mock_count.return_value = 3
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        # Should only be called once due to dedup (set)
        assert mock_count.call_count == 1


# === Tests: _filter_exploration_projects with batch GraphQL path ===


class TestFilterExplorationProjectsBatchPath:
    """Tests that verify the batch GraphQL path in _filter_exploration_projects."""

    def setup_method(self):
        from app.github import _pr_count_cache
        _pr_count_cache.clear()

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.batch_count_open_prs")
    def test_batch_provides_counts(self, mock_batch, mock_user, koan_root):
        """When batch succeeds, no sequential fallback needed."""
        mock_batch.return_value = {"owner/koan": 3}
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        mock_batch.assert_called_once()

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.batch_count_open_prs")
    def test_batch_at_limit_excludes(self, mock_batch, mock_user, koan_root):
        """Batch reports count at limit → project excluded."""
        mock_batch.return_value = {"owner/koan": 10}
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.batch_count_open_prs")
    def test_batch_multiple_repos_summed(self, mock_batch, mock_user, koan_root):
        """Batch sums counts across multiple URLs for the same project."""
        mock_batch.return_value = {"owner/koan": 2, "upstream/koan": 4}
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
    github_urls:
    - owner/koan
    - upstream/koan
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        # 2 + 4 = 6, over limit of 5
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.cached_count_open_prs", return_value=8)
    @patch("app.github.batch_count_open_prs", return_value={})
    def test_batch_failure_falls_back_to_sequential(
        self, mock_batch, mock_cached, mock_user, koan_root,
    ):
        """When batch returns empty, falls back to cached_count_open_prs."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]
        mock_cached.assert_called_once_with("owner/koan", "koan-bot")

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.batch_count_open_prs")
    def test_batch_receives_all_repos(self, mock_batch, mock_user, koan_root):
        """Batch is called with deduplicated repos from all projects."""
        mock_batch.return_value = {
            "owner/koan": 1,
            "owner/backend": 2,
        }
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
  backend:
    path: /path/to/backend
    github_url: owner/backend
    max_open_prs: 5
  webapp:
    path: /path/to/webapp
""")
        _filter_exploration_projects(PROJECTS_LIST, str(koan_root))
        # Should have called batch with both repos (webapp has no URL)
        repos_arg = mock_batch.call_args[0][0]
        assert set(repos_arg) == {"owner/koan", "owner/backend"}


# === Tests: _filter_exploration_projects with branch saturation ===


class TestFilterExplorationProjectsBranchSaturation:

    def setup_method(self):
        self._batch_patcher = patch("app.github.batch_count_open_prs", return_value={})
        self._batch_patcher.start()

    def teardown_method(self):
        self._batch_patcher.stop()

    @patch("app.branch_limiter.count_pending_branches", return_value=5)
    @patch("app.github.get_gh_username", return_value="koan-bot")
    def test_under_limit_included(self, mock_user, mock_count, koan_root):
        """Project under branch limit is included."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        assert result.branch_saturated == []

    @patch("app.branch_limiter.count_pending_branches", return_value=10)
    @patch("app.github.get_gh_username", return_value="koan-bot")
    def test_at_limit_excluded(self, mock_user, mock_count, koan_root):
        """Project at branch limit is excluded."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.branch_saturated == ["koan"]

    @patch("app.branch_limiter.count_pending_branches", return_value=15)
    @patch("app.github.get_gh_username", return_value="koan-bot")
    def test_over_limit_excluded(self, mock_user, mock_count, koan_root):
        """Project over branch limit is excluded."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 10
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert result.projects == []
        assert result.branch_saturated == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    def test_zero_limit_means_unlimited(self, mock_user, koan_root):
        """max_pending_branches: 0 means unlimited — no branch count check."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 0
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        assert result.branch_saturated == []

    @patch("app.branch_limiter.count_pending_branches", side_effect=Exception("git error"))
    @patch("app.github.get_gh_username", return_value="koan-bot")
    def test_error_allows_project(self, mock_user, mock_count, koan_root):
        """Branch count error → project allowed (fail-open)."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
        )
        assert len(result.projects) == 1
        assert result.branch_saturated == []


# === Tests: _filter_exploration_projects with deep_hours PR limit relaxation ===


class TestFilterExplorationProjectsDeepHours:

    def setup_method(self):
        """Clear the PR count cache between tests."""
        from app.github import _pr_count_cache
        _pr_count_cache.clear()

    @pytest.fixture(autouse=True)
    def _mock_batch(self):
        """Disable batch GraphQL so tests exercise the sequential fallback path."""
        with patch("app.github.batch_count_open_prs", return_value={}):
            yield

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=10)
    def test_deep_hours_skips_pr_limit(self, mock_count, mock_user, koan_root):
        """During deep_hours, PR limit is relaxed — project included even at limit."""
        from app.schedule_manager import ScheduleState
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        schedule = ScheduleState(in_deep_hours=True, in_work_hours=False)
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
            schedule_state=schedule,
        )
        assert len(result.projects) == 1
        assert result.pr_limited == []
        # PR count should NOT be called — skipped entirely
        mock_count.assert_not_called()

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=10)
    def test_normal_hours_enforces_pr_limit(self, mock_count, mock_user, koan_root):
        """Outside deep_hours, PR limit is enforced normally."""
        from app.schedule_manager import ScheduleState
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        schedule = ScheduleState(in_deep_hours=False, in_work_hours=False)
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
            schedule_state=schedule,
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=10)
    def test_no_schedule_state_enforces_pr_limit(self, mock_count, mock_user, koan_root):
        """When schedule_state is None, PR limit is enforced (backward compat)."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    github_url: owner/koan
    max_open_prs: 5
""")
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
            schedule_state=None,
        )
        assert result.projects == []
        assert result.pr_limited == ["koan"]

    @patch("app.github.get_gh_username", return_value="koan-bot")
    @patch("app.github.count_open_prs", return_value=10)
    def test_deep_hours_still_respects_exploration_flag(self, mock_count, mock_user, koan_root):
        """Deep hours relaxes PR limit but NOT the exploration:false flag."""
        from app.schedule_manager import ScheduleState
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    exploration: false
    github_url: owner/koan
    max_open_prs: 5
""")
        schedule = ScheduleState(in_deep_hours=True, in_work_hours=False)
        result = _filter_exploration_projects(
            [("koan", "/path/to/koan")], str(koan_root),
            schedule_state=schedule,
        )
        assert result.projects == []


# === Tests: plan_iteration with exploration flag ===


class TestPlanIterationExploration:

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_exploration_disabled_skips_project(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """When one project is exploration-disabled, another is selected."""
        # Return only webapp (koan and backend filtered out)
        mock_filter.return_value = FilterResult(
            projects=[("webapp", "/path/to/webapp")], pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "autonomous"
        assert result["project_name"] == "webapp"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    def test_all_disabled_returns_exploration_wait(
        self, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """All projects exploration-disabled → exploration_wait action."""
        mock_filter.return_value = FilterResult(projects=[], pr_limited=[])

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "exploration_wait"
        assert "exploration disabled" in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="backend:Fix bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_mission_still_runs_on_disabled_project(
        self, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """Explicit missions execute even on exploration-disabled projects."""
        # Write config with backend exploration disabled
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
  backend:
    path: /path/to/backend
    exploration: false
  webapp:
    path: /path/to/webapp
""")
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

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
        assert result["project_name"] == "backend"
        assert result["mission_title"] == "Fix bug"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("app.iteration_manager._check_schedule", return_value=None)
    @patch("random.randint", return_value=3)  # Would trigger contemplation
    def test_contemplation_uses_filtered_project(
        self, mock_rand, mock_schedule, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """Contemplative sessions use exploration-filtered project list."""
        mock_filter.return_value = FilterResult(
            projects=[("webapp", "/path/to/webapp")], pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "contemplative"
        assert result["project_name"] == "webapp"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_mixed_projects_selects_enabled(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """With mixed enabled/disabled, only enabled projects are selected."""
        mock_filter.return_value = FilterResult(
            projects=[("koan", "/path/to/koan"), ("webapp", "/path/to/webapp")],
            pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "autonomous"
        assert result["project_name"] in ("koan", "webapp")
        assert result["project_name"] != "backend"


# === Tests: plan_iteration with PR limit ===


class TestPlanIterationPrLimit:

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_all_pr_limited_returns_pr_limit_wait(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """When all exploration-eligible projects are PR-limited, action is pr_limit_wait."""
        mock_filter.return_value = FilterResult(
            projects=[], pr_limited=["koan", "backend"],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "pr_limit_wait"
        assert "PR limit" in result["decision_reason"]
        assert "koan" in result["decision_reason"]
        assert "backend" in result["decision_reason"]

    @patch("app.pick_mission.pick_mission", return_value="koan:fix a bug")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    def test_missions_bypass_pr_limit(
        self, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """Explicit missions run even when projects are PR-limited."""
        # _filter_exploration_projects is never called for missions
        mock_filter.return_value = FilterResult(projects=[], pr_limited=["koan"])

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

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
        assert result["project_name"] == "koan"
        assert result["mission_title"] == "fix a bug"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_mixed_disabled_and_pr_limited_returns_pr_limit(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """Mix of exploration-disabled and PR-limited returns pr_limit_wait."""
        mock_filter.return_value = FilterResult(
            projects=[], pr_limited=["koan"],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "pr_limit_wait"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_some_pr_limited_still_explores_remaining(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """When only some projects are PR-limited, remaining are still explored."""
        mock_filter.return_value = FilterResult(
            projects=[("webapp", "/path/to/webapp")],
            pr_limited=["koan"],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "autonomous"
        assert result["project_name"] == "webapp"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_no_pr_limited_returns_exploration_wait(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """All disabled with no PR-limited → exploration_wait, not pr_limit_wait."""
        mock_filter.return_value = FilterResult(
            projects=[], pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "exploration_wait"


# === Tests: plan_iteration with branch saturation ===


class TestPlanIterationBranchSaturation:

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)
    def test_all_branch_saturated_returns_branch_saturated_wait(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """When all projects are branch-saturated, action is branch_saturated_wait."""
        mock_filter.return_value = FilterResult(
            projects=[], pr_limited=[], branch_saturated=["koan", "backend"],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "branch_saturated_wait"
        assert "Branch limit" in result["decision_reason"]

    @patch("app.branch_limiter.count_pending_branches", return_value=10)
    @patch("app.pick_mission.pick_mission", return_value="koan:fix a bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_mission_blocked_when_branch_saturated(
        self, mock_refresh, mock_pick, mock_count,
        instance_dir, koan_root, usage_state,
    ):
        """Mission is skipped when project is branch-saturated."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 5
""")

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=2,
            count=1,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )

        assert result["action"] == "branch_saturated_wait"
        assert result["project_name"] == "koan"

    @patch("app.branch_limiter.count_pending_branches", return_value=3)
    @patch("app.pick_mission.pick_mission", return_value="koan:fix a bug")
    @patch("app.usage_estimator.cmd_refresh")
    def test_mission_allowed_when_under_limit(
        self, mock_refresh, mock_pick, mock_count,
        instance_dir, koan_root, usage_state,
    ):
        """Mission proceeds when project is under branch limit."""
        (koan_root / "projects.yaml").write_text("""
projects:
  koan:
    path: /path/to/koan
    max_pending_branches: 10
""")

        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

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
        assert result["project_name"] == "koan"


# === Tests: CLI interface ===


class TestCLI:

    def test_cli_outputs_valid_json(self, instance_dir, koan_root, usage_state):
        """CLI produces valid JSON output (autonomous mode when no missions)."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        result = subprocess.run(
            [
                sys.executable, "-m", "app.iteration_manager",
                "plan-iteration",
                "--instance", str(instance_dir),
                "--koan-root", str(koan_root),
                "--run-num", "2",
                "--count", "1",
                "--projects", PROJECTS_STR,
                "--last-project", "koan",
                "--usage-state", str(usage_state),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "KOAN_ROOT": str(koan_root), "PYTHONPATH": str(Path(__file__).parent.parent)},
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)
        # With no missions.md, should be autonomous
        assert data["action"] in ("autonomous", "contemplative")
        assert data["autonomous_mode"] in ("wait", "review", "implement", "deep")
        assert isinstance(data["available_pct"], int)
        assert isinstance(data["display_lines"], list)
        assert data["error"] is None

    def test_cli_with_mission(self, instance_dir, koan_root, usage_state):
        """CLI picks up a mission from missions.md (fallback picker, no Claude)."""
        usage_md = instance_dir / "usage.md"
        usage_md.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n")

        # Create missions.md with a pending mission
        missions_md = instance_dir / "missions.md"
        missions_md.write_text(
            "# Missions\n\n## Pending\n\n"
            "- [project:koan] Fix the test CLI\n\n"
            "## In Progress\n\n## Done\n"
        )

        result = subprocess.run(
            [
                sys.executable, "-m", "app.iteration_manager",
                "plan-iteration",
                "--instance", str(instance_dir),
                "--koan-root", str(koan_root),
                "--run-num", "1",
                "--count", "0",
                "--projects", PROJECTS_STR,
                "--last-project", "",
                "--usage-state", str(usage_state),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "KOAN_ROOT": str(koan_root), "PYTHONPATH": str(Path(__file__).parent.parent)},
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["action"] == "mission"
        assert data["project_name"] == "koan"
        assert "Fix the test CLI" in data["mission_title"]


# === Tests: _select_random_exploration_project ===


class TestSelectRandomExplorationProject:

    def test_single_project_always_returned(self):
        """With one project, it's always selected regardless of last_project."""
        projects = [("koan", "/path/to/koan")]
        for _ in range(10):
            name, path = _select_random_exploration_project(projects, "koan")
            assert name == "koan"
            assert path == "/path/to/koan"

    def test_avoids_last_project(self):
        """With multiple projects, avoids repeating the last one."""
        projects = [("koan", "/path/to/koan"), ("backend", "/path/to/backend")]
        for _ in range(20):
            name, _ = _select_random_exploration_project(projects, "koan")
            assert name == "backend"

    def test_no_last_project_selects_any(self):
        """Without a last_project, any project can be selected."""
        projects = [("koan", "/path/koan"), ("backend", "/path/backend"), ("webapp", "/path/webapp")]
        seen = set()
        # Run enough times that random should hit all 3
        for _ in range(100):
            name, _ = _select_random_exploration_project(projects, "")
            seen.add(name)
        assert len(seen) == 3, f"Expected all 3 projects, got: {seen}"

    def test_last_project_not_in_list(self):
        """If last_project isn't in the list, any project can be selected."""
        projects = [("koan", "/path/koan"), ("backend", "/path/backend")]
        seen = set()
        for _ in range(50):
            name, _ = _select_random_exploration_project(projects, "unknown")
            seen.add(name)
        assert len(seen) == 2

    def test_multiple_projects_distributes_fairly(self):
        """With 3+ projects and a last_project, should pick from the remaining ones."""
        projects = [("a", "/a"), ("b", "/b"), ("c", "/c"), ("d", "/d")]
        seen = set()
        for _ in range(100):
            name, _ = _select_random_exploration_project(projects, "a")
            seen.add(name)
            assert name != "a"
        assert seen == {"b", "c", "d"}

    def test_returns_tuple(self):
        """Return value is a (name, path) tuple."""
        projects = [("koan", "/path/to/koan")]
        result = _select_random_exploration_project(projects, "")
        assert isinstance(result, tuple)
        assert len(result) == 2

    @patch("app.config._load_config", return_value={
        "prompt_caching": {"same_project_stickiness_percent": 100}
    })
    def test_cache_stickiness_can_keep_last_project(self, _mock_cfg):
        """When stickiness is enabled, selection may intentionally keep last project."""
        projects = [("koan", "/path/to/koan"), ("backend", "/path/to/backend")]
        for _ in range(10):
            name, _ = _select_random_exploration_project(projects, "koan")
            assert name == "koan"

    @patch("app.config._load_config", return_value={
        "prompt_caching": {"same_project_stickiness_percent": 0}
    })
    def test_cache_stickiness_zero_preserves_anti_repeat(self, _mock_cfg):
        """With stickiness=0, last_project must still be excluded when alternatives exist."""
        projects = [("koan", "/path/to/koan"), ("backend", "/path/to/backend")]
        for _ in range(50):
            name, _ = _select_random_exploration_project(projects, "koan")
            assert name != "koan"
            assert name == "backend"


# === Tests: plan_iteration random project selection ===


class TestPlanIterationRandomSelection:

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)  # no contemplation
    def test_autonomous_uses_random_selection(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """Autonomous mode should use random selection, not deterministic index."""
        mock_filter.return_value = FilterResult(
            projects=[("a", "/a"), ("b", "/b"), ("c", "/c")],
            pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n"
        )

        seen = set()
        for run_num in range(1, 30):
            result = plan_iteration(
                instance_dir=str(instance_dir),
                koan_root=str(koan_root),
                run_num=run_num,
                count=0,
                projects=PROJECTS_LIST,
                last_project="",
                usage_state_path=str(usage_state),
            )
            assert result["action"] == "autonomous"
            seen.add(result["project_name"])

        # Over 29 iterations, random selection should cover multiple projects
        assert len(seen) >= 2, f"Expected multiple projects, got only: {seen}"

    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)  # no contemplation
    def test_autonomous_avoids_last_project(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick,
        instance_dir, koan_root, usage_state,
    ):
        """Autonomous mode should avoid the last project when multiple are available."""
        mock_filter.return_value = FilterResult(
            projects=[("koan", "/koan"), ("backend", "/backend")],
            pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n"
        )

        for _ in range(10):
            result = plan_iteration(
                instance_dir=str(instance_dir),
                koan_root=str(koan_root),
                run_num=1,
                count=0,
                projects=PROJECTS_LIST,
                last_project="koan",
                usage_state_path=str(usage_state),
            )
            assert result["action"] == "autonomous"
            assert result["project_name"] == "backend"

    @patch("app.config._load_config", return_value={
        "prompt_caching": {"same_project_stickiness_percent": 100}
    })
    @patch("app.pick_mission.pick_mission", return_value="")
    @patch("app.usage_estimator.cmd_refresh")
    @patch("app.iteration_manager._filter_exploration_projects")
    @patch("app.iteration_manager._check_focus", return_value=None)
    @patch("random.randint", return_value=99)  # no contemplation
    def test_autonomous_can_keep_last_project_with_stickiness(
        self, mock_rand, mock_focus, mock_filter, mock_refresh, mock_pick, _mock_cfg,
        instance_dir, koan_root, usage_state,
    ):
        """With stickiness=100, autonomous selection should keep the previous project."""
        mock_filter.return_value = FilterResult(
            projects=[("koan", "/koan"), ("backend", "/backend")],
            pr_limited=[],
        )

        usage_md = instance_dir / "usage.md"
        usage_md.write_text(
            "Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 20% (Resets in 5d)\n"
        )

        result = plan_iteration(
            instance_dir=str(instance_dir),
            koan_root=str(koan_root),
            run_num=1,
            count=0,
            projects=PROJECTS_LIST,
            last_project="koan",
            usage_state_path=str(usage_state),
        )
        assert result["action"] == "autonomous"
        assert result["project_name"] == "koan"
