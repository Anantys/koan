"""Tests for the /stats core skill — session outcome statistics."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path, args=""):
    """Create a SkillContext for /stats."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="stats",
        args=args,
    )


def _write_outcomes(instance_dir, outcomes):
    """Write session_outcomes.json."""
    path = instance_dir / "session_outcomes.json"
    path.write_text(json.dumps(outcomes))
    return path


def _make_outcome(project="koan", mode="implement", outcome="productive",
                  duration=10, summary="branch pushed", hours_ago=0):
    """Build a session outcome entry."""
    ts = datetime.now() - timedelta(hours=hours_ago)
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "project": project,
        "mode": mode,
        "duration_minutes": duration,
        "outcome": outcome,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tests: No data
# ---------------------------------------------------------------------------

class TestNoData:
    def test_no_outcomes_file(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "No session data" in result

    def test_empty_outcomes_file(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _write_outcomes(ctx.instance_dir, [])
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "No session data" in result

    def test_corrupt_json(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        (ctx.instance_dir / "session_outcomes.json").write_text("{bad json")
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "No session data" in result


# ---------------------------------------------------------------------------
# Tests: Overview (no project filter)
# ---------------------------------------------------------------------------

class TestOverview:
    def test_single_project(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(outcome="productive", hours_ago=3),
            _make_outcome(outcome="productive", hours_ago=2),
            _make_outcome(outcome="empty", hours_ago=1),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Session Stats" in result
        assert "3 sessions" in result
        assert "66%" in result  # 2/3
        assert "koan" in result

    def test_multi_project(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="backend", outcome="empty"),
            _make_outcome(project="backend", outcome="productive"),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "4 sessions" in result
        assert "koan" in result
        assert "backend" in result
        assert "/stats <project>" in result

    def test_staleness_warning_in_overview(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(project="stale", outcome="productive", hours_ago=10),
        ] + [
            _make_outcome(project="stale", outcome="empty", hours_ago=i)
            for i in range(5)
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "!!!" in result  # 5 consecutive non-productive

    def test_staleness_mild_in_overview(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(project="mild", outcome="productive", hours_ago=10),
        ] + [
            _make_outcome(project="mild", outcome="empty", hours_ago=i)
            for i in range(3)
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        # Should have single ! but not !!!
        lines = result.split("\n")
        mild_line = [l for l in lines if "mild" in l][0]
        assert "!" in mild_line
        assert "!!!" not in mild_line

    def test_blocked_count(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(outcome="productive"),
            _make_outcome(outcome="blocked"),
            _make_outcome(outcome="empty"),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "1 blocked" in result

    def test_projects_sorted_by_count(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(project="alpha"),
            _make_outcome(project="beta"),
            _make_outcome(project="beta"),
            _make_outcome(project="beta"),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        lines = result.split("\n")
        # Project lines: "  project: N (X% productive)" — exclude headers and time lines
        project_lines = [
            l.strip() for l in lines
            if "% productive)" in l
            and not l.strip().startswith("Total")
            and not l.strip().startswith("Today")
            and not l.strip().startswith("This week")
            and not l.strip().startswith("Last week")
        ]
        # beta (3 sessions) should come before alpha (1 session)
        assert project_lines[0].startswith("beta")


# ---------------------------------------------------------------------------
# Tests: Project detail (with filter)
# ---------------------------------------------------------------------------

class TestProjectDetail:
    def test_unknown_project(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="nonexistent")
        outcomes = [_make_outcome(project="koan")]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "No data for 'nonexistent'" in result
        assert "koan" in result  # suggests known projects

    def test_detail_header(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(outcome="productive", hours_ago=2),
            _make_outcome(outcome="empty", hours_ago=1),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Stats: koan" in result
        assert "2" in result  # 2 sessions
        assert "50%" in result  # 1/2

    def test_mode_breakdown(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(mode="implement", outcome="productive"),
            _make_outcome(mode="implement", outcome="empty"),
            _make_outcome(mode="deep", outcome="productive"),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "By mode:" in result
        assert "implement: 2" in result
        assert "deep: 1" in result

    def test_duration_average(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(duration=10),
            _make_outcome(duration=20),
            _make_outcome(duration=30),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Avg duration: 20 min" in result

    def test_recent_sessions_shown(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(outcome="productive", summary="PR created", hours_ago=i)
            for i in range(7)
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Recent:" in result
        # Should show at most 5 recent
        recent_lines = [l for l in result.split("\n") if l.strip().startswith("+") or l.strip().startswith("-")]
        assert len(recent_lines) == 5

    def test_recent_icons(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(outcome="productive", summary="tests pass"),
            _make_outcome(outcome="empty", summary="no changes"),
            _make_outcome(outcome="blocked", summary="merge queue"),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "+" in result  # productive
        assert "-" in result  # empty
        assert "~" in result  # blocked

    def test_staleness_in_detail(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(outcome="productive", hours_ago=10),
        ] + [
            _make_outcome(outcome="empty", hours_ago=i)
            for i in range(5)
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Staleness: 5" in result

    def test_no_staleness_when_fresh(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [_make_outcome(outcome="productive")]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Staleness" not in result

    def test_summary_truncation(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        long_summary = "x" * 100
        outcomes = [_make_outcome(summary=long_summary)]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "..." in result

    def test_zero_duration_handled(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [_make_outcome(duration=0)]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        # Should not crash, and should not show avg duration
        assert "Stats: koan" in result


# ---------------------------------------------------------------------------
# Tests: Internal helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_consecutive_non_productive_all_productive(self):
        from skills.core.stats.handler import _consecutive_non_productive
        outcomes = [{"outcome": "productive"}, {"outcome": "productive"}]
        assert _consecutive_non_productive(outcomes) == 0

    def test_consecutive_non_productive_all_empty(self):
        from skills.core.stats.handler import _consecutive_non_productive
        outcomes = [{"outcome": "empty"}, {"outcome": "empty"}, {"outcome": "empty"}]
        assert _consecutive_non_productive(outcomes) == 3

    def test_consecutive_non_productive_mixed(self):
        from skills.core.stats.handler import _consecutive_non_productive
        outcomes = [
            {"outcome": "productive"},
            {"outcome": "empty"},
            {"outcome": "blocked"},
        ]
        # Last 2 are non-productive
        assert _consecutive_non_productive(outcomes) == 2

    def test_consecutive_non_productive_ends_productive(self):
        from skills.core.stats.handler import _consecutive_non_productive
        outcomes = [
            {"outcome": "empty"},
            {"outcome": "empty"},
            {"outcome": "productive"},
        ]
        assert _consecutive_non_productive(outcomes) == 0

    def test_consecutive_non_productive_empty_list(self):
        from skills.core.stats.handler import _consecutive_non_productive
        assert _consecutive_non_productive([]) == 0

    def test_load_outcomes_missing_file(self, tmp_path):
        from skills.core.stats.handler import _load_outcomes
        assert _load_outcomes(tmp_path / "missing.json") == []


# ---------------------------------------------------------------------------
# Tests: Productive streak
# ---------------------------------------------------------------------------

class TestProductiveStreak:
    def test_all_productive(self):
        from skills.core.stats.handler import _productive_streak
        outcomes = [{"outcome": "productive"}, {"outcome": "productive"}]
        assert _productive_streak(outcomes) == 2

    def test_broken_by_empty(self):
        from skills.core.stats.handler import _productive_streak
        outcomes = [
            {"outcome": "productive"},
            {"outcome": "empty"},
            {"outcome": "productive"},
        ]
        assert _productive_streak(outcomes) == 1

    def test_no_productive(self):
        from skills.core.stats.handler import _productive_streak
        outcomes = [{"outcome": "empty"}, {"outcome": "blocked"}]
        assert _productive_streak(outcomes) == 0

    def test_empty_list(self):
        from skills.core.stats.handler import _productive_streak
        assert _productive_streak([]) == 0

    def test_long_streak(self):
        from skills.core.stats.handler import _productive_streak
        outcomes = [{"outcome": "productive"} for _ in range(10)]
        assert _productive_streak(outcomes) == 10

    def test_streak_shown_in_overview(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [
            _make_outcome(outcome="productive", hours_ago=i)
            for i in range(3)
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Streak: 3 productive in a row" in result

    def test_streak_hidden_when_one(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [_make_outcome(outcome="productive")]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Streak" not in result

    def test_streak_hidden_when_zero(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        outcomes = [_make_outcome(outcome="empty")]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Streak" not in result

    def test_streak_in_project_detail(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        outcomes = [
            _make_outcome(outcome="productive", hours_ago=i)
            for i in range(4)
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Streak: 4 productive in a row" in result


# ---------------------------------------------------------------------------
# Tests: Time-based filtering
# ---------------------------------------------------------------------------

class TestTimeFilter:
    def test_filter_today(self):
        from skills.core.stats.handler import _filter_by_period
        now = datetime(2026, 2, 23, 15, 0, 0)
        outcomes = [
            {"timestamp": "2026-02-23T10:00:00"},  # today
            {"timestamp": "2026-02-23T14:00:00"},  # today
            {"timestamp": "2026-02-22T23:00:00"},  # yesterday
        ]
        result = _filter_by_period(outcomes, "today", now)
        assert len(result) == 2

    def test_filter_today_empty(self):
        from skills.core.stats.handler import _filter_by_period
        now = datetime(2026, 2, 23, 15, 0, 0)
        outcomes = [
            {"timestamp": "2026-02-22T23:00:00"},  # yesterday
        ]
        result = _filter_by_period(outcomes, "today", now)
        assert len(result) == 0

    def test_filter_week(self):
        from skills.core.stats.handler import _filter_by_period
        # 2026-02-23 is a Monday
        now = datetime(2026, 2, 25, 15, 0, 0)  # Wednesday
        outcomes = [
            {"timestamp": "2026-02-23T10:00:00"},  # Monday (this week)
            {"timestamp": "2026-02-24T10:00:00"},  # Tuesday (this week)
            {"timestamp": "2026-02-22T10:00:00"},  # Sunday (last week)
        ]
        result = _filter_by_period(outcomes, "week", now)
        assert len(result) == 2

    def test_filter_last_week(self):
        from skills.core.stats.handler import _filter_by_period
        # 2026-02-23 is a Monday
        now = datetime(2026, 2, 25, 15, 0, 0)  # Wednesday
        # Last week = Mon Feb 16 to Sun Feb 22
        outcomes = [
            {"timestamp": "2026-02-23T10:00:00"},  # Monday (this week)
            {"timestamp": "2026-02-18T10:00:00"},  # last Wednesday (last week)
            {"timestamp": "2026-02-15T10:00:00"},  # Sunday (2 weeks ago)
        ]
        result = _filter_by_period(outcomes, "last_week", now)
        assert len(result) == 1

    def test_filter_invalid_timestamps_skipped(self):
        from skills.core.stats.handler import _filter_by_period
        now = datetime(2026, 2, 23, 15, 0, 0)
        outcomes = [
            {"timestamp": "2026-02-23T10:00:00"},
            {"timestamp": "bad-timestamp"},
            {},
        ]
        result = _filter_by_period(outcomes, "today", now)
        assert len(result) == 1

    def test_filter_unknown_period(self):
        from skills.core.stats.handler import _filter_by_period
        outcomes = [{"timestamp": "2026-02-23T10:00:00"}]
        result = _filter_by_period(outcomes, "unknown", datetime(2026, 2, 23))
        assert len(result) == 1  # returns all


# ---------------------------------------------------------------------------
# Tests: Period line formatting
# ---------------------------------------------------------------------------

class TestPeriodLine:
    def test_format_period_line(self):
        from skills.core.stats.handler import _format_period_line
        outcomes = [
            {"outcome": "productive"},
            {"outcome": "productive"},
            {"outcome": "empty"},
        ]
        result = _format_period_line(outcomes, "Today")
        assert "Today: 3 sessions (66% productive)" in result

    def test_empty_period(self):
        from skills.core.stats.handler import _format_period_line
        result = _format_period_line([], "Today")
        assert result == ""

    def test_all_productive(self):
        from skills.core.stats.handler import _format_period_line
        outcomes = [{"outcome": "productive"}, {"outcome": "productive"}]
        result = _format_period_line(outcomes, "This week")
        assert "100% productive" in result


# ---------------------------------------------------------------------------
# Tests: Time breakdowns in overview and detail
# ---------------------------------------------------------------------------

class TestTimeBreakdowns:
    def test_overview_shows_today(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # Pin time to Wednesday mid-day to avoid day/week boundary flakiness
        fixed_now = datetime(2026, 3, 4, 12, 0, 0)  # Wednesday
        outcomes = [
            {"timestamp": fixed_now.isoformat(timespec="seconds"),
             "project": "koan", "mode": "implement", "duration_minutes": 10,
             "outcome": "productive", "summary": "branch pushed"},
            {"timestamp": fixed_now.isoformat(timespec="seconds"),
             "project": "koan", "mode": "implement", "duration_minutes": 10,
             "outcome": "empty", "summary": "nothing"},
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        with patch("skills.core.stats.handler.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            result = handle(ctx)
        assert "Today:" in result

    def test_overview_shows_this_week(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # Pin time to Wednesday mid-day to avoid week boundary flakiness
        fixed_now = datetime(2026, 3, 4, 12, 0, 0)  # Wednesday
        outcomes = [
            {"timestamp": fixed_now.isoformat(timespec="seconds"),
             "project": "koan", "mode": "implement", "duration_minutes": 10,
             "outcome": "productive", "summary": "branch pushed"},
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        with patch("skills.core.stats.handler.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            result = handle(ctx)
        assert "This week:" in result

    def test_detail_shows_today(self, tmp_path):
        ctx = _make_ctx(tmp_path, args="koan")
        # Pin time to Wednesday mid-day to avoid day/week boundary flakiness
        fixed_now = datetime(2026, 3, 4, 12, 0, 0)  # Wednesday
        outcomes = [
            {"timestamp": fixed_now.isoformat(timespec="seconds"),
             "project": "koan", "mode": "implement", "duration_minutes": 10,
             "outcome": "productive", "summary": "branch pushed"},
            {"timestamp": fixed_now.isoformat(timespec="seconds"),
             "project": "koan", "mode": "implement", "duration_minutes": 10,
             "outcome": "productive", "summary": "branch pushed"},
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        with patch("skills.core.stats.handler.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            result = handle(ctx)
        assert "Today:" in result

    def test_no_time_lines_for_old_data(self, tmp_path):
        """Old outcomes (>2 weeks ago) shouldn't show Today/This week lines."""
        ctx = _make_ctx(tmp_path)
        # 500 hours ago = ~20 days
        outcomes = [
            _make_outcome(outcome="productive", hours_ago=500),
        ]
        _write_outcomes(ctx.instance_dir, outcomes)
        from skills.core.stats.handler import handle
        result = handle(ctx)
        assert "Today:" not in result
        # "This week:" should also be absent for data from 20 days ago
        assert "This week:" not in result
