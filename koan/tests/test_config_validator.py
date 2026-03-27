"""Tests for app.config_validator — startup config.yaml validation."""

import pytest

from app.config_validator import (
    validate_config, validate_and_warn, _check_type, _check_schedule_overlap,
    _suggest_typo, detect_config_drift, _collect_keys, _find_commented_keys,
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

    def test_drift_detection_with_koan_root(self, tmp_path, capsys):
        """validate_and_warn with koan_root triggers drift detection."""
        import yaml

        template = {"max_runs_per_day": 20, "auto_update": {"enabled": True}}
        user = {"max_runs_per_day": 20}

        (tmp_path / "instance.example").mkdir()
        (tmp_path / "instance.example" / "config.yaml").write_text(yaml.dump(template))

        # Write user config file (no commented keys) so comment detection works
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "config.yaml").write_text(yaml.dump(user))

        messages = validate_and_warn(user, koan_root=str(tmp_path))
        assert len(messages) == 1
        assert "Config drift" in messages[0]
        assert "auto_update" in messages[0]

    def test_no_drift_without_koan_root(self, capsys):
        """Without koan_root, no drift detection is performed."""
        messages = validate_and_warn({"max_runs_per_day": 20})
        assert messages == []


# ---------------------------------------------------------------------------
# _collect_keys
# ---------------------------------------------------------------------------

class TestCollectKeys:
    def test_flat_dict(self):
        assert _collect_keys({"a": 1, "b": 2}) == {"a", "b"}

    def test_nested_dict(self):
        keys = _collect_keys({"a": {"b": 1, "c": 2}})
        assert keys == {"a", "a.b", "a.c"}

    def test_deeply_nested(self):
        keys = _collect_keys({"a": {"b": {"c": 1}}})
        assert keys == {"a", "a.b", "a.b.c"}

    def test_empty_dict(self):
        assert _collect_keys({}) == set()


# ---------------------------------------------------------------------------
# detect_config_drift
# ---------------------------------------------------------------------------

class TestDetectConfigDrift:
    def _setup_configs(self, tmp_path, template_config, user_config=None,
                        user_config_text=None):
        """Helper to create template and optional user config files.

        Args:
            user_config: Dict to dump as YAML for instance/config.yaml.
            user_config_text: Raw text to write as instance/config.yaml
                (for testing commented-out keys). Takes precedence over user_config.
        """
        import yaml

        (tmp_path / "instance.example").mkdir(exist_ok=True)
        (tmp_path / "instance.example" / "config.yaml").write_text(
            yaml.dump(template_config)
        )

        if user_config_text is not None:
            (tmp_path / "instance").mkdir(exist_ok=True)
            (tmp_path / "instance" / "config.yaml").write_text(user_config_text)
        elif user_config is not None:
            (tmp_path / "instance").mkdir(exist_ok=True)
            (tmp_path / "instance" / "config.yaml").write_text(
                yaml.dump(user_config)
            )

    def test_no_drift_identical_configs(self, tmp_path):
        config = {"max_runs_per_day": 20, "debug": False}
        self._setup_configs(tmp_path, config)
        missing = detect_config_drift(str(tmp_path), user_config=config)
        assert missing == []

    def test_detects_missing_top_level_key(self, tmp_path):
        template = {"max_runs_per_day": 20, "debug": False, "fast_reply": True}
        user = {"max_runs_per_day": 20}
        self._setup_configs(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert "debug" in missing
        assert "fast_reply" in missing

    def test_detects_missing_nested_section(self, tmp_path):
        template = {"max_runs_per_day": 20, "auto_update": {"enabled": True, "notify": False}}
        user = {"max_runs_per_day": 20}
        self._setup_configs(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config=user)
        # Parent section is missing — children should be filtered out
        assert "auto_update" in missing
        assert "auto_update.enabled" not in missing
        assert "auto_update.notify" not in missing

    def test_detects_missing_nested_key_when_section_exists(self, tmp_path):
        template = {"budget": {"warn_at_percent": 70, "stop_at_percent": 85}}
        user = {"budget": {"warn_at_percent": 70}}
        self._setup_configs(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert "budget.stop_at_percent" in missing
        assert "budget" not in missing

    def test_ignores_user_only_keys(self, tmp_path):
        """Keys in user config but not in template are not reported."""
        template = {"max_runs_per_day": 20}
        user = {"max_runs_per_day": 20, "custom_setting": "hello"}
        self._setup_configs(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert missing == []

    def test_missing_template_returns_empty(self, tmp_path):
        missing = detect_config_drift(str(tmp_path), user_config={"a": 1})
        assert missing == []

    def test_loads_user_config_from_file(self, tmp_path):
        """When user_config is None, loads from instance/config.yaml."""
        template = {"max_runs_per_day": 20, "debug": False}
        user = {"max_runs_per_day": 20}
        self._setup_configs(tmp_path, template, user_config=user)
        missing = detect_config_drift(str(tmp_path))
        assert "debug" in missing

    def test_missing_user_config_file_returns_empty(self, tmp_path):
        template = {"a": 1}
        self._setup_configs(tmp_path, template)
        # No instance/config.yaml created
        missing = detect_config_drift(str(tmp_path))
        assert missing == []

    def test_empty_template_returns_empty(self, tmp_path):
        self._setup_configs(tmp_path, {})
        missing = detect_config_drift(str(tmp_path), user_config={"a": 1})
        assert missing == []

    def test_results_are_sorted(self, tmp_path):
        template = {"z_key": 1, "a_key": 2, "m_key": 3}
        self._setup_configs(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config={})
        assert missing == ["a_key", "m_key", "z_key"]

    def test_real_world_scenario(self, tmp_path):
        """Simulate a user who installed months ago and missed new features."""
        template = {
            "max_runs_per_day": 20,
            "interval_seconds": 300,
            "fast_reply": False,
            "auto_update": {"enabled": False, "check_interval": 10, "notify": True},
            "dashboard": {"enabled": False, "port": 5001},
        }
        user = {
            "max_runs_per_day": 20,
            "interval_seconds": 300,
        }
        self._setup_configs(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert "fast_reply" in missing
        assert "auto_update" in missing
        assert "dashboard" in missing
        # Children of missing parents should be filtered
        assert "auto_update.enabled" not in missing
        assert "dashboard.port" not in missing

    def test_commented_key_excluded_from_drift(self, tmp_path):
        """A key commented out in user config should not be reported as drift."""
        template = {"max_runs_per_day": 20, "debug": False, "fast_reply": True}
        user_text = "max_runs_per_day: 20\n# debug: false\n"
        self._setup_configs(tmp_path, template, user_config_text=user_text)
        user = {"max_runs_per_day": 20}
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert "debug" not in missing
        assert "fast_reply" in missing

    def test_commented_nested_key_excluded(self, tmp_path):
        """A nested key commented out should not be reported."""
        template = {"budget": {"warn_at_percent": 70, "stop_at_percent": 85}}
        user_text = "budget:\n  warn_at_percent: 70\n  # stop_at_percent: 85\n"
        self._setup_configs(tmp_path, template, user_config_text=user_text)
        user = {"budget": {"warn_at_percent": 70}}
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert missing == []

    def test_commented_section_excludes_children(self, tmp_path):
        """A whole section commented out should not report the section or children."""
        template = {"auto_update": {"enabled": True, "notify": False}}
        user_text = "# auto_update:\n#   enabled: true\n#   notify: false\n"
        self._setup_configs(tmp_path, template, user_config_text=user_text)
        user = {}
        missing = detect_config_drift(str(tmp_path), user_config=user)
        assert missing == []

    def test_no_user_config_file_skips_comment_check(self, tmp_path):
        """When user_config is passed but no file exists, comment check is skipped gracefully."""
        template = {"debug": False}
        self._setup_configs(tmp_path, template)
        # No instance/config.yaml — pass user_config directly
        missing = detect_config_drift(str(tmp_path), user_config={})
        assert "debug" in missing
