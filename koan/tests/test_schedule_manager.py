"""Tests for app.schedule_manager — time-window scheduling."""

import os
from datetime import datetime
from unittest.mock import patch

import pytest

os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")

from app.schedule_manager import (
    ScheduleState,
    TimeRange,
    adjust_contemplative_chance,
    cap_mode_for_schedule,
    check_schedule,
    get_current_schedule,
    get_schedule_config,
    parse_time_ranges,
    is_scheduled_active,
    should_relax_pr_limit,
    should_suppress_exploration,
)


# === Tests: TimeRange ===


class TestTimeRange:
    """Tests for TimeRange.contains()."""

    def test_simple_range(self):
        r = TimeRange(start=8, end=20)
        assert r.contains(8)
        assert r.contains(12)
        assert r.contains(19)
        assert not r.contains(20)
        assert not r.contains(7)
        assert not r.contains(0)

    def test_midnight_wrap(self):
        r = TimeRange(start=22, end=6)
        assert r.contains(22)
        assert r.contains(23)
        assert r.contains(0)
        assert r.contains(3)
        assert r.contains(5)
        assert not r.contains(6)
        assert not r.contains(12)
        assert not r.contains(21)

    def test_full_night(self):
        r = TimeRange(start=0, end=6)
        assert r.contains(0)
        assert r.contains(5)
        assert not r.contains(6)
        assert not r.contains(23)

    def test_single_hour(self):
        r = TimeRange(start=12, end=13)
        assert r.contains(12)
        assert not r.contains(11)
        assert not r.contains(13)

    def test_near_midnight(self):
        r = TimeRange(start=23, end=1)
        assert r.contains(23)
        assert r.contains(0)
        assert not r.contains(1)
        assert not r.contains(22)


# === Tests: parse_time_ranges ===


class TestParseTimeRanges:
    """Tests for parse_time_ranges()."""

    def test_empty_string(self):
        assert parse_time_ranges("") == []

    def test_simple_range(self):
        ranges = parse_time_ranges("0-6")
        assert len(ranges) == 1
        assert ranges[0].start == 0
        assert ranges[0].end == 6

    def test_multiple_ranges(self):
        ranges = parse_time_ranges("0-6,22-24")
        assert len(ranges) == 2
        assert ranges[0].start == 0
        assert ranges[0].end == 6
        assert ranges[1].start == 22
        assert ranges[1].end == 24

    def test_whitespace_handling(self):
        ranges = parse_time_ranges(" 0 - 6 , 22 - 24 ")
        assert len(ranges) == 2
        assert ranges[0].start == 0
        assert ranges[0].end == 6

    def test_wrap_around(self):
        ranges = parse_time_ranges("22-6")
        assert len(ranges) == 1
        assert ranges[0].start == 22
        assert ranges[0].end == 6

    def test_invalid_no_dash(self):
        with pytest.raises(ValueError, match="expected format"):
            parse_time_ranges("abc")

    def test_invalid_non_integer(self):
        with pytest.raises(ValueError, match="must be integers"):
            parse_time_ranges("a-b")

    def test_invalid_start_too_high(self):
        with pytest.raises(ValueError, match="must be 0-23"):
            parse_time_ranges("25-6")

    def test_invalid_end_too_high(self):
        with pytest.raises(ValueError, match="must be 0-24"):
            parse_time_ranges("0-25")

    def test_invalid_negative_start(self):
        with pytest.raises(ValueError):
            parse_time_ranges("-1-6")

    def test_invalid_equal_start_end(self):
        with pytest.raises(ValueError, match="cannot be equal"):
            parse_time_ranges("6-6")

    def test_end_24_is_valid(self):
        ranges = parse_time_ranges("22-24")
        assert len(ranges) == 1
        assert ranges[0].end == 24


# === Tests: check_schedule ===


class TestCheckSchedule:
    """Tests for check_schedule()."""

    def test_no_config(self):
        state = check_schedule(now=datetime(2026, 2, 7, 14, 0))
        assert state.mode == "normal"
        assert not state.in_deep_hours
        assert not state.in_work_hours

    def test_deep_hours_active(self):
        state = check_schedule(
            deep_hours_spec="0-6",
            now=datetime(2026, 2, 7, 3, 0),
        )
        assert state.mode == "deep"
        assert state.in_deep_hours
        assert not state.in_work_hours

    def test_deep_hours_inactive(self):
        state = check_schedule(
            deep_hours_spec="0-6",
            now=datetime(2026, 2, 7, 14, 0),
        )
        assert state.mode == "normal"
        assert not state.in_deep_hours

    def test_work_hours_active(self):
        state = check_schedule(
            work_hours_spec="8-20",
            now=datetime(2026, 2, 7, 14, 0),
        )
        assert state.mode == "work"
        assert not state.in_deep_hours
        assert state.in_work_hours

    def test_work_hours_inactive(self):
        state = check_schedule(
            work_hours_spec="8-20",
            now=datetime(2026, 2, 7, 3, 0),
        )
        assert state.mode == "normal"
        assert not state.in_work_hours

    def test_both_configured_deep_active(self):
        state = check_schedule(
            deep_hours_spec="0-6",
            work_hours_spec="8-20",
            now=datetime(2026, 2, 7, 3, 0),
        )
        assert state.mode == "deep"

    def test_both_configured_work_active(self):
        state = check_schedule(
            deep_hours_spec="0-6",
            work_hours_spec="8-20",
            now=datetime(2026, 2, 7, 14, 0),
        )
        assert state.mode == "work"

    def test_both_configured_gap_hours(self):
        state = check_schedule(
            deep_hours_spec="0-6",
            work_hours_spec="8-20",
            now=datetime(2026, 2, 7, 7, 0),
        )
        assert state.mode == "normal"

    def test_deep_priority_on_overlap(self):
        """When both deep and work overlap, deep takes priority."""
        state = check_schedule(
            deep_hours_spec="0-24",
            work_hours_spec="0-24",
            now=datetime(2026, 2, 7, 12, 0),
        )
        assert state.mode == "deep"

    def test_wrap_around_deep(self):
        state = check_schedule(
            deep_hours_spec="22-6",
            now=datetime(2026, 2, 7, 23, 0),
        )
        assert state.mode == "deep"

    def test_invalid_spec_ignored(self):
        state = check_schedule(
            deep_hours_spec="invalid",
            now=datetime(2026, 2, 7, 3, 0),
        )
        assert state.mode == "normal"

    def test_multiple_deep_ranges(self):
        state = check_schedule(
            deep_hours_spec="0-6,22-24",
            now=datetime(2026, 2, 7, 23, 0),
        )
        assert state.mode == "deep"


# === Tests: adjust_contemplative_chance ===


class TestAdjustContemplativeChance:
    """Tests for adjust_contemplative_chance()."""

    def test_normal_mode_unchanged(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert adjust_contemplative_chance(10, state) == 10

    def test_deep_mode_triples(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert adjust_contemplative_chance(10, state) == 30

    def test_deep_mode_caps_at_50(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert adjust_contemplative_chance(20, state) == 50

    def test_work_mode_zeroes(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=True)
        assert adjust_contemplative_chance(10, state) == 0

    def test_zero_base_stays_zero(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert adjust_contemplative_chance(0, state) == 0


# === Tests: should_relax_pr_limit ===


class TestShouldRelaxPrLimit:
    """Tests for should_relax_pr_limit()."""

    def test_deep_hours_relaxes(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert should_relax_pr_limit(state) is True

    def test_work_hours_does_not_relax(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=True)
        assert should_relax_pr_limit(state) is False

    def test_normal_does_not_relax(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert should_relax_pr_limit(state) is False


# === Tests: should_suppress_exploration ===


class TestShouldSuppressExploration:
    """Tests for should_suppress_exploration()."""

    def test_work_hours_suppresses(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=True)
        assert should_suppress_exploration(state) is True

    def test_deep_hours_does_not_suppress(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert should_suppress_exploration(state) is False

    def test_normal_does_not_suppress(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert should_suppress_exploration(state) is False


# === Tests: cap_mode_for_schedule ===


class TestCapModeForSchedule:
    """Tests for cap_mode_for_schedule()."""

    def test_deep_capped_outside_deep_hours(self):
        """Budget 'deep' is capped to 'implement' when outside configured deep_hours."""
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert cap_mode_for_schedule("deep", state, deep_hours_configured=True) == "implement"

    def test_deep_allowed_during_deep_hours(self):
        """Budget 'deep' stays 'deep' when inside deep_hours."""
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert cap_mode_for_schedule("deep", state, deep_hours_configured=True) == "deep"

    def test_deep_allowed_when_no_deep_hours_configured(self):
        """Budget 'deep' stays 'deep' when deep_hours is not configured at all."""
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert cap_mode_for_schedule("deep", state, deep_hours_configured=False) == "deep"

    def test_implement_not_capped(self):
        """Non-deep modes pass through unchanged."""
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert cap_mode_for_schedule("implement", state, deep_hours_configured=True) == "implement"

    def test_review_not_capped(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert cap_mode_for_schedule("review", state, deep_hours_configured=True) == "review"

    def test_wait_not_capped(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert cap_mode_for_schedule("wait", state, deep_hours_configured=True) == "wait"

    def test_deep_during_work_hours_is_capped(self):
        """Even if work_hours are active, the cap logic applies to deep mode."""
        state = ScheduleState(in_deep_hours=False, in_work_hours=True)
        assert cap_mode_for_schedule("deep", state, deep_hours_configured=True) == "implement"

    def test_implement_during_deep_hours_unchanged(self):
        """Implement mode is not promoted to deep during deep_hours (cap is one-way)."""
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert cap_mode_for_schedule("implement", state, deep_hours_configured=True) == "implement"


# === Tests: get_schedule_config ===


class TestGetScheduleConfig:
    """Tests for get_schedule_config()."""

    def test_reads_from_config(self):
        config = {
            "schedule": {
                "deep_hours": "0-6",
                "work_hours": "8-20",
            }
        }
        with patch("app.utils.load_config", return_value=config):
            deep, work = get_schedule_config()
            assert deep == "0-6"
            assert work == "8-20"

    def test_missing_schedule_section(self):
        with patch("app.utils.load_config", return_value={}):
            deep, work = get_schedule_config()
            assert deep == ""
            assert work == ""

    def test_partial_config(self):
        config = {"schedule": {"deep_hours": "0-6"}}
        with patch("app.utils.load_config", return_value=config):
            deep, work = get_schedule_config()
            assert deep == "0-6"
            assert work == ""

    def test_non_dict_schedule(self):
        config = {"schedule": "invalid"}
        with patch("app.utils.load_config", return_value=config):
            deep, work = get_schedule_config()
            assert deep == ""
            assert work == ""

    def test_import_error_returns_empty(self):
        with patch(
            "app.utils.load_config",
            side_effect=ImportError("no module"),
        ):
            deep, work = get_schedule_config()
            assert deep == ""
            assert work == ""


# === Tests: get_current_schedule ===


class TestGetCurrentSchedule:
    """Tests for get_current_schedule()."""

    def test_uses_config_and_time(self):
        config = {"schedule": {"deep_hours": "0-6", "work_hours": "8-20"}}
        with patch("app.utils.load_config", return_value=config):
            with patch("app.schedule_manager.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 2, 7, 3, 0)
                state = get_current_schedule()
                assert state.mode == "deep"


# === Tests: ScheduleState ===


class TestScheduleState:
    """Tests for ScheduleState dataclass."""

    def test_mode_deep(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert state.mode == "deep"

    def test_mode_work(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=True)
        assert state.mode == "work"

    def test_mode_normal(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert state.mode == "normal"


# === Tests: is_scheduled_active ===


class TestIsScheduledActive:
    """Tests for is_scheduled_active()."""

    def test_active_in_deep_hours(self):
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        assert is_scheduled_active(state) is True

    def test_active_in_work_hours(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=True)
        assert is_scheduled_active(state) is True

    def test_inactive_in_normal_hours(self):
        state = ScheduleState(in_deep_hours=False, in_work_hours=False)
        assert is_scheduled_active(state) is False

    def test_auto_fetches_schedule(self):
        """When no schedule is passed, it fetches the current one."""
        state = ScheduleState(in_deep_hours=True, in_work_hours=False)
        with patch("app.schedule_manager.get_current_schedule", return_value=state):
            assert is_scheduled_active() is True
