"""Tests for app.config_validator — startup config.yaml validation."""

import pytest

from app.config_validator import (
    validate_config, validate_and_warn, _check_type, _check_schedule_overlap,
    _suggest_typo,
)


# ---------------------------------------------------------------------------
# _check_type
# ---------------------------------------------------------------------------

class TestCheckType:
    def test_int(self):
        assert _check_type(42, "int") is True

    def test_int_rejects_bool(self):
        assert _check_type(True, "int") is False

    def test_bool(self):
        assert _check_type(True, "bool") is True
        assert _check_type(False, "bool") is True

    def test_str(self):
        assert _check_type("hello", "str") is True

    def test_list(self):
        assert _check_type([1, 2], "list") is True

    def test_dict(self):
        assert _check_type({"a": 1}, "dict") is True

    def test_wrong_type(self):
        assert _check_type("hello", "int") is False
        assert _check_type(42, "str") is False

    def test_tuple_of_types(self):
        assert _check_type([1], ("list", "str")) is True
        assert _check_type("hello", ("list", "str")) is True
        assert _check_type(42, ("list", "str")) is False


# ---------------------------------------------------------------------------
# _suggest_typo
# ---------------------------------------------------------------------------

class TestSuggestTypo:
    def test_close_match(self):
        assert _suggest_typo("intervl_seconds", ["interval_seconds", "max_runs_per_day"]) == "interval_seconds"

    def test_no_match(self):
        assert _suggest_typo("zzzzzzz", ["interval_seconds", "max_runs_per_day"]) == ""

    def test_prefix_typo(self):
        assert _suggest_typo("max_runs_pr_day", ["max_runs_per_day", "interval_seconds"]) == "max_runs_per_day"


# ---------------------------------------------------------------------------
# validate_config — valid configs
# ---------------------------------------------------------------------------

class TestValidConfigProducesNoWarnings:
    def test_empty_config(self):
        assert validate_config({}) == []

    def test_minimal_valid_config(self):
        config = {
            "max_runs_per_day": 20,
            "interval_seconds": 300,
        }
        assert validate_config(config) == []

    def test_full_valid_config(self):
        config = {
            "max_runs_per_day": 20,
            "interval_seconds": 300,
            "fast_reply": False,
            "debug": True,
            "cli_output_journal": True,
            "branch_prefix": "koan",
            "skill_timeout": 3600,
            "contemplative_chance": 10,
            "start_on_pause": False,
            "skip_permissions": False,
            "cli_provider": "claude",
            "telegram": {"bot_token": "tok", "chat_id": "123"},
            "budget": {"warn_at_percent": 70, "stop_at_percent": 85},
            "tools": {
                "chat": ["Read", "Glob"],
                "mission": ["Read", "Glob", "Edit", "Write", "Bash"],
                "description": "Available tools",
            },
            "models": {
                "mission": "",
                "chat": "",
                "lightweight": "haiku",
                "fallback": "sonnet",
                "review_mode": "",
            },
            "git_auto_merge": {
                "enabled": True,
                "base_branch": "main",
                "strategy": "squash",
                "rules": [],
            },
            "github": {
                "nickname": "koan-bot",
                "commands_enabled": False,
                "authorized_users": ["*"],
                "reply_enabled": False,
                "max_age_hours": 24,
                "check_interval_seconds": 60,
                "max_check_interval_seconds": 180,
            },
            "schedule": {"deep_hours": "0-6", "work_hours": "8-20"},
            "logs": {"max_backups": 3, "max_size_mb": 50, "compress": True},
            "local_llm": {"base_url": "http://localhost:11434/v1", "model": "glm4", "api_key": ""},
            "ollama_launch": {"model": "glm4"},
            "usage": {"session_token_limit": 500000, "weekly_token_limit": 5000000, "budget_mode": "full"},
            "email": {"enabled": False, "max_per_day": 5, "require_approval": True},
            "messaging": {"provider": "telegram"},
        }
        assert validate_config(config) == []

    def test_none_values_are_ok(self):
        """Keys set to null/None should not trigger warnings."""
        config = {
            "debug": None,
            "tools": None,
            "models": {"mission": None},
        }
        assert validate_config(config) == []

    def test_tools_chat_as_string(self):
        """tools.chat can be a comma-separated string."""
        config = {"tools": {"chat": "Read,Glob,Grep"}}
        assert validate_config(config) == []


# ---------------------------------------------------------------------------
# validate_config — unrecognized keys
# ---------------------------------------------------------------------------

class TestUnrecognizedKeys:
    def test_top_level_unknown(self):
        warnings = validate_config({"unknown_key": "value"})
        assert len(warnings) == 1
        assert "unrecognized key 'unknown_key'" in warnings[0][1]

    def test_top_level_typo_suggestion(self):
        warnings = validate_config({"intervl_seconds": 300})
        assert len(warnings) == 1
        assert "did you mean 'interval_seconds'" in warnings[0][1]

    def test_nested_unknown(self):
        warnings = validate_config({"budget": {"warnat_percent": 70}})
        assert len(warnings) == 1
        assert "unrecognized key 'budget.warnat_percent'" in warnings[0][1]

    def test_nested_typo_suggestion(self):
        warnings = validate_config({"github": {"comands_enabled": True}})
        assert len(warnings) == 1
        assert "did you mean 'github.commands_enabled'" in warnings[0][1]

    def test_multiple_unknowns(self):
        warnings = validate_config({"foo": 1, "bar": 2})
        assert len(warnings) == 2


# ---------------------------------------------------------------------------
# validate_config — type mismatches
# ---------------------------------------------------------------------------

class TestTypeMismatches:
    def test_int_given_string(self):
        warnings = validate_config({"max_runs_per_day": "twenty"})
        assert len(warnings) == 1
        assert "should be int" in warnings[0][1]
        assert "got str" in warnings[0][1]

    def test_bool_given_string(self):
        warnings = validate_config({"debug": "yes"})
        assert len(warnings) == 1
        assert "should be bool" in warnings[0][1]

    def test_bool_given_int(self):
        """Ensure int values like 0/1 are flagged as not bool."""
        warnings = validate_config({"fast_reply": 1})
        assert len(warnings) == 1
        assert "should be bool" in warnings[0][1]

    def test_nested_type_mismatch(self):
        warnings = validate_config({"budget": {"warn_at_percent": "seventy"}})
        assert len(warnings) == 1
        assert "budget.warn_at_percent" in warnings[0][1]
        assert "should be int" in warnings[0][1]

    def test_section_not_dict(self):
        warnings = validate_config({"telegram": "not-a-dict"})
        assert len(warnings) == 1
        assert "should be a mapping" in warnings[0][1]

    def test_int_is_not_bool(self):
        """Bool True/False should not be accepted as int."""
        warnings = validate_config({"max_runs_per_day": True})
        assert len(warnings) == 1
        assert "should be int" in warnings[0][1]

    def test_str_given_int(self):
        warnings = validate_config({"branch_prefix": 123})
        assert len(warnings) == 1
        assert "should be str" in warnings[0][1]


# ---------------------------------------------------------------------------
# validate_config — edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_non_dict_root(self):
        warnings = validate_config("not a dict")
        assert len(warnings) == 1
        assert "root is not a mapping" in warnings[0][1]

    def test_unknown_section_without_subschema(self):
        """A known nested key without section schema should still pass."""
        # All our nested keys have schemas, so this tests the pass-through
        config = {"telegram": {"bot_token": "x", "chat_id": "y"}}
        assert validate_config(config) == []

    def test_mixed_valid_and_invalid(self):
        config = {
            "max_runs_per_day": 20,
            "unknown_key": "bad",
            "debug": "not-a-bool",
            "budget": {"warn_at_percent": 70, "fake_key": 1},
        }
        warnings = validate_config(config)
        assert len(warnings) == 3
        paths = [w[0] for w in warnings]
        assert "unknown_key" in paths
        assert "debug" in paths
        assert "budget.fake_key" in paths


# ---------------------------------------------------------------------------
# validate_and_warn — integration with logging
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _check_schedule_overlap
# ---------------------------------------------------------------------------

class TestCheckScheduleOverlap:
    def test_no_overlap(self):
        assert _check_schedule_overlap("0-8", "8-20") is False

    def test_full_overlap(self):
        assert _check_schedule_overlap("0-8", "0-8") is True

    def test_partial_overlap(self):
        assert _check_schedule_overlap("0-10", "8-20") is True

    def test_empty_specs(self):
        assert _check_schedule_overlap("", "") is False

    def test_invalid_spec_returns_false(self):
        assert _check_schedule_overlap("invalid", "0-8") is False

    def test_wrap_around_overlap(self):
        assert _check_schedule_overlap("22-6", "0-8") is True

    def test_wrap_around_no_overlap(self):
        assert _check_schedule_overlap("22-6", "8-20") is False


class TestValidateConfigScheduleOverlap:
    def test_overlapping_schedule_produces_warning(self):
        config = {
            "schedule": {
                "deep_hours": "0-8",
                "work_hours": "0-8",
            }
        }
        warnings = validate_config(config)
        paths = [p for p, _ in warnings]
        assert "schedule" in paths
        msgs = [m for _, m in warnings]
        assert any("overlap" in m for m in msgs)

    def test_non_overlapping_schedule_no_warning(self):
        config = {
            "schedule": {
                "deep_hours": "0-8",
                "work_hours": "8-20",
            }
        }
        warnings = validate_config(config)
        paths = [p for p, _ in warnings]
        assert "schedule" not in paths

    def test_partial_schedule_no_overlap_check(self):
        """Only one range configured — no overlap possible."""
        config = {
            "schedule": {
                "deep_hours": "0-8",
            }
        }
        warnings = validate_config(config)
        paths = [p for p, _ in warnings]
        assert "schedule" not in paths


# ---------------------------------------------------------------------------
# validate_and_warn
# ---------------------------------------------------------------------------

class TestValidateAndWarn:
    def test_logs_warnings(self, capsys):
        config = {"unknwon": 1, "max_runs_per_day": "bad"}
        messages = validate_and_warn(config)
        assert len(messages) == 2
        out = capsys.readouterr().out
        assert "[config]" in out

    def test_no_warnings_no_output(self, capsys):
        messages = validate_and_warn({"max_runs_per_day": 20})
        assert messages == []
        out = capsys.readouterr().out
        assert "[config]" not in out
