"""Tests for usage_estimator.py — Token accumulation and usage % estimation."""

import json
import time
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.usage_estimator import (
    _extract_tokens,
    extract_tokens_detailed,
    _fresh_state,
    _load_state,
    _maybe_reset,
    _estimate_reset_time,
    _write_usage_md,
    _get_limits,
    cmd_update,
    cmd_refresh,
    cmd_reset_session,
    cmd_reset_time,
    SESSION_DURATION_HOURS,
)


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "usage_state.json"


@pytest.fixture
def usage_md(tmp_path):
    return tmp_path / "usage.md"


@pytest.fixture
def claude_json(tmp_path):
    """Claude --output-format json output with token counts."""
    f = tmp_path / "claude_out.json"
    f.write_text(json.dumps({
        "result": "Hello, I completed the task.",
        "input_tokens": 1500,
        "output_tokens": 500,
    }))
    return f


@pytest.fixture
def claude_json_nested(tmp_path):
    """Claude JSON with nested usage object."""
    f = tmp_path / "claude_nested.json"
    f.write_text(json.dumps({
        "result": "Done.",
        "usage": {"input_tokens": 3000, "output_tokens": 1000},
    }))
    return f


class TestExtractTokens:
    def test_top_level_fields(self, claude_json):
        assert _extract_tokens(claude_json) == 2000

    def test_nested_usage(self, claude_json_nested):
        assert _extract_tokens(claude_json_nested) == 4000

    def test_no_tokens(self, tmp_path):
        f = tmp_path / "no_tokens.json"
        f.write_text(json.dumps({"result": "hello"}))
        assert _extract_tokens(f) is None

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json at all")
        assert _extract_tokens(f) is None

    def test_missing_file(self, tmp_path):
        assert _extract_tokens(tmp_path / "nonexistent.json") is None


class TestExtractTokensDetailed:
    def test_top_level_fields(self, claude_json):
        result = extract_tokens_detailed(claude_json)
        assert result["input_tokens"] == 1500
        assert result["output_tokens"] == 500
        assert result["model"] == "unknown"  # No model field in fixture

    def test_nested_usage(self, claude_json_nested):
        result = extract_tokens_detailed(claude_json_nested)
        assert result["input_tokens"] == 3000
        assert result["output_tokens"] == 1000

    def test_with_model_field(self, tmp_path):
        f = tmp_path / "with_model.json"
        f.write_text(json.dumps({
            "input_tokens": 100, "output_tokens": 50,
            "model": "claude-sonnet-4-20250514",
        }))
        result = extract_tokens_detailed(f)
        assert result["model"] == "claude-sonnet-4-20250514"
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_no_tokens_returns_none(self, tmp_path):
        f = tmp_path / "no_tokens.json"
        f.write_text(json.dumps({"result": "hello"}))
        assert extract_tokens_detailed(f) is None

    def test_invalid_json_returns_none(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        assert extract_tokens_detailed(f) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert extract_tokens_detailed(tmp_path / "nope.json") is None

    def test_stats_nested_with_model(self, tmp_path):
        f = tmp_path / "stats_model.json"
        f.write_text(json.dumps({
            "result": "done",
            "model": "claude-opus-4-20250514",
            "stats": {"input_tokens": 2000, "output_tokens": 500},
        }))
        result = extract_tokens_detailed(f)
        assert result["model"] == "claude-opus-4-20250514"
        assert result["input_tokens"] == 2000
        assert result["output_tokens"] == 500

    def test_backward_compat_with_extract_tokens(self, claude_json):
        """_extract_tokens still returns int total."""
        total = _extract_tokens(claude_json)
        detailed = extract_tokens_detailed(claude_json)
        assert total == detailed["input_tokens"] + detailed["output_tokens"]

    def test_cache_fields_from_usage_object(self, tmp_path):
        """Extract cache tokens from nested usage (Claude CLI format)."""
        f = tmp_path / "cached.json"
        f.write_text(json.dumps({
            "result": "done",
            "model": "claude-opus-4-6",
            "total_cost_usd": 0.927,
            "usage": {
                "input_tokens": 24,
                "cache_creation_input_tokens": 41777,
                "cache_read_input_tokens": 1036218,
                "output_tokens": 5916,
            },
        }))
        result = extract_tokens_detailed(f)
        assert result["input_tokens"] == 24
        assert result["output_tokens"] == 5916
        assert result["cache_creation_input_tokens"] == 41777
        assert result["cache_read_input_tokens"] == 1036218
        assert result["cost_usd"] == 0.927

    def test_cache_fields_from_model_usage(self, tmp_path):
        """Extract cache tokens from modelUsage (camelCase format)."""
        f = tmp_path / "model_usage.json"
        f.write_text(json.dumps({
            "input_tokens": 100,
            "output_tokens": 50,
            "model": "claude-sonnet-4-6",
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cacheReadInputTokens": 5000,
                    "cacheCreationInputTokens": 2000,
                },
            },
        }))
        result = extract_tokens_detailed(f)
        assert result["cache_read_input_tokens"] == 5000
        assert result["cache_creation_input_tokens"] == 2000

    def test_no_cache_fields_defaults_to_zero(self, claude_json):
        """Files without cache data should return 0 for cache fields."""
        result = extract_tokens_detailed(claude_json)
        assert result["cache_creation_input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0
        assert result["cost_usd"] == 0.0

    def test_cost_usd_missing_defaults_zero(self, tmp_path):
        """Missing total_cost_usd should default to 0.0."""
        f = tmp_path / "no_cost.json"
        f.write_text(json.dumps({
            "input_tokens": 100,
            "output_tokens": 50,
        }))
        result = extract_tokens_detailed(f)
        assert result["cost_usd"] == 0.0

    def test_cost_usd_non_numeric_defaults_zero(self, tmp_path):
        """Non-numeric total_cost_usd should default to 0.0."""
        f = tmp_path / "bad_cost.json"
        f.write_text(json.dumps({
            "input_tokens": 100,
            "output_tokens": 50,
            "total_cost_usd": "not a number",
        }))
        result = extract_tokens_detailed(f)
        assert result["cost_usd"] == 0.0


class TestMaybeReset:
    def test_no_reset_within_session(self):
        state = _fresh_state()
        result = _maybe_reset(state)
        assert result["session_tokens"] == 0

    def test_session_resets_after_5h(self):
        state = _fresh_state()
        state["session_tokens"] = 100000
        state["runs"] = 5
        # Set session start to 6 hours ago
        state["session_start"] = (datetime.now() - timedelta(hours=6)).isoformat()
        result = _maybe_reset(state)
        assert result["session_tokens"] == 0
        assert result["runs"] == 0

    def test_weekly_resets_after_7_days(self):
        state = _fresh_state()
        state["weekly_tokens"] = 500000
        state["weekly_start"] = (datetime.now() - timedelta(days=8)).isoformat()
        result = _maybe_reset(state)
        assert result["weekly_tokens"] == 0


class TestWriteUsageMd:
    def test_writes_parseable_format(self, tmp_path, usage_md):
        state = {
            "session_start": datetime.now().isoformat(),
            "session_tokens": 125000,
            "weekly_start": datetime.now().isoformat(),
            "weekly_tokens": 1250000,
            "runs": 5,
        }
        config = {"usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}}
        _write_usage_md(state, usage_md, config)

        content = usage_md.read_text()
        assert "Session (5hr) : 25%" in content
        assert "Weekly (7 day) : 25%" in content
        assert "reset in" in content

    def test_caps_at_100_percent(self, tmp_path, usage_md):
        state = {
            "session_start": datetime.now().isoformat(),
            "session_tokens": 999999,
            "weekly_start": datetime.now().isoformat(),
            "weekly_tokens": 999999,
            "runs": 10,
        }
        config = {"usage": {"session_token_limit": 100, "weekly_token_limit": 100}}
        _write_usage_md(state, usage_md, config)

        content = usage_md.read_text()
        assert "100%" in content

    def test_includes_cache_line_when_available(self, tmp_path, usage_md):
        state = {
            "session_start": datetime.now().isoformat(),
            "session_tokens": 100000,
            "weekly_start": datetime.now().isoformat(),
            "weekly_tokens": 100000,
            "runs": 3,
        }
        config = {"usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}}
        with patch(
            "app.usage_estimator._get_today_cache_line",
            return_value="Cache: 45% hit rate (12.3k read / 8.1k created)",
        ):
            _write_usage_md(state, usage_md, config)
        content = usage_md.read_text()
        assert "Cache: 45% hit rate" in content

    def test_no_cache_line_when_empty(self, tmp_path, usage_md):
        state = {
            "session_start": datetime.now().isoformat(),
            "session_tokens": 100000,
            "weekly_start": datetime.now().isoformat(),
            "weekly_tokens": 100000,
            "runs": 3,
        }
        config = {"usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}}
        with patch("app.usage_estimator._get_today_cache_line", return_value=""):
            _write_usage_md(state, usage_md, config)
        content = usage_md.read_text()
        assert "Cache" not in content


class TestCmdUpdate:
    @patch("app.usage_estimator.load_config", return_value={
        "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}
    })
    def test_accumulates_tokens(self, mock_config, claude_json, state_file, usage_md):
        cmd_update(claude_json, state_file, usage_md)

        state = json.loads(state_file.read_text())
        assert state["session_tokens"] == 2000
        assert state["weekly_tokens"] == 2000
        assert state["runs"] == 1

        # Second run accumulates
        cmd_update(claude_json, state_file, usage_md)
        state = json.loads(state_file.read_text())
        assert state["session_tokens"] == 4000
        assert state["runs"] == 2

    @patch("app.usage_estimator.load_config", return_value={})
    def test_handles_no_tokens_gracefully(self, mock_config, tmp_path, state_file, usage_md):
        f = tmp_path / "empty.json"
        f.write_text(json.dumps({"result": "done"}))
        cmd_update(f, state_file, usage_md)

        state = json.loads(state_file.read_text())
        assert state["session_tokens"] == 0


class TestCmdRefresh:
    @patch("app.usage_estimator.load_config", return_value={
        "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}
    })
    def test_creates_usage_md(self, mock_config, state_file, usage_md):
        # Write some state
        state = _fresh_state()
        state["session_tokens"] = 50000
        state["weekly_tokens"] = 250000
        state_file.write_text(json.dumps(state))

        cmd_refresh(state_file, usage_md)

        content = usage_md.read_text()
        assert "Session (5hr) : 10%" in content
        assert "Weekly (7 day) : 5%" in content

    @patch("app.usage_estimator.load_config", return_value={})
    def test_fresh_state_if_no_file(self, mock_config, state_file, usage_md):
        cmd_refresh(state_file, usage_md)
        assert usage_md.exists()
        content = usage_md.read_text()
        assert "0%" in content


class TestGetLimits:
    def test_defaults(self):
        session, weekly = _get_limits({})
        assert session == 500000
        assert weekly == 5000000

    def test_custom(self):
        config = {"usage": {"session_token_limit": 100000, "weekly_token_limit": 1000000}}
        session, weekly = _get_limits(config)
        assert session == 100000
        assert weekly == 1000000


class TestEstimateResetTime:
    def test_returns_time_remaining(self):
        # Start 1 hour ago, 5h duration → ~4h remaining
        start = (datetime.now() - timedelta(hours=1)).isoformat()
        result = _estimate_reset_time(start, 5.0)
        assert "h" in result or "m" in result
        assert result != "unknown"
        assert result != "0m"

    def test_returns_0m_when_past(self):
        start = (datetime.now() - timedelta(hours=10)).isoformat()
        result = _estimate_reset_time(start, 5.0)
        assert result == "0m"

    def test_returns_unknown_on_invalid_iso(self):
        result = _estimate_reset_time("not-a-date", 5.0)
        assert result == "unknown"

    def test_hours_and_minutes_format(self):
        # Start 30 minutes ago, 5h duration → should be like "4h30m"
        start = (datetime.now() - timedelta(minutes=30)).isoformat()
        result = _estimate_reset_time(start, 5.0)
        assert "h" in result

    def test_minutes_only_format(self):
        # Start 4h50m ago, 5h duration → ~10m remaining
        start = (datetime.now() - timedelta(hours=4, minutes=50)).isoformat()
        result = _estimate_reset_time(start, 5.0)
        assert "m" in result


class TestExtractTokensStatsMeta:
    def test_stats_nested_tokens(self, tmp_path):
        f = tmp_path / "stats.json"
        f.write_text(json.dumps({
            "result": "done",
            "stats": {"input_tokens": 2000, "output_tokens": 500},
        }))
        assert _extract_tokens(f) == 2500

    def test_metadata_nested_tokens(self, tmp_path):
        f = tmp_path / "meta.json"
        f.write_text(json.dumps({
            "result": "done",
            "metadata": {"input_tokens": 1000, "output_tokens": 300},
        }))
        assert _extract_tokens(f) == 1300

    def test_session_nested_tokens(self, tmp_path):
        f = tmp_path / "session.json"
        f.write_text(json.dumps({
            "result": "done",
            "session": {"input_tokens": 500, "output_tokens": 100},
        }))
        assert _extract_tokens(f) == 600


class TestMaybeResetEdgeCases:
    def test_missing_session_start_key(self):
        state = {"weekly_start": datetime.now().isoformat(), "weekly_tokens": 0}
        # Missing session_start should not crash
        result = _maybe_reset(state)
        assert "session_start" in result

    def test_invalid_session_start_value(self):
        state = _fresh_state()
        state["session_start"] = "garbage"
        result = _maybe_reset(state)
        assert "session_start" in result

    def test_weekly_reset_on_monday_crossing(self):
        state = _fresh_state()
        state["weekly_tokens"] = 100000
        # Set weekly start to 3 days ago — if we crossed a Monday, should reset
        three_days_ago = datetime.now() - timedelta(days=3)
        state["weekly_start"] = three_days_ago.isoformat()
        result = _maybe_reset(state)
        # Result depends on day of week — just verify no crash
        assert "weekly_tokens" in result


class TestLoadState:
    def test_fresh_state_for_missing_file(self, tmp_path):
        state = _load_state(tmp_path / "nonexistent.json")
        assert state["session_tokens"] == 0
        assert state["weekly_tokens"] == 0

    def test_fresh_state_for_corrupted_file(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        state = _load_state(f)
        assert state["session_tokens"] == 0


class TestUsageEstimatorCLI:
    """CLI tests call main() directly to avoid runpy re-import issues."""

    @patch("app.usage_estimator.load_config", return_value={})
    def test_main_update(self, mock_config, tmp_path):
        import sys
        from app.usage_estimator import main
        claude_json = tmp_path / "out.json"
        claude_json.write_text(json.dumps({"input_tokens": 100, "output_tokens": 50}))
        state_file = tmp_path / "state.json"
        usage_md = tmp_path / "usage.md"

        with patch.object(sys, "argv", [
            "usage_estimator.py", "update",
            str(claude_json), str(state_file), str(usage_md),
        ]):
            main()
        assert usage_md.exists()

    @patch("app.usage_estimator.load_config", return_value={})
    def test_main_refresh(self, mock_config, tmp_path):
        import sys
        from app.usage_estimator import main
        state_file = tmp_path / "state.json"
        usage_md = tmp_path / "usage.md"

        with patch.object(sys, "argv", [
            "usage_estimator.py", "refresh",
            str(state_file), str(usage_md),
        ]):
            main()
        assert usage_md.exists()

    def test_main_missing_args(self):
        import sys
        from app.usage_estimator import main
        with patch.object(sys, "argv", ["usage_estimator.py"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_unknown_command(self):
        import sys
        from app.usage_estimator import main
        with patch.object(sys, "argv", ["usage_estimator.py", "destroy"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_update_missing_args(self):
        import sys
        from app.usage_estimator import main
        with patch.object(sys, "argv", ["usage_estimator.py", "update"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_refresh_missing_args(self):
        import sys
        from app.usage_estimator import main
        with patch.object(sys, "argv", ["usage_estimator.py", "refresh"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_reset_time(self, tmp_path):
        import sys
        from app.usage_estimator import main
        state_file = tmp_path / "state.json"
        state = _fresh_state()
        state_file.write_text(json.dumps(state))

        with patch.object(sys, "argv", [
            "usage_estimator.py", "reset-time", str(state_file),
        ]):
            main()  # Should print a timestamp and not crash

    def test_main_reset_time_missing_args(self):
        import sys
        from app.usage_estimator import main
        with patch.object(sys, "argv", ["usage_estimator.py", "reset-time"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1


class TestCmdResetTime:
    """Tests for cmd_reset_time — compute session reset timestamp."""

    def test_returns_future_timestamp(self, tmp_path):
        """Reset time should be in the future for a recently started session."""
        state_file = tmp_path / "state.json"
        state = _fresh_state()  # session_start = now
        state_file.write_text(json.dumps(state))

        ts = cmd_reset_time(state_file)
        now_ts = int(time.time())
        # Should be roughly 5 hours from now (with some tolerance)
        assert ts > now_ts
        assert ts <= now_ts + SESSION_DURATION_HOURS * 3600 + 60

    def test_returns_future_for_stale_session(self, tmp_path):
        """If session started >5h ago, should return now + 5h (not a past time)."""
        state_file = tmp_path / "state.json"
        state = _fresh_state()
        state["session_start"] = (datetime.now() - timedelta(hours=10)).isoformat()
        state_file.write_text(json.dumps(state))

        ts = cmd_reset_time(state_file)
        now_ts = int(time.time())
        assert ts > now_ts

    def test_returns_future_for_missing_file(self, tmp_path):
        """Missing state file should fallback to now + 5h."""
        state_file = tmp_path / "nonexistent.json"
        ts = cmd_reset_time(state_file)
        now_ts = int(time.time())
        assert ts > now_ts
        assert ts <= now_ts + SESSION_DURATION_HOURS * 3600 + 60

    def test_returns_future_for_corrupted_state(self, tmp_path):
        """Corrupted state file should fallback to now + 5h."""
        state_file = tmp_path / "bad.json"
        state_file.write_text("not json")
        ts = cmd_reset_time(state_file)
        now_ts = int(time.time())
        assert ts > now_ts

    def test_returns_future_for_invalid_session_start(self, tmp_path):
        """Invalid session_start should fallback to now + 5h."""
        state_file = tmp_path / "state.json"
        state = _fresh_state()
        state["session_start"] = "garbage"
        state_file.write_text(json.dumps(state))

        ts = cmd_reset_time(state_file)
        now_ts = int(time.time())
        assert ts > now_ts

    def test_mid_session_returns_correct_remainder(self, tmp_path):
        """Session started 2h ago -> reset should be ~3h from now."""
        state_file = tmp_path / "state.json"
        state = _fresh_state()
        state["session_start"] = (datetime.now() - timedelta(hours=2)).isoformat()
        state_file.write_text(json.dumps(state))

        ts = cmd_reset_time(state_file)
        now_ts = int(time.time())
        expected_ts = now_ts + 3 * 3600  # ~3h from now
        # Allow 2 minutes tolerance
        assert abs(ts - expected_ts) < 120

    def test_prevents_immediate_auto_resume(self, tmp_path):
        """Core regression test: reset time must NEVER be <= now.

        This is the exact bug that caused the infinite loop.
        """
        state_file = tmp_path / "state.json"
        for state_data in [
            _fresh_state(),
            {"session_start": "garbage", "session_tokens": 0,
             "weekly_start": datetime.now().isoformat(), "weekly_tokens": 0, "runs": 0},
            {"session_start": (datetime.now() - timedelta(hours=20)).isoformat(),
             "session_tokens": 500000,
             "weekly_start": datetime.now().isoformat(), "weekly_tokens": 0, "runs": 50},
        ]:
            state_file.write_text(json.dumps(state_data))
            ts = cmd_reset_time(state_file)
            now_ts = int(time.time())
            assert ts > now_ts, (
                f"Reset time {ts} must be strictly in the future "
                f"(now={now_ts}), state={state_data}"
            )


class TestCmdResetSession:
    """Tests for cmd_reset_session — force-reset session counters on quota resume."""

    @patch("app.usage_estimator.load_config", return_value={
        "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}
    })
    def test_resets_session_tokens_to_zero(self, mock_config, state_file, usage_md):
        """Session tokens and runs should be zeroed after reset."""
        state = _fresh_state()
        state["session_tokens"] = 450000  # 90% usage
        state["weekly_tokens"] = 2000000
        state["runs"] = 15
        state_file.write_text(json.dumps(state))

        cmd_reset_session(state_file, usage_md)

        new_state = json.loads(state_file.read_text())
        assert new_state["session_tokens"] == 0
        assert new_state["runs"] == 0

    @patch("app.usage_estimator.load_config", return_value={
        "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}
    })
    def test_preserves_weekly_tokens(self, mock_config, state_file, usage_md):
        """Weekly tokens should NOT be reset — only session is cleared."""
        state = _fresh_state()
        state["session_tokens"] = 450000
        state["weekly_tokens"] = 2000000
        state["runs"] = 15
        state_file.write_text(json.dumps(state))

        cmd_reset_session(state_file, usage_md)

        new_state = json.loads(state_file.read_text())
        assert new_state["weekly_tokens"] == 2000000

    @patch("app.usage_estimator.load_config", return_value={
        "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}
    })
    def test_updates_usage_md(self, mock_config, state_file, usage_md):
        """usage.md should reflect the reset (session back to 0%)."""
        state = _fresh_state()
        state["session_tokens"] = 450000  # was 90%
        state_file.write_text(json.dumps(state))

        cmd_reset_session(state_file, usage_md)

        content = usage_md.read_text()
        assert "Session (5hr) : 0%" in content

    @patch("app.usage_estimator.load_config", return_value={
        "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000}
    })
    def test_resets_session_start_to_now(self, mock_config, state_file, usage_md):
        """Session start should be refreshed to current time."""
        state = _fresh_state()
        old_start = (datetime.now() - timedelta(hours=4)).isoformat()
        state["session_start"] = old_start
        state["session_tokens"] = 400000
        state_file.write_text(json.dumps(state))

        cmd_reset_session(state_file, usage_md)

        new_state = json.loads(state_file.read_text())
        new_start = datetime.fromisoformat(new_state["session_start"])
        # Should be within last 5 seconds (essentially "now")
        assert (datetime.now() - new_start).total_seconds() < 5

    @patch("app.usage_estimator.load_config", return_value={})
    def test_works_with_missing_state_file(self, mock_config, state_file, usage_md):
        """Should create fresh state if no file exists."""
        assert not state_file.exists()
        cmd_reset_session(state_file, usage_md)
        assert state_file.exists()
        assert usage_md.exists()
        new_state = json.loads(state_file.read_text())
        assert new_state["session_tokens"] == 0

    @patch("app.usage_estimator.load_config", return_value={})
    def test_prevents_immediate_repause(self, mock_config, state_file, usage_md):
        """Core regression test: after reset, usage_tracker should NOT return 'wait'.

        This is the exact bug from the mission — stale high session % caused
        the run loop to re-pause immediately after /resume.
        """
        # Simulate exhausted session: 95% usage
        state = _fresh_state()
        state["session_tokens"] = 475000
        state["weekly_tokens"] = 100000
        state_file.write_text(json.dumps(state))

        # Reset session (simulating what happens on /resume)
        cmd_reset_session(state_file, usage_md)

        # Now usage.md should show 0% session, which means mode != "wait"
        content = usage_md.read_text()
        assert "Session (5hr) : 0%" in content

    @patch("app.usage_estimator.load_config", return_value={})
    def test_cli_reset_session(self, mock_config, tmp_path):
        """CLI entry point for reset-session should work."""
        import sys
        from app.usage_estimator import main

        state_file = tmp_path / "state.json"
        state = _fresh_state()
        state["session_tokens"] = 400000
        state_file.write_text(json.dumps(state))
        usage_md = tmp_path / "usage.md"

        with patch.object(sys, "argv", [
            "usage_estimator.py", "reset-session",
            str(state_file), str(usage_md),
        ]):
            main()

        new_state = json.loads(state_file.read_text())
        assert new_state["session_tokens"] == 0
        assert usage_md.exists()

    def test_cli_reset_session_missing_args(self):
        """CLI reset-session with missing args should exit 1."""
        import sys
        from app.usage_estimator import main
        with patch.object(sys, "argv", ["usage_estimator.py", "reset-session"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
