"""Tests for usage analytics: daily_series, /api/usage extensions, /api/metrics."""

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from jinja2 import FileSystemLoader

from app.cost_tracker import daily_series
from app.mission_metrics import compute_global_metrics, compute_project_trend
from app import dashboard

REAL_TEMPLATES = Path(__file__).parent.parent / "templates"


# --- Fixtures ---

@pytest.fixture
def instance_dir(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "usage").mkdir()
    (inst / "memory" / "global").mkdir(parents=True)
    (inst / "memory" / "projects" / "koan").mkdir(parents=True)
    (inst / "journal" / "2026-03-14").mkdir(parents=True)
    (inst / "soul.md").write_text("You are Kōan.")
    (inst / "memory" / "summary.md").write_text("")
    (inst / "missions.md").write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
    (inst / "journal" / "2026-03-14" / "koan.md").write_text("")
    return inst


@pytest.fixture
def app_client(instance_dir, tmp_path):
    tpl_dest = tmp_path / "koan" / "templates"
    shutil.copytree(REAL_TEMPLATES, tpl_dest)
    with patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
         patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"), \
         patch.object(dashboard, "OUTBOX_FILE", instance_dir / "outbox.md"), \
         patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
         patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
         patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"), \
         patch.object(dashboard, "PENDING_FILE", instance_dir / "journal" / "pending.md"), \
         patch.object(dashboard, "KOAN_ROOT", tmp_path):
        dashboard.app.config["TESTING"] = True
        dashboard.app.jinja_loader = FileSystemLoader(str(tpl_dest))
        with dashboard.app.test_client() as client:
            yield client


def _write_jsonl(usage_dir, d, entries):
    lines = [json.dumps(e, separators=(",", ":")) for e in entries]
    (usage_dir / f"{d.isoformat()}.jsonl").write_text("\n".join(lines) + "\n")


def _write_outcomes(instance_dir, outcomes):
    Path(instance_dir / "session_outcomes.json").write_text(json.dumps(outcomes))


def _make_outcome(project="koan", outcome="productive", days_ago=0):
    ts = datetime.now() - timedelta(days=days_ago)
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "project": project,
        "mode": "implement",
        "duration_minutes": 10,
        "outcome": outcome,
        "mission_type": "mission",
        "has_pr": False,
        "has_branch": False,
    }


# --- daily_series tests ---

class TestDailySeries:
    def test_shape_and_keys(self, instance_dir):
        start = date(2026, 3, 10)
        end = date(2026, 3, 12)
        result = daily_series(instance_dir, start, end)
        assert len(result) == 3
        required = {"date", "total_input", "total_output", "count", "cost"}
        for day in result:
            assert required.issubset(day.keys())

    def test_aggregates_per_day(self, instance_dir):
        usage_dir = instance_dir / "usage"
        d = date(2026, 3, 10)
        _write_jsonl(usage_dir, d, [
            {"input_tokens": 100, "output_tokens": 50, "project": "koan", "model": "sonnet"},
            {"input_tokens": 200, "output_tokens": 100, "project": "koan", "model": "sonnet"},
        ])
        result = daily_series(instance_dir, d, d)
        assert len(result) == 1
        assert result[0]["total_input"] == 300
        assert result[0]["total_output"] == 150
        assert result[0]["count"] == 2

    def test_empty_days_included(self, instance_dir):
        start = date(2026, 3, 10)
        end = date(2026, 3, 12)
        result = daily_series(instance_dir, start, end)
        assert len(result) == 3
        for day in result:
            assert day["total_input"] == 0
            assert day["count"] == 0

    def test_project_filter(self, instance_dir):
        usage_dir = instance_dir / "usage"
        d = date(2026, 3, 10)
        _write_jsonl(usage_dir, d, [
            {"input_tokens": 100, "output_tokens": 50, "project": "koan", "model": "sonnet"},
            {"input_tokens": 200, "output_tokens": 100, "project": "other", "model": "sonnet"},
        ])
        result = daily_series(instance_dir, d, d, project="koan")
        assert result[0]["total_input"] == 100
        assert result[0]["count"] == 1

    def test_cost_without_pricing(self, instance_dir):
        usage_dir = instance_dir / "usage"
        d = date(2026, 3, 10)
        _write_jsonl(usage_dir, d, [
            {"input_tokens": 100, "output_tokens": 50, "project": "koan", "model": "sonnet"},
        ])
        with patch("app.cost_tracker.get_pricing_config", return_value=None):
            result = daily_series(instance_dir, d, d)
        assert result[0]["cost"] is None

    def test_cost_with_pricing(self, instance_dir):
        usage_dir = instance_dir / "usage"
        d = date(2026, 3, 10)
        _write_jsonl(usage_dir, d, [
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "project": "koan", "model": "claude-sonnet-4"},
        ])
        pricing = {"sonnet": {"input": 3.0, "output": 15.0}}
        with patch("app.cost_tracker.get_pricing_config", return_value=pricing):
            result = daily_series(instance_dir, d, d)
        assert result[0]["cost"] == pytest.approx(18.0)


# --- compute_global_metrics extensions ---

class TestGlobalMetricsExtended:
    def test_includes_productive_empty_blocked(self, instance_dir):
        _write_outcomes(instance_dir, [
            _make_outcome(outcome="productive"),
            _make_outcome(outcome="productive"),
            _make_outcome(outcome="empty"),
            _make_outcome(outcome="blocked"),
        ])
        result = compute_global_metrics(str(instance_dir))
        assert result["productive"] == 2
        assert result["empty"] == 1
        assert result["blocked"] == 1

    def test_empty_data_has_no_counts(self, instance_dir):
        result = compute_global_metrics(str(instance_dir))
        assert result["total_sessions"] == 0


# --- compute_project_trend ---

class TestComputeProjectTrend:
    def test_stable_with_few_sessions(self, instance_dir):
        _write_outcomes(instance_dir, [
            _make_outcome(project="koan") for _ in range(3)
        ])
        assert compute_project_trend(str(instance_dir), "koan") == "stable"

    def test_improving(self, instance_dir):
        outcomes = (
            [_make_outcome(project="koan", outcome="empty") for _ in range(5)]
            + [_make_outcome(project="koan", outcome="productive") for _ in range(5)]
        )
        _write_outcomes(instance_dir, outcomes)
        assert compute_project_trend(str(instance_dir), "koan") == "improving"

    def test_declining(self, instance_dir):
        outcomes = (
            [_make_outcome(project="koan", outcome="productive") for _ in range(5)]
            + [_make_outcome(project="koan", outcome="empty") for _ in range(5)]
        )
        _write_outcomes(instance_dir, outcomes)
        assert compute_project_trend(str(instance_dir), "koan") == "declining"

    def test_filters_by_project(self, instance_dir):
        outcomes = (
            [_make_outcome(project="koan", outcome="productive") for _ in range(5)]
            + [_make_outcome(project="other", outcome="empty") for _ in range(5)]
        )
        _write_outcomes(instance_dir, outcomes)
        assert compute_project_trend(str(instance_dir), "koan") == "stable"


# --- /api/usage extensions ---

class TestApiUsageExtended:
    def test_daily_array_present(self, app_client, instance_dir):
        resp = app_client.get("/api/usage?days=3")
        data = resp.get_json()
        assert "daily" in data
        assert isinstance(data["daily"], list)
        assert len(data["daily"]) == 3

    def test_estimated_cost_null_without_pricing(self, app_client, instance_dir):
        with patch("app.cost_tracker.get_pricing_config", return_value=None):
            resp = app_client.get("/api/usage?days=1")
        data = resp.get_json()
        assert data["estimated_cost"] is None

    def test_estimated_cost_with_pricing(self, app_client, instance_dir):
        usage_dir = instance_dir / "usage"
        today = date.today()
        _write_jsonl(usage_dir, today, [
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
             "project": "koan", "model": "claude-sonnet-4"},
        ])
        pricing = {"sonnet": {"input": 3.0, "output": 15.0}}
        with patch("app.cost_tracker.get_pricing_config", return_value=pricing):
            resp = app_client.get("/api/usage?days=1")
        data = resp.get_json()
        assert data["estimated_cost"] == pytest.approx(18.0)
        assert data["has_pricing"] is True


# --- /api/metrics ---

class TestApiMetrics:
    def test_global_metrics_structure(self, app_client, instance_dir):
        _write_outcomes(instance_dir, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="koan", outcome="empty"),
        ])
        resp = app_client.get("/api/metrics?days=30")
        data = resp.get_json()
        assert data["total_sessions"] == 2
        assert "productive" in data
        assert "empty" in data
        assert "blocked" in data
        assert "by_project" in data

    def test_per_project_trend_included(self, app_client, instance_dir):
        outcomes = [_make_outcome(project="koan") for _ in range(3)]
        _write_outcomes(instance_dir, outcomes)
        resp = app_client.get("/api/metrics?days=30")
        data = resp.get_json()
        assert "trend" in data["by_project"]["koan"]

    def test_project_specific_metrics(self, app_client, instance_dir):
        _write_outcomes(instance_dir, [
            _make_outcome(project="koan", outcome="productive"),
            _make_outcome(project="other", outcome="empty"),
        ])
        resp = app_client.get("/api/metrics?days=30&project=koan")
        data = resp.get_json()
        assert data["total_sessions"] == 1
        assert data["productive"] == 1
        assert "trend" in data

    def test_empty_outcomes(self, app_client, instance_dir):
        resp = app_client.get("/api/metrics")
        data = resp.get_json()
        assert data["total_sessions"] == 0
        assert data["trend"] == "stable"
