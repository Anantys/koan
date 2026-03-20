"""Tests for cost_tracker.py — Structured per-model/project token tracking."""

import json
import pytest
from datetime import date, datetime, timedelta
from pathlib import Path

from app.cost_tracker import (
    record_usage,
    summarize_day,
    summarize_range,
    summarize_by_project,
    summarize_by_model,
    estimate_cost,
    estimate_cache_savings,
    daily_series,
    get_pricing_config,
    format_cache_summary,
    format_mission_cache_line,
    _read_jsonl_for_date,
    _read_jsonl_range,
    _aggregate,
    _format_tokens,
)


@pytest.fixture
def instance_dir(tmp_path):
    """Create a temporary instance directory."""
    d = tmp_path / "instance"
    d.mkdir()
    return d


@pytest.fixture
def usage_dir(instance_dir):
    """Create the usage subdirectory."""
    d = instance_dir / "usage"
    d.mkdir()
    return d


@pytest.fixture
def sample_entries():
    """A set of sample usage entries for testing aggregation."""
    return [
        {"ts": "2026-03-08T10:00:00", "project": "koan", "model": "claude-sonnet-4-20250514",
         "input_tokens": 1000, "output_tokens": 500, "mode": "implement", "mission": "Fix bug"},
        {"ts": "2026-03-08T11:00:00", "project": "koan", "model": "claude-opus-4-20250514",
         "input_tokens": 3000, "output_tokens": 1500, "mode": "deep", "mission": "Refactor"},
        {"ts": "2026-03-08T12:00:00", "project": "other", "model": "claude-sonnet-4-20250514",
         "input_tokens": 800, "output_tokens": 200, "mode": "review", "mission": "Review PR"},
    ]


class TestRecordUsage:
    def test_creates_jsonl_file(self, instance_dir):
        result = record_usage(
            instance_dir, "koan", "claude-sonnet-4-20250514",
            1000, 500, "implement", "Fix bug",
        )
        assert result is True
        today = date.today().isoformat()
        jsonl_path = instance_dir / "usage" / f"{today}.jsonl"
        assert jsonl_path.exists()

    def test_appends_valid_json(self, instance_dir):
        record_usage(instance_dir, "koan", "sonnet", 100, 50)
        record_usage(instance_dir, "other", "opus", 200, 100)

        today = date.today().isoformat()
        lines = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["project"] == "koan"
        assert entry1["input_tokens"] == 100

        entry2 = json.loads(lines[1])
        assert entry2["project"] == "other"

    def test_creates_usage_dir(self, instance_dir):
        """Usage dir should be auto-created if missing."""
        assert not (instance_dir / "usage").exists()
        record_usage(instance_dir, "p", "m", 1, 1)
        assert (instance_dir / "usage").exists()

    def test_empty_project_defaults_to_global(self, instance_dir):
        record_usage(instance_dir, "", "sonnet", 100, 50)
        today = date.today().isoformat()
        line = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip()
        entry = json.loads(line)
        assert entry["project"] == "_global"

    def test_empty_model_defaults_to_unknown(self, instance_dir):
        record_usage(instance_dir, "p", "", 100, 50)
        today = date.today().isoformat()
        line = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip()
        entry = json.loads(line)
        assert entry["model"] == "unknown"

    def test_zero_tokens_recorded(self, instance_dir):
        """Zero tokens should still be recorded (failed/aborted runs)."""
        result = record_usage(instance_dir, "p", "m", 0, 0)
        assert result is True
        today = date.today().isoformat()
        lines = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1

    def test_compact_json_format(self, instance_dir):
        """JSONL lines should be compact (no spaces after separators)."""
        record_usage(instance_dir, "p", "m", 100, 50)
        today = date.today().isoformat()
        line = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip()
        assert ": " not in line  # No pretty-printing


class TestReadJsonl:
    def test_reads_existing_file(self, usage_dir):
        today = date.today()
        jsonl_path = usage_dir / f"{today.isoformat()}.jsonl"
        jsonl_path.write_text(
            json.dumps({"input_tokens": 100, "output_tokens": 50}) + "\n"
        )
        entries = _read_jsonl_for_date(usage_dir, today)
        assert len(entries) == 1
        assert entries[0]["input_tokens"] == 100

    def test_skips_invalid_lines(self, usage_dir):
        today = date.today()
        jsonl_path = usage_dir / f"{today.isoformat()}.jsonl"
        jsonl_path.write_text(
            '{"input_tokens": 100}\n'
            'corrupt line\n'
            '{"input_tokens": 200}\n'
        )
        entries = _read_jsonl_for_date(usage_dir, today)
        assert len(entries) == 2

    def test_returns_empty_for_missing_file(self, usage_dir):
        entries = _read_jsonl_for_date(usage_dir, date(2020, 1, 1))
        assert entries == []

    def test_reads_date_range(self, usage_dir):
        d1 = date(2026, 3, 7)
        d2 = date(2026, 3, 8)
        (usage_dir / f"{d1.isoformat()}.jsonl").write_text(
            json.dumps({"input_tokens": 100}) + "\n"
        )
        (usage_dir / f"{d2.isoformat()}.jsonl").write_text(
            json.dumps({"input_tokens": 200}) + "\n"
        )
        entries = _read_jsonl_range(usage_dir, d1, d2)
        assert len(entries) == 2

    def test_skips_empty_lines(self, usage_dir):
        today = date.today()
        jsonl_path = usage_dir / f"{today.isoformat()}.jsonl"
        jsonl_path.write_text('\n{"input_tokens": 100}\n\n')
        entries = _read_jsonl_for_date(usage_dir, today)
        assert len(entries) == 1


class TestAggregate:
    def test_empty_entries(self):
        result = _aggregate([])
        assert result["total_input"] == 0
        assert result["total_output"] == 0
        assert result["count"] == 0
        assert result["by_project"] == {}
        assert result["by_model"] == {}

    def test_single_entry(self):
        entries = [{"input_tokens": 100, "output_tokens": 50, "project": "p", "model": "m"}]
        result = _aggregate(entries)
        assert result["total_input"] == 100
        assert result["total_output"] == 50
        assert result["count"] == 1
        assert result["by_project"]["p"]["input_tokens"] == 100
        assert result["by_model"]["m"]["output_tokens"] == 50

    def test_multiple_projects(self, sample_entries):
        result = _aggregate(sample_entries)
        assert result["total_input"] == 4800
        assert result["total_output"] == 2200
        assert result["count"] == 3
        assert "koan" in result["by_project"]
        assert "other" in result["by_project"]
        assert result["by_project"]["koan"]["count"] == 2
        assert result["by_project"]["other"]["count"] == 1

    def test_multiple_models(self, sample_entries):
        result = _aggregate(sample_entries)
        sonnet = result["by_model"]["claude-sonnet-4-20250514"]
        opus = result["by_model"]["claude-opus-4-20250514"]
        assert sonnet["count"] == 2
        assert opus["count"] == 1
        assert sonnet["input_tokens"] == 1800  # 1000 + 800
        assert opus["input_tokens"] == 3000

    def test_missing_fields_default(self):
        entries = [{"project": "p"}]  # No token fields
        result = _aggregate(entries)
        assert result["total_input"] == 0
        assert result["total_output"] == 0
        assert result["count"] == 1


class TestSummarize:
    def test_summarize_day(self, instance_dir):
        record_usage(instance_dir, "koan", "sonnet", 1000, 500)
        record_usage(instance_dir, "other", "opus", 2000, 800)
        result = summarize_day(instance_dir)
        assert result["total_input"] == 3000
        assert result["total_output"] == 1300
        assert result["count"] == 2

    def test_summarize_day_empty(self, instance_dir):
        result = summarize_day(instance_dir, date(2020, 1, 1))
        assert result["count"] == 0

    def test_summarize_range(self, instance_dir):
        usage_dir = instance_dir / "usage"
        usage_dir.mkdir(exist_ok=True)
        d1 = date(2026, 3, 7)
        d2 = date(2026, 3, 8)
        (usage_dir / f"{d1.isoformat()}.jsonl").write_text(
            json.dumps({"input_tokens": 100, "output_tokens": 50, "project": "p", "model": "m"}) + "\n"
        )
        (usage_dir / f"{d2.isoformat()}.jsonl").write_text(
            json.dumps({"input_tokens": 200, "output_tokens": 100, "project": "p", "model": "m"}) + "\n"
        )
        result = summarize_range(instance_dir, d1, d2)
        assert result["total_input"] == 300
        assert result["count"] == 2

    def test_summarize_by_project(self, instance_dir):
        record_usage(instance_dir, "koan", "sonnet", 1000, 500)
        record_usage(instance_dir, "koan", "sonnet", 500, 200)
        record_usage(instance_dir, "other", "opus", 2000, 800)
        result = summarize_by_project(instance_dir, days=1)
        assert result["koan"]["input_tokens"] == 1500
        assert result["koan"]["count"] == 2
        assert result["other"]["count"] == 1

    def test_summarize_by_model(self, instance_dir):
        record_usage(instance_dir, "koan", "claude-sonnet-4-20250514", 1000, 500)
        record_usage(instance_dir, "koan", "claude-opus-4-20250514", 2000, 800)
        result = summarize_by_model(instance_dir, days=1)
        assert "claude-sonnet-4-20250514" in result
        assert "claude-opus-4-20250514" in result


class TestCacheTracking:
    """Tests for prompt cache observability in cost tracking."""

    def test_record_with_cache_fields(self, instance_dir):
        """Cache fields should be recorded in JSONL when non-zero."""
        record_usage(
            instance_dir, "koan", "claude-opus-4-6",
            24, 5916, "deep", "Fix bug",
            cache_creation_input_tokens=41777,
            cache_read_input_tokens=1036218,
            cost_usd=0.927,
        )
        today = date.today().isoformat()
        line = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip()
        entry = json.loads(line)
        assert entry["cache_creation_input_tokens"] == 41777
        assert entry["cache_read_input_tokens"] == 1036218
        assert entry["cost_usd"] == 0.927

    def test_record_without_cache_fields_stays_compact(self, instance_dir):
        """Zero cache fields should NOT appear in JSONL (backward compat)."""
        record_usage(instance_dir, "koan", "sonnet", 100, 50)
        today = date.today().isoformat()
        line = (instance_dir / "usage" / f"{today}.jsonl").read_text().strip()
        entry = json.loads(line)
        assert "cache_creation_input_tokens" not in entry
        assert "cache_read_input_tokens" not in entry
        assert "cost_usd" not in entry

    def test_aggregate_cache_totals(self):
        """Aggregation should sum cache tokens across entries."""
        entries = [
            {"input_tokens": 24, "output_tokens": 500, "project": "p", "model": "m",
             "cache_creation_input_tokens": 40000, "cache_read_input_tokens": 100000,
             "cost_usd": 0.5},
            {"input_tokens": 50, "output_tokens": 300, "project": "p", "model": "m",
             "cache_read_input_tokens": 100000, "cost_usd": 0.3},
        ]
        result = _aggregate(entries)
        assert result["cache_creation_input_tokens"] == 40000
        assert result["cache_read_input_tokens"] == 200000
        assert result["total_cost_usd"] == pytest.approx(0.8)

    def test_aggregate_cache_hit_rate(self):
        """Cache hit rate = cache_read / total_all_input."""
        entries = [
            {"input_tokens": 100, "output_tokens": 50, "project": "p", "model": "m",
             "cache_read_input_tokens": 900},
        ]
        result = _aggregate(entries)
        # total_all_input = 100 + 900 = 1000, cache_read = 900
        assert result["cache_hit_rate"] == pytest.approx(0.9)

    def test_aggregate_no_cache_hit_rate_zero(self):
        """No cache tokens should give 0% hit rate."""
        entries = [
            {"input_tokens": 100, "output_tokens": 50, "project": "p", "model": "m"},
        ]
        result = _aggregate(entries)
        assert result["cache_hit_rate"] == 0.0

    def test_aggregate_empty_has_cache_fields(self):
        """Empty aggregation should include cache fields."""
        result = _aggregate([])
        assert result["cache_creation_input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0
        assert result["cache_hit_rate"] == 0.0
        assert result["total_cost_usd"] == 0.0

    def test_summarize_day_includes_cache(self, instance_dir):
        """Day summary should include cache metrics."""
        record_usage(
            instance_dir, "koan", "opus",
            24, 5000, cache_read_input_tokens=100000,
        )
        result = summarize_day(instance_dir)
        assert result["cache_read_input_tokens"] == 100000
        assert result["cache_hit_rate"] > 0

    def test_estimate_cache_savings_from_pricing(self):
        summary = {
            "by_model": {
                "claude-sonnet-4-20250514": {
                    "cache_read_input_tokens": 1_000_000,
                },
            }
        }
        pricing = {"sonnet": {"input": 3.0, "output": 15.0}}
        # 1M read tokens * $3/M input * 90% savings
        assert estimate_cache_savings(summary, pricing) == pytest.approx(2.7)

    def test_estimate_cache_savings_none_without_pricing(self):
        summary = {"by_model": {"m": {"cache_read_input_tokens": 1000}}}
        assert estimate_cache_savings(summary, None) is None

    def test_daily_series_includes_cache_fields(self, instance_dir):
        record_usage(
            instance_dir,
            "koan",
            "claude-sonnet-4-20250514",
            100,
            50,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=800,
        )
        today = date.today()
        rows = daily_series(instance_dir, today, today)
        assert len(rows) == 1
        row = rows[0]
        assert row["cache_creation_input_tokens"] == 200
        assert row["cache_read_input_tokens"] == 800
        assert row["cache_hit_rate"] > 0


class TestEstimateCost:
    def test_returns_none_without_pricing(self):
        tokens = {"input_tokens": 1000, "output_tokens": 500, "model": "sonnet"}
        assert estimate_cost(tokens) is None
        assert estimate_cost(tokens, None) is None

    def test_calculates_cost(self):
        pricing = {
            "sonnet": {"input": 3.0, "output": 15.0},
        }
        tokens = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "model": "claude-sonnet-4"}
        cost = estimate_cost(tokens, pricing)
        assert cost == pytest.approx(18.0)

    def test_unknown_model_returns_none(self):
        pricing = {"sonnet": {"input": 3.0, "output": 15.0}}
        tokens = {"input_tokens": 1000, "output_tokens": 500, "model": "gpt-4"}
        assert estimate_cost(tokens, pricing) is None

    def test_opus_pricing(self):
        pricing = {
            "opus": {"input": 15.0, "output": 75.0},
        }
        tokens = {"input_tokens": 1_000_000, "output_tokens": 100_000, "model": "claude-opus-4-20250514"}
        cost = estimate_cost(tokens, pricing)
        assert cost == pytest.approx(15.0 + 7.5)

    def test_haiku_pricing(self):
        pricing = {"haiku": {"input": 0.25, "output": 1.25}}
        tokens = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "model": "claude-haiku-4-5"}
        cost = estimate_cost(tokens, pricing)
        assert cost == pytest.approx(1.5)


class TestGetPricingConfig:
    def test_returns_none_without_config(self):
        assert get_pricing_config({}) is None

    def test_returns_pricing_from_config(self):
        config = {"usage": {"pricing": {"sonnet": {"input": 3.0, "output": 15.0}}}}
        pricing = get_pricing_config(config)
        assert pricing["sonnet"]["input"] == 3.0

    def test_returns_none_for_non_dict_pricing(self):
        config = {"usage": {"pricing": "invalid"}}
        assert get_pricing_config(config) is None


class TestFormatCacheSummary:
    def test_returns_empty_when_no_data(self, instance_dir):
        assert format_cache_summary(instance_dir) == ""

    def test_returns_summary_with_cache_data(self, instance_dir):
        record_usage(
            instance_dir, "koan", "opus",
            1000, 500,
            cache_read_input_tokens=9000,
            cache_creation_input_tokens=1000,
        )
        result = format_cache_summary(instance_dir)
        assert "hit rate" in result
        assert "read" in result
        assert "created" in result

    def test_summary_shows_zero_pct_for_creation_only(self, instance_dir):
        record_usage(
            instance_dir, "koan", "opus",
            1000, 500,
            cache_creation_input_tokens=5000,
        )
        result = format_cache_summary(instance_dir)
        assert "0% hit rate" in result


class TestFormatMissionCacheLine:
    def test_empty_when_no_cache(self):
        assert format_mission_cache_line(0, 0, 1000) == ""

    def test_shows_hit_rate(self):
        result = format_mission_cache_line(
            cache_read=9000, cache_create=0, input_tokens=1000,
        )
        assert "90% hit" in result
        assert "9.0k read" in result

    def test_shows_creation(self):
        result = format_mission_cache_line(
            cache_read=0, cache_create=5000, input_tokens=1000,
        )
        assert "0% hit" in result
        assert "5.0k created" in result

    def test_mixed(self):
        result = format_mission_cache_line(
            cache_read=4000, cache_create=1000, input_tokens=5000,
        )
        assert "hit" in result
        assert "read" in result
        assert "created" in result


class TestFormatTokens:
    def test_small(self):
        assert _format_tokens(500) == "500"

    def test_thousands(self):
        assert _format_tokens(1500) == "1.5k"

    def test_millions(self):
        assert _format_tokens(1_500_000) == "1.5M"


class TestDailySeriesCacheFields:
    def test_includes_cache_fields(self, instance_dir):
        record_usage(
            instance_dir, "koan", "opus",
            1000, 500,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=1000,
        )
        from app.cost_tracker import daily_series
        series = daily_series(instance_dir, date.today(), date.today())
        assert len(series) == 1
        day = series[0]
        assert day["cache_read_input_tokens"] == 3000
        assert day["cache_creation_input_tokens"] == 1000
        assert day["cache_hit_rate"] > 0
