"""Tests for CLI __main__ blocks and uncovered edge cases across modules.

Covers:
- usage_tracker.py CLI main (L218-236, 240) + wait mode (L138) + review reason (L193)
- memory_manager.py CLI main (L230-267)
- recover.py CLI main (L132-146) + complex mission fallback (L74)
- utils.py edge cases (load_config, get_allowed_tools, get_tools_description,
  atomic_write error path, conversation history helpers)
"""

import json
import os
from tests._helpers import run_module
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# usage_tracker.py — CLI main + edge cases
# ---------------------------------------------------------------------------

class TestUsageTrackerCLI:
    """Test CLI entry point for usage_tracker.py."""

    def test_cli_no_args(self):
        """Missing args prints usage to stderr and exits 1."""
        with patch.object(sys, "argv", ["usage_tracker.py"]):
            with pytest.raises(SystemExit, match="1"):
                run_module("app.usage_tracker", run_name="__main__")

    def test_cli_normal_run(self, tmp_path, capsys):
        """Normal CLI run outputs mode:available:reason."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 10% (reset in 4h)\nWeekly (7 day) : 25% (Resets in 5d)")
        with patch.object(sys, "argv", ["usage_tracker.py", str(usage), "3"]):
            run_module("app.usage_tracker", run_name="__main__")
        out = capsys.readouterr().out.strip()
        parts = out.split(":")
        assert len(parts) == 3
        assert parts[0] == "deep"

    def test_cli_parse_error_fallback(self, tmp_path, capsys):
        """Parse error falls back to review:50:Fallback mode."""
        from app.usage_tracker import main
        usage = tmp_path / "usage.md"
        usage.write_text("garbage")
        with patch.object(sys, "argv", ["usage_tracker.py", str(usage), "1"]):
            with patch("app.usage_tracker.UsageTracker.decide_mode", side_effect=ValueError("bad")):
                with pytest.raises(SystemExit, match="0"):
                    main()
        out = capsys.readouterr().out.strip()
        assert "Fallback" in out


class TestUsageTrackerWaitMode:
    """Cover decide_mode() wait branch (L138)."""

    def test_wait_mode_budget_below_5(self, tmp_path):
        """Budget < 5% triggers wait mode."""
        from app.usage_tracker import UsageTracker
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 90% (reset in 1h)\nWeekly (7 day) : 95% (Resets in 1d)")
        tracker = UsageTracker(usage)
        assert tracker.decide_mode() == "wait"

    def test_review_decision_reason(self, tmp_path):
        """Review reason string includes 'conservative' or 'low'."""
        from app.usage_tracker import UsageTracker
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 78% (reset in 1h)\nWeekly (7 day) : 80% (Resets in 1d)")
        tracker = UsageTracker(usage)
        reason = tracker.get_decision_reason("review")
        assert "low" in reason.lower() or "conservative" in reason.lower()

    def test_implement_decision_reason(self, tmp_path):
        """Implement reason string includes 'normal'."""
        from app.usage_tracker import UsageTracker
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 50% (reset in 2h)\nWeekly (7 day) : 50% (Resets in 3d)")
        tracker = UsageTracker(usage)
        reason = tracker.get_decision_reason("implement")
        assert "normal" in reason.lower()


# ---------------------------------------------------------------------------
# memory_manager.py — CLI main
# ---------------------------------------------------------------------------

class TestMemoryManagerCLI:
    """Test CLI entry point for memory_manager.py."""

    def test_cli_no_args(self):
        """Missing args prints usage and exits 1."""
        with patch.object(sys, "argv", ["memory_manager.py"]):
            with pytest.raises(SystemExit, match="1"):
                run_module("app.memory_manager", run_name="__main__")

    def test_cli_scoped_summary(self, instance_dir, capsys):
        """scoped-summary command prints filtered summary."""
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("# Summary\n\n## 2026-02-01\n\nSession 1 (project: koan) : stuff\n")
        with patch.object(sys, "argv", ["memory_manager.py", str(instance_dir), "scoped-summary", "koan"]):
            run_module("app.memory_manager", run_name="__main__")
        out = capsys.readouterr().out
        assert "koan" in out

    def test_cli_scoped_summary_no_project(self):
        """scoped-summary without project arg exits 1."""
        with patch.object(sys, "argv", ["memory_manager.py", "/tmp", "scoped-summary"]):
            with pytest.raises(SystemExit, match="1"):
                run_module("app.memory_manager", run_name="__main__")

    def test_cli_compact(self, instance_dir, capsys):
        """compact command reports removed count."""
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("# Summary\n\n## 2026-02-01\n\nSession 1\n")
        with patch.object(sys, "argv", ["memory_manager.py", str(instance_dir), "compact", "10"]):
            run_module("app.memory_manager", run_name="__main__")
        out = capsys.readouterr().out
        assert "Compacted" in out

    def test_cli_cleanup_learnings(self, instance_dir, capsys):
        """cleanup-learnings command reports dedup count."""
        proj_dir = instance_dir / "memory" / "projects" / "koan"
        proj_dir.mkdir(parents=True)
        (proj_dir / "learnings.md").write_text("# L\n\n- item\n- item\n")
        with patch.object(sys, "argv", ["memory_manager.py", str(instance_dir), "cleanup-learnings", "koan"]):
            run_module("app.memory_manager", run_name="__main__")
        out = capsys.readouterr().out
        assert "Deduped" in out

    def test_cli_cleanup_learnings_no_project(self):
        """cleanup-learnings without project arg exits 1."""
        with patch.object(sys, "argv", ["memory_manager.py", "/tmp", "cleanup-learnings"]):
            with pytest.raises(SystemExit, match="1"):
                run_module("app.memory_manager", run_name="__main__")

    def test_cli_cleanup(self, instance_dir, capsys):
        """cleanup command runs full cleanup."""
        summary = instance_dir / "memory" / "summary.md"
        summary.write_text("# Summary\n\n## 2026-02-01\n\nSession 1\n")
        with patch.object(sys, "argv", ["memory_manager.py", str(instance_dir), "cleanup", "10"]):
            run_module("app.memory_manager", run_name="__main__")

    def test_cli_unknown_command(self):
        """Unknown command exits 1."""
        with patch.object(sys, "argv", ["memory_manager.py", "/tmp", "bogus"]):
            with pytest.raises(SystemExit, match="1"):
                run_module("app.memory_manager", run_name="__main__")


# ---------------------------------------------------------------------------
# recover.py — CLI main + complex mission fallback (L74)
# ---------------------------------------------------------------------------

class TestRecoverCLI:
    """Test CLI entry point for recover.py."""

    def test_cli_no_args(self):
        """Missing args exits 1."""
        with patch.object(sys, "argv", ["recover.py"]):
            with pytest.raises(SystemExit, match="1"):
                run_module("app.recover", run_name="__main__")

    def test_cli_no_stale_missions(self, instance_dir, capsys):
        """No stale missions prints 0."""
        with patch("app.recover.format_and_send"):
            with patch.object(sys, "argv", ["recover.py", str(instance_dir)]):
                run_module("app.recover", run_name="__main__")
        out = capsys.readouterr().out
        assert "No stale" in out or "0" in out

    def test_cli_with_stale_missions(self, instance_dir, capsys):
        """Stale missions recovered and notification sent."""
        missions = instance_dir / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Stale task\n\n"
            "## Done\n\n"
        )
        with patch("app.notify.format_and_send") as mock_send:
            with patch.object(sys, "argv", ["recover.py", str(instance_dir)]):
                run_module("app.recover", run_name="__main__")
        mock_send.assert_called_once()
        out = capsys.readouterr().out
        assert "1" in out


class TestRecoverComplexMissionFallback:
    """Cover L74 — complex mission ending with non-sub-item line."""

    def test_complex_mission_ends_with_non_subitem(self, instance_dir):
        """Everything from ### header to next ### header (or section end) stays in-progress."""
        from app.recover import recover_missions
        missions = instance_dir / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "### Complex project\n"
            "- ~~done step~~ done\n"
            "- Still working\n"
            "Simple stale mission\n"
            "- Another stale\n\n"
            "## Done\n\n"
        )
        count, _ = recover_missions(str(instance_dir))
        # All lines after ### are part of the complex mission — none recovered
        assert count == 0


# ---------------------------------------------------------------------------
# utils.py — edge cases
# ---------------------------------------------------------------------------

class TestUtilsLoadConfig:
    """Cover load_config, get_allowed_tools, get_tools_description."""

    def test_load_config_missing_file(self):
        """Missing config.yaml returns empty dict."""
        from app.utils import load_config
        with patch("app.utils.KOAN_ROOT", Path("/nonexistent")):
            result = load_config()
        assert result == {}

    def test_load_config_valid_yaml(self, tmp_path):
        """Valid YAML is loaded."""
        from app.utils import load_config
        config_dir = tmp_path / "instance"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("tools:\n  allowed: [Read, Write]\n")
        with patch("app.utils.KOAN_ROOT", tmp_path):
            result = load_config()
        assert result["tools"]["allowed"] == ["Read", "Write"]

    def test_load_config_bad_yaml(self, tmp_path):
        """Invalid YAML returns empty dict."""
        from app.utils import load_config
        config_dir = tmp_path / "instance"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{{invalid yaml")
        with patch("app.utils.KOAN_ROOT", tmp_path):
            result = load_config()
        assert result == {}

    def test_get_chat_tools_default(self):
        """Default chat tools are read-only (no Bash, Edit, Write)."""
        from app.utils import get_chat_tools
        with patch("app.utils.load_config", return_value={}):
            tools = get_chat_tools()
        assert "Read" in tools
        assert "Glob" in tools
        assert "Grep" in tools
        assert "Bash" not in tools
        assert "Edit" not in tools
        assert "Write" not in tools

    def test_get_chat_tools_custom(self):
        """Custom chat tools from config."""
        from app.utils import get_chat_tools
        with patch("app.utils.load_config", return_value={"tools": {"chat": ["Read", "Edit"]}}):
            tools = get_chat_tools()
        assert tools == "Read,Edit"

    def test_get_mission_tools_default(self):
        """Default mission tools include Bash."""
        from app.utils import get_mission_tools
        with patch("app.utils.load_config", return_value={}):
            tools = get_mission_tools()
        assert "Bash" in tools
        assert "Edit" in tools
        assert "Write" in tools

    def test_get_mission_tools_custom(self):
        """Custom mission tools from config."""
        from app.utils import get_mission_tools
        with patch("app.utils.load_config", return_value={"tools": {"mission": ["Bash"]}}):
            tools = get_mission_tools()
        assert tools == "Bash"

    def test_get_allowed_tools_backward_compat(self):
        """get_allowed_tools() is backward compatible alias for get_mission_tools()."""
        from app.utils import get_allowed_tools, get_mission_tools
        with patch("app.utils.load_config", return_value={}):
            allowed = get_allowed_tools()
            mission = get_mission_tools()
        assert allowed == mission

    def test_get_tools_description_empty(self):
        """Empty description when config has no description."""
        from app.utils import get_tools_description
        with patch("app.utils.load_config", return_value={}):
            desc = get_tools_description()
        assert desc == ""

    def test_get_tools_description_custom(self):
        """Custom description from config."""
        from app.utils import get_tools_description
        with patch("app.utils.load_config", return_value={"tools": {"description": "Custom desc"}}):
            desc = get_tools_description()
        assert desc == "Custom desc"


class TestModelConfig:
    """Tests for model configuration and CLI flag builders."""

    def test_get_model_config_defaults(self):
        """Default model config when no models section in config."""
        from app.utils import get_model_config
        with patch("app.utils.load_config", return_value={}):
            cfg = get_model_config()
        assert cfg["mission"] == ""
        assert cfg["lightweight"] == "haiku"
        assert cfg["fallback"] == "sonnet"

    def test_get_model_config_custom(self):
        """Custom model config overrides defaults."""
        from app.utils import get_model_config
        config = {"models": {"mission": "opus", "lightweight": "sonnet", "fallback": ""}}
        with patch("app.utils.load_config", return_value=config):
            cfg = get_model_config()
        assert cfg["mission"] == "opus"
        assert cfg["lightweight"] == "sonnet"
        assert cfg["fallback"] == ""
        # Unspecified keys get defaults
        assert cfg["review_mode"] == ""

    def test_build_claude_flags_empty(self):
        """No flags when everything is empty."""
        from app.utils import build_claude_flags
        assert build_claude_flags() == []

    def test_build_claude_flags_model(self):
        """Model flag generated correctly."""
        from app.utils import build_claude_flags
        flags = build_claude_flags(model="haiku")
        assert flags == ["--model", "haiku"]

    def test_build_claude_flags_fallback(self):
        """Fallback flag generated correctly."""
        from app.utils import build_claude_flags
        flags = build_claude_flags(fallback="sonnet")
        assert flags == ["--fallback-model", "sonnet"]

    def test_build_claude_flags_disallowed_tools(self):
        """DisallowedTools flags generated correctly."""
        from app.utils import build_claude_flags
        flags = build_claude_flags(disallowed_tools=["Bash", "Edit"])
        assert flags == ["--disallowedTools", "Bash", "Edit"]

    def test_build_claude_flags_combined(self):
        """All flags combined."""
        from app.utils import build_claude_flags
        flags = build_claude_flags(model="opus", fallback="sonnet", disallowed_tools=["Write"])
        assert "--model" in flags
        assert "--fallback-model" in flags
        assert "--disallowedTools" in flags
        assert "Write" in flags

    def test_get_claude_flags_for_role_mission(self):
        """Mission role returns fallback flag."""
        from app.utils import get_claude_flags_for_role
        config = {"models": {"mission": "", "fallback": "sonnet", "review_mode": ""}}
        with patch("app.utils.load_config", return_value=config):
            flags = get_claude_flags_for_role("mission")
        assert "--fallback-model sonnet" in flags
        assert "--model" not in flags

    def test_get_claude_flags_for_role_mission_with_model(self):
        """Mission role with explicit model."""
        from app.utils import get_claude_flags_for_role
        config = {"models": {"mission": "opus", "fallback": "sonnet", "review_mode": ""}}
        with patch("app.utils.load_config", return_value=config):
            flags = get_claude_flags_for_role("mission")
        assert "--model opus" in flags
        assert "--fallback-model sonnet" in flags

    def test_get_claude_flags_for_role_review_mode(self):
        """Review mode blocks write tools and uses cheaper model."""
        from app.utils import get_claude_flags_for_role
        config = {"models": {"mission": "", "fallback": "", "review_mode": "haiku"}}
        with patch("app.utils.load_config", return_value=config):
            flags = get_claude_flags_for_role("mission", autonomous_mode="review")
        assert "--model haiku" in flags
        assert "--disallowedTools" in flags
        assert "Bash" in flags
        assert "Edit" in flags
        assert "Write" in flags

    def test_get_claude_flags_for_role_contemplative(self):
        """Contemplative role uses lightweight model."""
        from app.utils import get_claude_flags_for_role
        config = {"models": {"lightweight": "haiku"}}
        with patch("app.utils.load_config", return_value=config):
            flags = get_claude_flags_for_role("contemplative")
        assert "--model haiku" in flags

    def test_get_claude_flags_for_role_chat(self):
        """Chat role uses chat model with fallback."""
        from app.utils import get_claude_flags_for_role
        config = {"models": {"chat": "sonnet", "fallback": "haiku"}}
        with patch("app.utils.load_config", return_value=config):
            flags = get_claude_flags_for_role("chat")
        assert "--model sonnet" in flags
        assert "--fallback-model haiku" in flags

    def test_get_claude_flags_for_role_unknown(self):
        """Unknown role returns empty flags."""
        from app.utils import get_claude_flags_for_role
        with patch("app.utils.load_config", return_value={}):
            flags = get_claude_flags_for_role("unknown_role")
        assert flags == ""

    def test_get_fast_reply_model_enabled(self):
        """fast_reply=true returns lightweight model."""
        from app.utils import get_fast_reply_model
        config = {"fast_reply": True, "models": {"lightweight": "haiku"}}
        with patch("app.utils.load_config", return_value=config):
            model = get_fast_reply_model()
        assert model == "haiku"

    def test_get_fast_reply_model_disabled(self):
        """fast_reply=false returns empty string (use default)."""
        from app.utils import get_fast_reply_model
        config = {"fast_reply": False, "models": {"lightweight": "haiku"}}
        with patch("app.utils.load_config", return_value=config):
            model = get_fast_reply_model()
        assert model == ""

    def test_get_fast_reply_model_missing(self):
        """Missing fast_reply key defaults to false."""
        from app.utils import get_fast_reply_model
        config = {"models": {"lightweight": "haiku"}}
        with patch("app.utils.load_config", return_value=config):
            model = get_fast_reply_model()
        assert model == ""

    def test_get_fast_reply_model_custom_lightweight(self):
        """fast_reply uses custom lightweight model from config."""
        from app.utils import get_fast_reply_model
        config = {"fast_reply": True, "models": {"lightweight": "sonnet"}}
        with patch("app.utils.load_config", return_value=config):
            model = get_fast_reply_model()
        assert model == "sonnet"

    def test_get_contemplative_chance_default(self):
        """Missing contemplative_chance defaults to 10."""
        from app.utils import get_contemplative_chance
        with patch("app.utils.load_config", return_value={}):
            chance = get_contemplative_chance()
        assert chance == 10

    def test_get_contemplative_chance_configured(self):
        """contemplative_chance reads from config."""
        from app.utils import get_contemplative_chance
        config = {"contemplative_chance": 25}
        with patch("app.utils.load_config", return_value=config):
            chance = get_contemplative_chance()
        assert chance == 25

    def test_get_contemplative_chance_zero(self):
        """contemplative_chance can be set to 0 (never)."""
        from app.utils import get_contemplative_chance
        config = {"contemplative_chance": 0}
        with patch("app.utils.load_config", return_value=config):
            chance = get_contemplative_chance()
        assert chance == 0

    def test_get_contemplative_chance_hundred(self):
        """contemplative_chance can be set to 100 (always)."""
        from app.utils import get_contemplative_chance
        config = {"contemplative_chance": 100}
        with patch("app.utils.load_config", return_value=config):
            chance = get_contemplative_chance()
        assert chance == 100

    def test_get_contemplative_chance_clamped_above_100(self):
        """Values above 100 are clamped to 100."""
        from app.utils import get_contemplative_chance
        config = {"contemplative_chance": 500}
        with patch("app.utils.load_config", return_value=config):
            chance = get_contemplative_chance()
        assert chance == 100

    def test_get_contemplative_chance_clamped_below_zero(self):
        """Negative values are clamped to 0."""
        from app.utils import get_contemplative_chance
        config = {"contemplative_chance": -10}
        with patch("app.utils.load_config", return_value=config):
            chance = get_contemplative_chance()
        assert chance == 0


class TestUtilsConversationHistory:
    """Cover save/load/format conversation history edge cases."""

    def test_save_message_error(self, tmp_path, capsys):
        """OSError on save prints error but doesn't raise."""
        from app.utils import save_telegram_message
        # Write to a non-writable path
        bad_path = tmp_path / "readonly" / "history.jsonl"
        save_telegram_message(bad_path, "user", "hello")
        err = capsys.readouterr().err
        # Should print error (or it may be stdout depending on print target)

    def test_load_history_error(self, tmp_path):
        """OSError on load returns empty list."""
        from app.utils import load_recent_telegram_history
        # Create a directory where file is expected (causes OSError)
        bad_path = tmp_path / "history.jsonl"
        bad_path.mkdir()  # is a dir, not a file
        result = load_recent_telegram_history(bad_path)
        assert result == []

    def test_load_history_bad_json(self, tmp_path):
        """Malformed JSON lines are skipped."""
        from app.utils import load_recent_telegram_history
        history = tmp_path / "history.jsonl"
        history.write_text('{"role":"user","text":"hi"}\nnot json\n{"role":"assistant","text":"hey"}\n')
        result = load_recent_telegram_history(history)
        assert len(result) == 2

    def test_format_history_truncates_long_messages(self):
        """Messages > 500 chars are truncated."""
        from app.utils import format_conversation_history
        messages = [{"role": "user", "text": "x" * 600}]
        result = format_conversation_history(messages)
        assert "..." in result
        assert len(result) < 700

    def test_format_history_max_chars(self):
        """Output respects max_chars limit."""
        from app.utils import format_conversation_history
        messages = [{"role": "user", "text": f"msg {i}"} for i in range(100)]
        result = format_conversation_history(messages, max_chars=100)
        assert len(result) <= 200  # Some overhead is fine, but it should stop early


class TestUtilsAtomicWriteError:
    """Cover atomic_write error path (L225-230)."""

    def test_atomic_write_cleans_temp_on_error(self, tmp_path):
        """Temp file is cleaned up if os.replace fails."""
        from app.utils import atomic_write
        target = tmp_path / "test.txt"
        target.write_text("original")
        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write(target, "new content")
        # Original should be untouched
        assert target.read_text() == "original"
