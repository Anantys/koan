"""Tests for the /quota core skill — live LLM quota check."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path, args=""):
    """Create a SkillContext for /quota."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="quota",
        args=args,
    )


def _write_usage_state(instance_dir, **overrides):
    """Write a usage_state.json with sensible defaults."""
    now = datetime.now()
    state = {
        "session_start": now.isoformat(),
        "session_tokens": 150_000,
        "weekly_start": now.isoformat(),
        "weekly_tokens": 2_000_000,
        "runs": 5,
    }
    state.update(overrides)
    state_path = instance_dir / "usage_state.json"
    state_path.write_text(json.dumps(state))
    return state_path


def _write_cli_stats(path, **overrides):
    """Write a stats-cache.json with sensible defaults."""
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "version": 2,
        "lastComputedDate": today,
        "dailyActivity": [
            {
                "date": today,
                "messageCount": 500,
                "sessionCount": 12,
                "toolCallCount": 200,
            }
        ],
        "dailyModelTokens": [
            {
                "date": today,
                "tokensByModel": {
                    "claude-opus-4-6": 100_000,
                    "claude-haiku-4-5-20251001": 5_000,
                },
            }
        ],
        "modelUsage": {
            "claude-opus-4-6": {
                "inputTokens": 500_000,
                "outputTokens": 80_000,
                "cacheReadInputTokens": 10_000_000,
                "cacheCreationInputTokens": 2_000_000,
                "webSearchRequests": 0,
                "costUSD": 0,
            },
            "claude-haiku-4-5-20251001": {
                "inputTokens": 20_000,
                "outputTokens": 3_000,
                "cacheReadInputTokens": 500_000,
                "cacheCreationInputTokens": 100_000,
                "webSearchRequests": 0,
                "costUSD": 0,
            },
        },
        "totalSessions": 50,
        "totalMessages": 2000,
    }
    stats.update(overrides)
    path.write_text(json.dumps(stats))
    return path


# ---------------------------------------------------------------------------
# Handler: handle() main entry point
# ---------------------------------------------------------------------------

class TestQuotaHandler:
    """Test the quota skill handler directly."""

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    def test_no_data_available(self, tmp_path):
        """Handler works when no usage state or CLI stats exist."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        result = handle(ctx)
        assert "first run" in result.lower() or "no internal" in result.lower()
        assert "Agent" in result

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_with_usage_state(self, mock_config, tmp_path):
        """Handler shows session/weekly data from usage_state.json."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)
        result = handle(ctx)
        assert "Session quota" in result
        assert "Weekly quota" in result
        assert "%" in result

    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_with_cli_stats(self, mock_config, tmp_path):
        """Handler shows Claude CLI stats when available."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)

        stats_path = tmp_path / "stats-cache.json"
        _write_cli_stats(stats_path)

        with patch("skills.core.quota.handler.STATS_CACHE_PATH", stats_path):
            result = handle(ctx)

        assert "Claude CLI stats" in result
        assert "Today:" in result
        assert "Opus" in result

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_shows_agent_state_running(self, mock_config, tmp_path):
        """Agent state shows 'running' when no pause/stop files."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)
        result = handle(ctx)
        assert "running" in result

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_shows_agent_state_paused_quota(self, mock_config, tmp_path):
        """Agent state shows 'paused (quota exhausted)' when paused for quota."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)
        (tmp_path / ".koan-pause").write_text("quota\n1234567890\n")
        result = handle(ctx)
        assert "paused" in result
        assert "quota" in result

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_shows_loop_status(self, mock_config, tmp_path):
        """Shows .koan-status content in agent state."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)
        (tmp_path / ".koan-status").write_text("sleeping (next run in 3m)")
        result = handle(ctx)
        assert "sleeping" in result


# ---------------------------------------------------------------------------
# _format_tokens
# ---------------------------------------------------------------------------

class TestFormatTokens:
    """Test human-friendly token formatting."""

    def test_small_number(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(500) == "500"

    def test_thousands(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(1_500) == "1.5k"

    def test_exact_thousands(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(10_000) == "10.0k"

    def test_millions(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(2_500_000) == "2.5M"

    def test_zero(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(0) == "0"

    def test_just_under_thousand(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(999) == "999"

    def test_exactly_one_million(self):
        from skills.core.quota.handler import _format_tokens
        assert _format_tokens(1_000_000) == "1.0M"


# ---------------------------------------------------------------------------
# _progress_bar
# ---------------------------------------------------------------------------

class TestProgressBar:
    """Test text progress bar rendering."""

    def test_zero_percent(self):
        from skills.core.quota.handler import _progress_bar
        assert _progress_bar(0) == "[..........]"

    def test_hundred_percent(self):
        from skills.core.quota.handler import _progress_bar
        assert _progress_bar(100) == "[==========]"

    def test_fifty_percent(self):
        from skills.core.quota.handler import _progress_bar
        bar = _progress_bar(50)
        assert bar.count("=") == 5
        assert bar.count(".") == 5

    def test_over_hundred_clamped(self):
        from skills.core.quota.handler import _progress_bar
        assert _progress_bar(150) == "[==========]"

    def test_custom_width(self):
        from skills.core.quota.handler import _progress_bar
        bar = _progress_bar(50, width=20)
        assert bar.count("=") == 10
        assert bar.count(".") == 10


# ---------------------------------------------------------------------------
# _time_remaining
# ---------------------------------------------------------------------------

class TestTimeRemaining:
    """Test time remaining calculation."""

    def test_future_reset(self):
        from skills.core.quota.handler import _time_remaining
        start = (datetime.now() - timedelta(hours=3)).isoformat()
        result = _time_remaining(start, 5)
        # ~2h remaining
        assert "h" in result

    def test_past_reset(self):
        from skills.core.quota.handler import _time_remaining
        start = (datetime.now() - timedelta(hours=10)).isoformat()
        result = _time_remaining(start, 5)
        assert result == "now"

    def test_invalid_start(self):
        from skills.core.quota.handler import _time_remaining
        assert _time_remaining("not-a-date", 5) == "?"

    def test_none_start(self):
        from skills.core.quota.handler import _time_remaining
        assert _time_remaining(None, 5) == "?"

    def test_minutes_only(self):
        from skills.core.quota.handler import _time_remaining
        start = (datetime.now() - timedelta(hours=4, minutes=30)).isoformat()
        result = _time_remaining(start, 5)
        assert "m" in result
        assert "h" not in result


# ---------------------------------------------------------------------------
# _short_model_name
# ---------------------------------------------------------------------------

class TestShortModelName:
    """Test model ID shortening."""

    def test_opus(self):
        from skills.core.quota.handler import _short_model_name
        assert _short_model_name("claude-opus-4-6") == "Opus"

    def test_sonnet(self):
        from skills.core.quota.handler import _short_model_name
        assert _short_model_name("claude-sonnet-4-5-20250929") == "Sonnet"

    def test_haiku(self):
        from skills.core.quota.handler import _short_model_name
        assert _short_model_name("claude-haiku-4-5-20251001") == "Haiku"

    def test_unknown_model(self):
        from skills.core.quota.handler import _short_model_name
        result = _short_model_name("claude-future-99")
        assert result == "future"


# ---------------------------------------------------------------------------
# _apply_resets
# ---------------------------------------------------------------------------

class TestApplyResets:
    """Test session/weekly reset logic."""

    def test_session_reset_when_expired(self):
        from skills.core.quota.handler import _apply_resets
        old_start = (datetime.now() - timedelta(hours=6)).isoformat()
        state = {
            "session_start": old_start,
            "session_tokens": 100_000,
            "weekly_start": datetime.now().isoformat(),
            "weekly_tokens": 500_000,
            "runs": 10,
        }
        result = _apply_resets(state)
        assert result["session_tokens"] == 0
        assert result["runs"] == 0
        # Weekly should be untouched
        assert result["weekly_tokens"] == 500_000

    def test_session_not_reset_when_active(self):
        from skills.core.quota.handler import _apply_resets
        recent_start = (datetime.now() - timedelta(hours=2)).isoformat()
        state = {
            "session_start": recent_start,
            "session_tokens": 100_000,
            "weekly_start": datetime.now().isoformat(),
            "weekly_tokens": 500_000,
            "runs": 3,
        }
        result = _apply_resets(state)
        assert result["session_tokens"] == 100_000
        assert result["runs"] == 3

    def test_weekly_reset_after_7_days(self):
        from skills.core.quota.handler import _apply_resets
        old_start = (datetime.now() - timedelta(days=8)).isoformat()
        state = {
            "session_start": datetime.now().isoformat(),
            "session_tokens": 0,
            "weekly_start": old_start,
            "weekly_tokens": 3_000_000,
            "runs": 0,
        }
        result = _apply_resets(state)
        assert result["weekly_tokens"] == 0

    def test_handles_missing_keys(self):
        from skills.core.quota.handler import _apply_resets
        state = {}
        result = _apply_resets(state)
        assert "session_start" in result
        assert "weekly_start" in result


# ---------------------------------------------------------------------------
# _format_koan_usage
# ---------------------------------------------------------------------------

class TestFormatKoanUsage:
    """Test internal usage formatting."""

    def test_includes_progress_bar(self):
        from skills.core.quota.handler import _format_koan_usage
        state = {
            "session_tokens": 250_000,
            "weekly_tokens": 2_500_000,
            "session_start": datetime.now().isoformat(),
            "runs": 5,
        }
        result = _format_koan_usage(state, 500_000, 5_000_000)
        assert "[" in result and "]" in result
        assert "50%" in result
        assert "5 run(s)" in result

    def test_zero_usage(self):
        from skills.core.quota.handler import _format_koan_usage
        state = {
            "session_tokens": 0,
            "weekly_tokens": 0,
            "session_start": datetime.now().isoformat(),
            "runs": 0,
        }
        result = _format_koan_usage(state, 500_000, 5_000_000)
        assert "0%" in result
        assert "0 run(s)" in result

    def test_max_usage_capped_at_100(self):
        from skills.core.quota.handler import _format_koan_usage
        state = {
            "session_tokens": 1_000_000,
            "weekly_tokens": 10_000_000,
            "session_start": datetime.now().isoformat(),
            "runs": 20,
        }
        result = _format_koan_usage(state, 500_000, 5_000_000)
        assert "100%" in result


# ---------------------------------------------------------------------------
# _format_cli_stats
# ---------------------------------------------------------------------------

class TestFormatCliStats:
    """Test Claude CLI stats formatting."""

    def test_full_stats(self):
        from skills.core.quota.handler import _format_cli_stats
        today = datetime.now().strftime("%Y-%m-%d")
        stats = {
            "dailyActivity": [
                {"date": today, "messageCount": 300, "sessionCount": 8, "toolCallCount": 120}
            ],
            "dailyModelTokens": [
                {"date": today, "tokensByModel": {"claude-opus-4-6": 50_000}}
            ],
            "modelUsage": {
                "claude-opus-4-6": {
                    "inputTokens": 200_000,
                    "outputTokens": 30_000,
                    "cacheReadInputTokens": 5_000_000,
                }
            },
            "totalSessions": 100,
            "totalMessages": 5000,
        }
        result = _format_cli_stats(stats)
        assert "300" in result
        assert "8 sessions" in result
        assert "Opus" in result
        assert "100" in result

    def test_empty_daily_activity(self):
        from skills.core.quota.handler import _format_cli_stats
        stats = {
            "dailyActivity": [],
            "dailyModelTokens": [],
            "modelUsage": {},
            "totalSessions": 0,
            "totalMessages": 0,
        }
        result = _format_cli_stats(stats)
        assert "Claude CLI stats" in result

    def test_no_today_data(self):
        """Stats exist but nothing for today."""
        from skills.core.quota.handler import _format_cli_stats
        stats = {
            "dailyActivity": [
                {"date": "2026-01-01", "messageCount": 100, "sessionCount": 3, "toolCallCount": 50}
            ],
            "dailyModelTokens": [],
            "modelUsage": {},
            "totalSessions": 10,
            "totalMessages": 400,
        }
        result = _format_cli_stats(stats)
        assert "Claude CLI stats" in result
        assert "Today:" not in result


# ---------------------------------------------------------------------------
# _format_agent_state
# ---------------------------------------------------------------------------

class TestFormatAgentState:
    """Test agent state formatting."""

    def test_running_state(self, tmp_path):
        from skills.core.quota.handler import _format_agent_state
        result = _format_agent_state(tmp_path)
        assert "running" in result

    def test_paused_state(self, tmp_path):
        from skills.core.quota.handler import _format_agent_state
        (tmp_path / ".koan-pause").write_text("123")
        result = _format_agent_state(tmp_path)
        assert "paused" in result

    def test_paused_with_quota_reason(self, tmp_path):
        from skills.core.quota.handler import _format_agent_state
        (tmp_path / ".koan-pause").write_text("quota\n1234567890\n")
        result = _format_agent_state(tmp_path)
        assert "quota exhausted" in result

    def test_paused_with_max_runs_reason(self, tmp_path):
        from skills.core.quota.handler import _format_agent_state
        (tmp_path / ".koan-pause").write_text("max_runs\n1234567890\n")
        result = _format_agent_state(tmp_path)
        assert "max runs" in result

    def test_stopping_state(self, tmp_path):
        from skills.core.quota.handler import _format_agent_state
        (tmp_path / ".koan-stop").write_text("")
        result = _format_agent_state(tmp_path)
        assert "stopping" in result

    def test_with_loop_status(self, tmp_path):
        from skills.core.quota.handler import _format_agent_state
        (tmp_path / ".koan-status").write_text("executing mission: fix auth")
        result = _format_agent_state(tmp_path)
        assert "executing mission" in result


# ---------------------------------------------------------------------------
# Command routing integration
# ---------------------------------------------------------------------------

class TestQuotaCommandRouting:
    """Test that /quota and /q route to the skill via command_handlers."""

    @patch("app.command_handlers.send_telegram")
    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_quota_routes_via_skill(self, mock_config, mock_send, tmp_path):
        from app.command_handlers import handle_command

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)

        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance_dir):
            handle_command("/quota")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "Agent" in output

    @patch("app.command_handlers.send_telegram")
    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_q_alias_routes(self, mock_config, mock_send, tmp_path):
        from app.command_handlers import handle_command

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)

        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance_dir):
            handle_command("/q")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "Agent" in output

    @patch("app.command_handlers.send_telegram")
    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_quota_with_args_routes_via_skill(self, mock_config, mock_send, tmp_path):
        """``/quota 32`` routes through the skill and applies override."""
        from app.command_handlers import handle_command

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir(exist_ok=True)

        with patch("app.command_handlers.KOAN_ROOT", tmp_path), \
             patch("app.command_handlers.INSTANCE_DIR", instance_dir):
            handle_command("/quota 32")
        mock_send.assert_called_once()
        output = mock_send.call_args[0][0]
        assert "32%" in output
        assert "override" in output.lower()

    @patch("app.command_handlers.send_telegram")
    def test_quota_appears_in_help(self, mock_send, tmp_path):
        from app.command_handlers import _handle_help_detail

        _handle_help_detail("status")
        mock_send.assert_called_once()
        help_text = mock_send.call_args[0][0]
        assert "/quota" in help_text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestQuotaEdgeCases:
    """Edge cases and error handling."""

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={})
    def test_corrupt_usage_state(self, mock_config, tmp_path):
        """Corrupt JSON in usage_state.json doesn't crash."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        (ctx.instance_dir / "usage_state.json").write_text("{invalid json")
        result = handle(ctx)
        assert "first run" in result.lower() or "no internal" in result.lower()

    def test_corrupt_cli_stats(self, tmp_path):
        """Corrupt stats-cache.json doesn't crash."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)
        stats_path = tmp_path / "stats-cache.json"
        stats_path.write_text("not json")

        with patch("skills.core.quota.handler.STATS_CACHE_PATH", stats_path), \
             patch("skills.core.quota.handler._load_config", return_value={}):
            result = handle(ctx)
        # Should still return usage data, just skip CLI stats
        assert "Session quota" in result

    @patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent"))
    @patch("skills.core.quota.handler._load_config", return_value={
        "usage": {"session_token_limit": 1_000_000, "weekly_token_limit": 10_000_000}
    })
    def test_custom_token_limits(self, mock_config, tmp_path):
        """Custom limits from config.yaml are respected."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir, session_tokens=500_000)
        result = handle(ctx)
        # 500k / 1M = 50%
        assert "50%" in result
        assert "1.0M" in result


# ---------------------------------------------------------------------------
# _format_cost_breakdown
# ---------------------------------------------------------------------------

class TestFormatCostBreakdown:
    """Test per-project/model cost breakdown in quota output."""

    def test_no_data_returns_none(self, tmp_path):
        from skills.core.quota.handler import _format_cost_breakdown
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        result = _format_cost_breakdown(instance_dir)
        assert result is None

    def test_shows_project_breakdown(self, tmp_path):
        from skills.core.quota.handler import _format_cost_breakdown
        from app.cost_tracker import record_usage

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        record_usage(instance_dir, "koan", "sonnet", 10000, 5000, "implement", "fix")
        record_usage(instance_dir, "other", "opus", 20000, 8000, "deep", "refactor")

        result = _format_cost_breakdown(instance_dir)
        assert result is not None
        assert "Usage (7 days)" in result
        assert "By project:" in result
        assert "koan" in result
        assert "other" in result

    def test_shows_model_breakdown(self, tmp_path):
        from skills.core.quota.handler import _format_cost_breakdown
        from app.cost_tracker import record_usage

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        record_usage(instance_dir, "p", "claude-sonnet-4-20250514", 10000, 5000)
        record_usage(instance_dir, "p", "claude-opus-4-20250514", 20000, 8000)

        result = _format_cost_breakdown(instance_dir)
        assert "By model:" in result
        assert "Opus" in result
        assert "Sonnet" in result

    def test_limits_projects_to_top_3(self, tmp_path):
        from skills.core.quota.handler import _format_cost_breakdown
        from app.cost_tracker import record_usage

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        for i in range(5):
            record_usage(instance_dir, f"proj-{i}", "sonnet", 1000 * (i + 1), 500)

        result = _format_cost_breakdown(instance_dir)
        # Should show top 3 projects by tokens
        assert "proj-4" in result  # highest
        assert "proj-3" in result
        assert "proj-2" in result
        assert "proj-0" not in result  # lowest, excluded

    def test_limits_models_to_top_2(self, tmp_path):
        from skills.core.quota.handler import _format_cost_breakdown
        from app.cost_tracker import record_usage

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        record_usage(instance_dir, "p", "claude-opus-4", 30000, 10000)
        record_usage(instance_dir, "p", "claude-sonnet-4", 20000, 5000)
        record_usage(instance_dir, "p", "claude-haiku-4", 1000, 200)

        result = _format_cost_breakdown(instance_dir)
        assert "Opus" in result
        assert "Sonnet" in result
        # Haiku may or may not appear — top 2 only

    def test_shows_run_counts(self, tmp_path):
        from skills.core.quota.handler import _format_cost_breakdown
        from app.cost_tracker import record_usage

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        record_usage(instance_dir, "koan", "sonnet", 1000, 500)
        record_usage(instance_dir, "koan", "sonnet", 2000, 800)

        result = _format_cost_breakdown(instance_dir)
        assert "2 runs" in result

    def test_integration_with_handle(self, tmp_path):
        """Cost breakdown appears in full /quota output when data exists."""
        from skills.core.quota.handler import handle
        from app.cost_tracker import record_usage

        ctx = _make_ctx(tmp_path)
        _write_usage_state(ctx.instance_dir)
        record_usage(ctx.instance_dir, "koan", "sonnet", 10000, 5000)

        with patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent")), \
             patch("skills.core.quota.handler._load_config", return_value={}):
            result = handle(ctx)
        assert "Session quota" in result
        assert "Usage (7 days)" in result
        assert "koan" in result


# ---------------------------------------------------------------------------
# Quota override (/quota <N>)
# ---------------------------------------------------------------------------

class TestQuotaOverride:
    """Test the /quota <N> override feature."""

    def test_override_sets_used_percentage(self, tmp_path):
        """``/quota 32`` sets internal usage to 32% used."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="32")
        _write_usage_state(ctx.instance_dir, session_tokens=400_000)

        result = handle(ctx)
        assert "32% used" in result
        assert "68% remaining" in result
        assert "override" in result.lower()

        # Verify usage_state.json was updated
        state = json.loads((ctx.instance_dir / "usage_state.json").read_text())
        # With default 500k limit, 32% used = 160k tokens
        assert state["session_tokens"] == 160_000

    def test_override_updates_usage_md(self, tmp_path):
        """Override rewrites usage.md to reflect the new percentage."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="50")
        _write_usage_state(ctx.instance_dir)

        handle(ctx)

        usage_md = ctx.instance_dir / "usage.md"
        assert usage_md.exists()
        content = usage_md.read_text()
        assert "50%" in content

    def test_override_clears_quota_pause(self, tmp_path):
        """Override clears .koan-pause when paused for quota."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="60")
        _write_usage_state(ctx.instance_dir)
        (tmp_path / ".koan-pause").write_text("quota\n1234567890\nresets 10am\n")

        result = handle(ctx)
        assert "pause cleared" in result.lower()
        assert not (tmp_path / ".koan-pause").exists()

    def test_override_does_not_clear_manual_pause(self, tmp_path):
        """Override doesn't clear .koan-pause when paused manually."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="60")
        _write_usage_state(ctx.instance_dir)
        (tmp_path / ".koan-pause").write_text("manual\n1234567890\n\n")

        result = handle(ctx)
        assert "pause cleared" not in result.lower()
        assert (tmp_path / ".koan-pause").exists()

    def test_override_invalid_input(self, tmp_path):
        """Non-numeric argument returns usage help."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="abc")
        result = handle(ctx)
        assert "usage:" in result.lower() or "example" in result.lower()

    def test_override_out_of_range(self, tmp_path):
        """Values outside 0-100 are rejected."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="150")
        result = handle(ctx)
        assert "between 0 and 100" in result

    def test_override_zero_used(self, tmp_path):
        """``/quota 0`` sets 0% used (100% remaining)."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="0")
        _write_usage_state(ctx.instance_dir)
        result = handle(ctx)
        assert "0% used" in result
        assert "100% remaining" in result

    def test_override_hundred_used(self, tmp_path):
        """``/quota 100`` sets 100% used (0% remaining)."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="100")
        _write_usage_state(ctx.instance_dir)
        result = handle(ctx)
        assert "100% used" in result
        assert "0% remaining" in result

    def test_override_resets_session_start(self, tmp_path):
        """Override resets the session start time so the 5h window restarts."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="80")
        old_start = (datetime.now() - timedelta(hours=4)).isoformat()
        _write_usage_state(ctx.instance_dir, session_start=old_start)

        handle(ctx)
        state = json.loads((ctx.instance_dir / "usage_state.json").read_text())
        new_start = datetime.fromisoformat(state["session_start"])
        # Session start should be very recent (within last 5 seconds)
        assert (datetime.now() - new_start).total_seconds() < 5

    def test_display_mode_still_works_with_no_args(self, tmp_path):
        """No args still shows the display output (no regression)."""
        from skills.core.quota.handler import handle

        ctx = _make_ctx(tmp_path, args="")
        _write_usage_state(ctx.instance_dir)

        with patch("skills.core.quota.handler.STATS_CACHE_PATH", Path("/nonexistent")), \
             patch("skills.core.quota.handler._load_config", return_value={}):
            result = handle(ctx)
        assert "Session quota" in result
        assert "Agent" in result
