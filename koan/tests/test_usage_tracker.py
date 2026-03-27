"""Tests for usage_tracker.py — Usage parsing and autonomous mode decisions."""

import logging
import pytest
from pathlib import Path
from unittest.mock import patch
from app.usage_tracker import UsageTracker, _get_budget_mode, MALFORMED_DEFAULT_PCT


@pytest.fixture
def usage_file_standard(tmp_path):
    """Standard usage.md format."""
    usage = tmp_path / "usage.md"
    usage.write_text("""# Usage

```
usage date: 2026-02-01 - 13:05
Session (5hr) : 25% (reset in 3h)
Weekly (7 day) : 60% (Resets in 3d)
```
""")
    return usage


@pytest.fixture
def usage_file_high(tmp_path):
    """High usage (near exhaustion)."""
    usage = tmp_path / "usage.md"
    usage.write_text("""# Usage

```
Session (5hr) : 85% (reset in 1h)
Weekly (7 day) : 82% (Resets in 2d)
```
""")
    return usage


@pytest.fixture
def usage_file_low(tmp_path):
    """Low usage (plenty of budget)."""
    usage = tmp_path / "usage.md"
    usage.write_text("""# Usage

```
Session (5hr) : 10% (reset in 4h)
Weekly (7 day) : 25% (Resets in 5d)
```
""")
    return usage


@pytest.fixture
def usage_file_empty(tmp_path):
    """Empty usage file (fallback scenario)."""
    usage = tmp_path / "usage.md"
    usage.write_text("")
    return usage


class TestUsageParsing:
    """Test parsing of usage.md formats."""

    def test_parse_standard_format(self, usage_file_standard):
        """Parse standard usage.md with session and weekly percentages."""
        tracker = UsageTracker(usage_file_standard)

        assert tracker.session_pct == 25.0
        assert tracker.session_reset == "3h"
        assert tracker.weekly_pct == 60.0
        assert tracker.weekly_reset == "3d"

    def test_parse_high_usage(self, usage_file_high):
        """Parse high usage percentages."""
        tracker = UsageTracker(usage_file_high)

        assert tracker.session_pct == 85.0
        assert tracker.weekly_pct == 82.0

    def test_parse_low_usage(self, usage_file_low):
        """Parse low usage percentages."""
        tracker = UsageTracker(usage_file_low)

        assert tracker.session_pct == 10.0
        assert tracker.weekly_pct == 25.0

    def test_parse_empty_file(self, usage_file_empty):
        """Empty file has no content to parse — stays at 0.0 (no false conservative)."""
        tracker = UsageTracker(usage_file_empty)

        # Empty string after strip() → not malformed, just empty
        assert tracker.session_pct == 0.0
        assert tracker.weekly_pct == 0.0
        assert tracker.session_reset == "unknown"

    def test_parse_missing_file(self, tmp_path):
        """Handle missing usage.md file."""
        missing = tmp_path / "nonexistent.md"
        tracker = UsageTracker(missing)

        assert tracker.session_pct == 0.0
        assert tracker.weekly_pct == 0.0

    def test_parse_malformed_defaults_to_conservative(self, tmp_path):
        """Malformed usage.md defaults to conservative usage, not 0%."""
        usage = tmp_path / "usage.md"
        usage.write_text("This is not valid usage data\nRandom text here")
        tracker = UsageTracker(usage)

        assert tracker.session_pct == MALFORMED_DEFAULT_PCT
        assert tracker.weekly_pct == MALFORMED_DEFAULT_PCT

    def test_parse_malformed_logs_warning(self, tmp_path, caplog):
        """Malformed usage.md logs a warning."""
        usage = tmp_path / "usage.md"
        usage.write_text("garbage data")
        with caplog.at_level(logging.WARNING, logger="app.usage_tracker"):
            UsageTracker(usage)

        assert any("could not parse" in r.message for r in caplog.records)

    def test_parse_malformed_does_not_get_deep_mode(self, tmp_path):
        """Malformed usage.md must NOT result in deep mode (the actual bug)."""
        usage = tmp_path / "usage.md"
        usage.write_text("Broken format: session 50 weekly 70")
        tracker = UsageTracker(usage)
        mode = tracker.decide_mode()

        # 75% used → 15% remaining after safety → should be review or wait, NOT deep
        assert mode != "deep"

    def test_partial_parse_session_only(self, tmp_path):
        """If only session line parses, weekly stays 0 — no malformed default."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 40% (reset in 2h)\nWeekly line is broken")
        tracker = UsageTracker(usage)

        assert tracker.session_pct == 40.0
        assert tracker.weekly_pct == 0.0  # partial parse, not malformed

    def test_partial_parse_weekly_only(self, tmp_path):
        """If only weekly line parses, session stays 0 — no malformed default."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session line broken\nWeekly (7 day) : 50% (Resets in 3d)")
        tracker = UsageTracker(usage)

        assert tracker.session_pct == 0.0
        assert tracker.weekly_pct == 50.0


class TestRemainingBudget:
    """Test budget calculations with safety margin."""

    def test_remaining_budget_standard(self, usage_file_standard):
        """Calculate remaining budget with 10% safety margin."""
        tracker = UsageTracker(usage_file_standard)
        session_rem, weekly_rem = tracker.remaining_budget()

        # 100 - 25 (used) - 10 (safety) = 65
        assert session_rem == 65.0
        # 100 - 60 (used) - 10 (safety) = 30
        assert weekly_rem == 30.0

    def test_remaining_budget_high_usage(self, usage_file_high):
        """Remaining budget near exhaustion."""
        tracker = UsageTracker(usage_file_high)
        session_rem, weekly_rem = tracker.remaining_budget()

        # 100 - 85 - 10 = 5
        assert session_rem == 5.0
        # 100 - 82 - 10 = 8
        assert weekly_rem == 8.0

    def test_remaining_budget_low_usage(self, usage_file_low):
        """Remaining budget with plenty available."""
        tracker = UsageTracker(usage_file_low)
        session_rem, weekly_rem = tracker.remaining_budget()

        assert session_rem == 80.0  # 100 - 10 - 10
        assert weekly_rem == 65.0   # 100 - 25 - 10

    def test_remaining_budget_never_negative(self, tmp_path):
        """Remaining budget floors at zero."""
        usage = tmp_path / "usage.md"
        usage.write_text("""
Session (5hr) : 95% (reset in 1h)
Weekly (7 day) : 99% (Resets in 1d)
""")
        tracker = UsageTracker(usage)
        session_rem, weekly_rem = tracker.remaining_budget()

        assert session_rem == 0.0  # max(0, 100 - 95 - 10)
        assert weekly_rem == 0.0   # max(0, 100 - 99 - 10)


class TestCostEstimation:
    """Test run cost estimation logic."""

    def test_estimate_first_run_default(self, usage_file_standard):
        """First run uses conservative default (5%)."""
        tracker = UsageTracker(usage_file_standard, runs_completed=0)
        cost = tracker.estimate_run_cost()

        assert cost == 5.0

    def test_estimate_from_history(self, usage_file_standard):
        """Estimate based on session average after runs."""
        tracker = UsageTracker(usage_file_standard, runs_completed=5)
        # 25% used / 5 runs = 5% per run
        cost = tracker.estimate_run_cost()

        assert cost == 5.0

    def test_estimate_expensive_session(self, usage_file_high):
        """High usage per run."""
        tracker = UsageTracker(usage_file_high, runs_completed=3)
        # 85% / 3 runs ≈ 28.33% per run
        cost = tracker.estimate_run_cost()

        assert cost == pytest.approx(28.33, rel=0.01)


class TestModeDecisions:
    """Test autonomous mode decision logic."""

    def test_decide_deep_mode_high_budget(self, usage_file_low):
        """Deep mode when ample budget (>= 40%)."""
        tracker = UsageTracker(usage_file_low)
        mode = tracker.decide_mode()

        assert mode == "deep"

    def test_decide_implement_mode_medium_budget(self, usage_file_standard):
        """Implement mode with medium budget (15-40%)."""
        tracker = UsageTracker(usage_file_standard)
        mode = tracker.decide_mode()

        # Session: 65% remaining, Weekly: 30% remaining → min = 30%
        assert mode == "implement"

    def test_decide_review_mode_low_budget(self, tmp_path):
        """Review mode when low budget (15-30% remaining with default thresholds)."""
        usage = tmp_path / "usage.md"
        usage.write_text("""
Session (5hr) : 65% (reset in 1h)
Weekly (7 day) : 70% (Resets in 1d)
""")
        tracker = UsageTracker(usage)
        mode = tracker.decide_mode()

        # Session: 25% remaining, Weekly: 20% remaining → min = 20%
        # With defaults (warn=70, stop=85): 15 < 20 < 30 → review
        assert mode == "review"

    def test_decide_wait_mode_exhausted(self, usage_file_high):
        """Wait mode when budget exhausted (< 15% remaining with default thresholds)."""
        tracker = UsageTracker(usage_file_high)
        mode = tracker.decide_mode()

        # Session: 5% remaining, Weekly: 8% remaining → min = 5%
        # With defaults (stop=85): 5 < 15 → wait
        assert mode == "wait"


class TestOutputFormatting:
    """Test CLI output formatting."""

    def test_format_output_structure(self, usage_file_standard):
        """Output format: mode:available%:reason."""
        tracker = UsageTracker(usage_file_standard)
        mode = "implement"

        output = tracker.format_output(mode)
        parts = output.split(':')

        assert len(parts) == 3
        assert parts[0] == "implement"
        assert parts[1] == "30"  # min(65 session, 30 weekly)
        assert "30%" in parts[2] or "budget" in parts[2].lower()

    def test_get_decision_reason(self, usage_file_low):
        """Reason strings are descriptive."""
        tracker = UsageTracker(usage_file_low)

        reason = tracker.get_decision_reason("deep")
        assert "ample" in reason.lower() or "full" in reason.lower()

        reason = tracker.get_decision_reason("wait")
        assert "exhaust" in reason.lower()


class TestCanAffordRun:
    """Test can_afford_run() with cost multipliers across modes."""

    def test_review_cheapest(self, usage_file_standard):
        """Review mode costs 0.5x — affordable even with moderate budget."""
        tracker = UsageTracker(usage_file_standard, runs_completed=5)
        # base_cost = 25/5 = 5.0, review = 5*0.5 = 2.5
        # available = min(65, 30) = 30 → 2.5 <= 30
        assert tracker.can_afford_run("review") is True

    def test_implement_normal_cost(self, usage_file_standard):
        """Implement mode costs 1.0x."""
        tracker = UsageTracker(usage_file_standard, runs_completed=5)
        # base_cost = 5.0, implement = 5.0
        # available = 30 → 5.0 <= 30
        assert tracker.can_afford_run("implement") is True

    def test_deep_most_expensive(self, usage_file_standard):
        """Deep mode costs 2.0x."""
        tracker = UsageTracker(usage_file_standard, runs_completed=5)
        # base_cost = 5.0, deep = 10.0
        # available = 30 → 10.0 <= 30
        assert tracker.can_afford_run("deep") is True

    def test_cannot_afford_deep_near_exhaustion(self, usage_file_high):
        """Near-exhaustion: deep mode too expensive."""
        tracker = UsageTracker(usage_file_high, runs_completed=3)
        # base_cost = 85/3 ≈ 28.33, deep = 56.67
        # available = min(5, 8) = 5 → 56.67 > 5
        assert tracker.can_afford_run("deep") is False

    def test_cannot_afford_implement_near_exhaustion(self, usage_file_high):
        """Near-exhaustion: implement also too expensive."""
        tracker = UsageTracker(usage_file_high, runs_completed=3)
        # base_cost ≈ 28.33, implement = 28.33
        # available = 5 → 28.33 > 5
        assert tracker.can_afford_run("implement") is False

    def test_review_still_possible_near_exhaustion(self, tmp_path):
        """Review might still fit when others don't."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 80% (reset in 1h)\nWeekly (7 day) : 70% (Resets in 2d)")
        tracker = UsageTracker(usage, runs_completed=10)
        # base_cost = 80/10 = 8.0, review = 4.0
        # available = min(10, 20) = 10 → 4.0 <= 10
        assert tracker.can_afford_run("review") is True

    def test_unknown_mode_defaults_to_1x(self, usage_file_standard):
        """Unknown mode uses 1.0x multiplier (fallback)."""
        tracker = UsageTracker(usage_file_standard, runs_completed=5)
        assert tracker.can_afford_run("unknown_mode") is True

    def test_first_run_uses_default_cost(self, usage_file_low):
        """First run (0 completed) uses 5.0 default cost."""
        tracker = UsageTracker(usage_file_low, runs_completed=0)
        # base_cost = 5.0 (default), deep = 10.0
        # available = min(80, 65) = 65 → 10.0 <= 65
        assert tracker.can_afford_run("deep") is True

    def test_exact_boundary(self, tmp_path):
        """Cost exactly equals available budget — should be affordable."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 80% (reset in 1h)\nWeekly (7 day) : 80% (Resets in 2d)")
        tracker = UsageTracker(usage, runs_completed=10)
        # base_cost = 80/10 = 8.0, implement = 8.0
        # available = min(10, 10) = 10 → 8.0 <= 10
        assert tracker.can_afford_run("implement") is True
        # deep = 16.0 → 16.0 > 10
        assert tracker.can_afford_run("deep") is False


class TestBudgetMode:
    """Test budget_mode parameter for controlling which limits are active."""

    def test_full_mode_uses_both_limits(self, tmp_path):
        """budget_mode='full': min(session, weekly) determines available budget."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 20% (reset in 4h)\nWeekly (7 day) : 95% (Resets in 1d)")
        tracker = UsageTracker(usage, budget_mode="full")

        session_rem, weekly_rem = tracker.remaining_budget()
        assert session_rem == 70.0  # 100 - 20 - 10
        assert weekly_rem == 0.0    # max(0, 100 - 95 - 10)
        assert tracker.decide_mode() == "wait"

    def test_session_only_ignores_weekly(self, tmp_path):
        """budget_mode='session_only': weekly limit is ignored."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 20% (reset in 4h)\nWeekly (7 day) : 95% (Resets in 1d)")
        tracker = UsageTracker(usage, budget_mode="session_only")

        session_rem, weekly_rem = tracker.remaining_budget()
        assert session_rem == 70.0  # 100 - 20 - 10
        assert weekly_rem == 90.0   # ignored, returns 90
        # min(70, 90) = 70 → deep mode
        assert tracker.decide_mode() == "deep"

    def test_disabled_mode_always_deep(self, tmp_path):
        """budget_mode='disabled': always returns high budget."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 95% (reset in 1h)\nWeekly (7 day) : 99% (Resets in 1d)")
        tracker = UsageTracker(usage, budget_mode="disabled")

        session_rem, weekly_rem = tracker.remaining_budget()
        assert session_rem == 90.0
        assert weekly_rem == 90.0
        assert tracker.decide_mode() == "deep"

    def test_session_only_still_respects_session(self, tmp_path):
        """Session-only mode still pauses when session is exhausted."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 98% (reset in 0m)\nWeekly (7 day) : 10% (Resets in 5d)")
        tracker = UsageTracker(usage, budget_mode="session_only")

        session_rem, weekly_rem = tracker.remaining_budget()
        assert session_rem == 0.0   # 100 - 98 - 10 → max(0, -8) = 0
        assert weekly_rem == 90.0   # ignored
        # min(0, 90) = 0 → wait mode
        assert tracker.decide_mode() == "wait"

    def test_default_budget_mode(self, usage_file_standard):
        """Default budget_mode is 'full'."""
        tracker = UsageTracker(usage_file_standard)
        assert tracker.budget_mode == "full"

    def test_budget_mode_affects_format_output(self, tmp_path):
        """Format output reflects the effective available budget."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 20% (reset in 4h)\nWeekly (7 day) : 95% (Resets in 1d)")

        # Full mode: available = 0
        full = UsageTracker(usage, budget_mode="full")
        assert "0" in full.format_output(full.decide_mode())

        # Session-only: available = 70
        session = UsageTracker(usage, budget_mode="session_only")
        output = session.format_output(session.decide_mode())
        assert "70" in output

    def test_session_only_can_afford_run(self, tmp_path):
        """Session-only mode allows runs even with high weekly usage."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 30% (reset in 3h)\nWeekly (7 day) : 95% (Resets in 1d)")
        tracker = UsageTracker(usage, runs_completed=5, budget_mode="session_only")
        # available = min(60, 90) = 60
        assert tracker.can_afford_run("deep") is True


class TestGetBudgetMode:
    """Test _get_budget_mode() config reading."""

    def test_default_is_session_only(self):
        """Default budget_mode when not configured is 'session_only'."""
        with patch("app.utils.load_config", return_value={}):
            assert _get_budget_mode() == "session_only"

    def test_reads_from_config(self):
        """Reads usage.budget_mode from config.yaml."""
        with patch("app.utils.load_config", return_value={
            "usage": {"budget_mode": "full"}
        }):
            assert _get_budget_mode() == "full"

    def test_disabled_from_config(self):
        """Disabled mode from config."""
        with patch("app.utils.load_config", return_value={
            "usage": {"budget_mode": "disabled"}
        }):
            assert _get_budget_mode() == "disabled"

    def test_invalid_value_falls_back(self):
        """Invalid budget_mode value falls back to session_only."""
        with patch("app.utils.load_config", return_value={
            "usage": {"budget_mode": "bogus"}
        }):
            assert _get_budget_mode() == "session_only"

    def test_config_load_error_falls_back(self):
        """Config load failure falls back to session_only."""
        with patch("app.utils.load_config", side_effect=OSError("nope")):
            assert _get_budget_mode() == "session_only"
