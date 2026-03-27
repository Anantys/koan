"""Tests for mission_metrics.py — statistical mission metrics."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.mission_metrics import (
    compute_project_metrics,
    compute_global_metrics,
    get_project_success_rates,
    format_metrics_summary,
    _compute_trend,
    _filter_by_window,
)


@pytest.fixture
def metrics_env(tmp_path):
    """Create a minimal environment for metrics testing."""
    instance = tmp_path / "instance"
    instance.mkdir()
    return str(instance)


def _write_outcomes(instance_dir, outcomes):
    """Write test outcomes to session_outcomes.json."""
    Path(instance_dir, "session_outcomes.json").write_text(
        json.dumps(outcomes, indent=2)
    )


def _make_outcome(project="koan", outcome="productive", mission_type="mission",
                  has_pr=False, has_branch=False, duration=10, days_ago=0):
    """Build a test outcome entry."""
    ts = datetime.now() - timedelta(days=days_ago)
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "project": project,
        "mode": "implement",
        "duration_minutes": duration,
        "outcome": outcome,
        "summary": f"{outcome} session",
        "mission_type": mission_type,
        "has_pr": has_pr,
        "has_branch": has_branch,
    }


# --- compute_project_metrics ---

class TestComputeProjectMetrics:

    def test_no_data(self, metrics_env):
        result = compute_project_metrics(metrics_env, "koan")
        assert result["total_sessions"] == 0
        assert result["success_rate"] == 0.0

    def test_all_productive(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(outcome="productive") for _ in range(5)
        ])
        result = compute_project_metrics(metrics_env, "koan")
        assert result["total_sessions"] == 5
        assert result["success_rate"] == 1.0

    def test_mixed_outcomes(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(outcome="productive"),
            _make_outcome(outcome="productive"),
            _make_outcome(outcome="empty"),
            _make_outcome(outcome="blocked"),
        ])
        result = compute_project_metrics(metrics_env, "koan")
        assert result["total_sessions"] == 4
        assert result["productive"] == 2
        assert result["empty"] == 1
        assert result["blocked"] == 1
        assert result["success_rate"] == 0.5

    def test_pr_and_branch_rates(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(has_pr=True, has_branch=True),
            _make_outcome(has_pr=True, has_branch=True),
            _make_outcome(has_pr=False, has_branch=True),
            _make_outcome(has_pr=False, has_branch=False),
        ])
        result = compute_project_metrics(metrics_env, "koan")
        assert result["pr_rate"] == 0.5
        assert result["branch_rate"] == 0.75

    def test_filters_by_project(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="other", outcome="empty"),
            _make_outcome(project="koan", outcome="empty"),
        ])
        result = compute_project_metrics(metrics_env, "koan")
        assert result["total_sessions"] == 2

    def test_by_mission_type(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(mission_type="skill", outcome="productive"),
            _make_outcome(mission_type="skill", outcome="productive"),
            _make_outcome(mission_type="mission", outcome="empty"),
            _make_outcome(mission_type="autonomous", outcome="productive"),
        ])
        result = compute_project_metrics(metrics_env, "koan")
        assert result["by_mission_type"]["skill"]["total"] == 2
        assert result["by_mission_type"]["skill"]["success_rate"] == 1.0
        assert result["by_mission_type"]["mission"]["success_rate"] == 0.0

    def test_avg_duration(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(duration=10),
            _make_outcome(duration=20),
            _make_outcome(duration=30),
        ])
        result = compute_project_metrics(metrics_env, "koan")
        assert result["avg_duration_minutes"] == 20.0

    def test_time_window_filter(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(days_ago=5),   # within 7 days
            _make_outcome(days_ago=10),  # outside 7 days
        ])
        result = compute_project_metrics(metrics_env, "koan", days=7)
        assert result["total_sessions"] == 1

    def test_all_time(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(days_ago=5),
            _make_outcome(days_ago=100),
        ])
        result = compute_project_metrics(metrics_env, "koan", days=0)
        assert result["total_sessions"] == 2


# --- compute_global_metrics ---

class TestComputeGlobalMetrics:

    def test_no_data(self, metrics_env):
        result = compute_global_metrics(metrics_env)
        assert result["total_sessions"] == 0
        assert result["trend"] == "stable"

    def test_cross_project(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="other", outcome="empty"),
        ])
        result = compute_global_metrics(metrics_env)
        assert result["total_sessions"] == 2
        assert result["success_rate"] == 0.5
        assert "koan" in result["by_project"]
        assert "other" in result["by_project"]

    def test_by_project_breakdown(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="empty"),
        ])
        result = compute_global_metrics(metrics_env)
        koan = result["by_project"]["koan"]
        assert koan["total"] == 3
        assert koan["productive"] == 2
        assert abs(koan["success_rate"] - 2/3) < 0.01


# --- get_project_success_rates ---

class TestGetProjectSuccessRates:

    def test_no_data_returns_neutral(self, metrics_env):
        rates = get_project_success_rates(metrics_env, ["koan", "other"])
        assert rates["koan"] == 0.5
        assert rates["other"] == 0.5

    def test_insufficient_data_returns_neutral(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="productive"),
        ])
        rates = get_project_success_rates(metrics_env, ["koan"])
        assert rates["koan"] == 0.5  # Only 2 sessions, need >= 3

    def test_sufficient_data(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="empty"),
            _make_outcome(project="koan", outcome="empty"),
        ])
        rates = get_project_success_rates(metrics_env, ["koan"])
        assert rates["koan"] == 0.5

    def test_multiple_projects(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="other", outcome="empty"),
            _make_outcome(project="other", outcome="empty"),
            _make_outcome(project="other", outcome="empty"),
        ])
        rates = get_project_success_rates(metrics_env, ["koan", "other"])
        assert rates["koan"] == 1.0
        assert rates["other"] == 0.0


# --- _compute_trend ---

class TestComputeTrend:

    def test_too_few_sessions(self):
        outcomes = [{"outcome": "productive"}] * 3
        assert _compute_trend(outcomes) == "stable"

    def test_improving(self):
        outcomes = (
            [{"outcome": "empty"}] * 5
            + [{"outcome": "productive"}] * 5
        )
        assert _compute_trend(outcomes) == "improving"

    def test_declining(self):
        outcomes = (
            [{"outcome": "productive"}] * 5
            + [{"outcome": "empty"}] * 5
        )
        assert _compute_trend(outcomes) == "declining"

    def test_stable(self):
        # Even mix in both halves → stable
        half = [{"outcome": "productive"}, {"outcome": "empty"}] * 2
        outcomes = half + half  # 8 entries, same ratio in both halves
        assert _compute_trend(outcomes) == "stable"


# --- format_metrics_summary ---

class TestFormatMetricsSummary:

    def test_no_data(self, metrics_env):
        result = format_metrics_summary(metrics_env)
        assert "No session data" in result

    def test_with_data(self, metrics_env):
        _write_outcomes(metrics_env, [
            _make_outcome(project="koan", outcome="productive", has_pr=True, has_branch=True),
            _make_outcome(project="koan", outcome="productive", has_pr=True, has_branch=True),
            _make_outcome(project="koan", outcome="empty"),
        ])
        result = format_metrics_summary(metrics_env)
        assert "Mission Metrics" in result
        assert "Total sessions: 3" in result
        assert "koan" in result

    def test_includes_trend(self, metrics_env):
        # Create enough data for trend detection
        _write_outcomes(metrics_env, [
            _make_outcome(outcome="empty") for _ in range(4)
        ] + [
            _make_outcome(outcome="productive") for _ in range(4)
        ])
        result = format_metrics_summary(metrics_env)
        assert "Trend:" in result


# --- _filter_by_window ---

class TestFilterByWindow:

    def test_zero_days_returns_all(self):
        outcomes = [{"timestamp": "2020-01-01T00:00:00"}]
        assert _filter_by_window(outcomes, 0) == outcomes

    def test_filters_old_entries(self):
        old = {"timestamp": (datetime.now() - timedelta(days=60)).isoformat()}
        new = {"timestamp": datetime.now().isoformat()}
        result = _filter_by_window([old, new], 30)
        assert len(result) == 1

    def test_includes_unparseable_timestamps(self):
        entries = [{"timestamp": "not-a-date"}, {"timestamp": "also-bad"}]
        result = _filter_by_window(entries, 30)
        assert len(result) == 2  # Included by benefit of the doubt
