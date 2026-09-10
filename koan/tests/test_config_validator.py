"""Tests for app.config_validator — startup config.yaml validation."""

import pytest

from app.config_validator import (
    validate_config, validate_and_warn, _check_type, _check_schedule_overlap,
    _suggest_typo, detect_config_drift, find_extra_config_keys,
    _collect_keys, _find_commented_keys,
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
# Schema completeness — every _NESTED key must have a SECTION_SCHEMA
# ---------------------------------------------------------------------------

class TestSchemaCompleteness:
    def test_all_nested_keys_have_section_schemas(self):
        """Every top-level key marked as _NESTED in CONFIG_SCHEMA must have
        a corresponding entry in SECTION_SCHEMAS (or be validated inline).
        Missing entries mean nested keys won't get type-checked or
        typo-detected at startup."""
        from app.config_validator import (
            CONFIG_SCHEMA, SECTION_SCHEMAS, _INLINE_VALIDATED_NESTED_KEYS,
        )
        nested_keys = [k for k, v in CONFIG_SCHEMA.items() if v == "dict"]
        missing = [
            k for k in nested_keys
            if k not in SECTION_SCHEMAS and k not in _INLINE_VALIDATED_NESTED_KEYS
        ]
        assert missing == [], (
            f"CONFIG_SCHEMA marks these as nested but SECTION_SCHEMAS "
            f"has no entry for them: {missing}"
        )

    def test_section_schemas_only_for_nested_keys(self):
        """SECTION_SCHEMAS should not define schemas for keys that aren't
        declared as _NESTED in CONFIG_SCHEMA (stale section after rename)."""
        from app.config_validator import CONFIG_SCHEMA, SECTION_SCHEMAS
        nested_keys = {k for k, v in CONFIG_SCHEMA.items() if v == "dict"}
        orphans = [k for k in SECTION_SCHEMAS if k not in nested_keys]
        assert orphans == [], (
            f"SECTION_SCHEMAS defines schemas for keys not in CONFIG_SCHEMA: {orphans}"
        )


# ---------------------------------------------------------------------------
# validate_config — valid configs
# ---------------------------------------------------------------------------

class TestDeprecatedLocalProvider:
    def test_cli_provider_local_triggers_deprecation_warning(self):
        warnings = validate_config({"cli_provider": "local"})
        joined = " ".join(msg for _path, msg in warnings).lower()
        assert "removed" in joined
        assert "ollama-launch" in joined

    def test_cli_provider_claude_has_no_local_warning(self):
        warnings = validate_config({"cli_provider": "claude"})
        joined = " ".join(msg for _path, msg in warnings).lower()
        assert "ollama-launch" not in joined


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
            "skill_timeout": 7200,
            "contemplative_chance": 10,
            "start_on_pause": False,
            "skip_permissions": False,
            "strip_co_authored_by": True,
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
                "max_check_interval_seconds": 300,
            },
            "notification_polling": {
                "check_interval_seconds": 60,
                "max_check_interval_seconds": 300,
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

    def test_github_multi_instance_and_scan_keys_are_known(self):
        config = {
            "enable_multiple_instances": True,
            "github": {
                "mention_scan_interval_minutes": 5,
                "review_scan_interval_minutes": 15,
                "parallel_workers": 4,
                "ack_enabled": False,
                "stale_drain_hours": 48,
            },
        }
        assert validate_config(config) == []

    def test_unlimited_quota_is_known_under_usage(self):
        # The code reads usage.unlimited_quota — not a top-level key.
        assert validate_config({"usage": {"unlimited_quota": True}}) == []

    def test_strip_co_authored_by_is_known(self):
        # Read by config.is_strip_co_authored_by_enabled() — must validate clean.
        assert validate_config({"strip_co_authored_by": True}) == []

    def test_strip_co_authored_by_rejects_non_bool(self):
        warnings = validate_config({"strip_co_authored_by": "yes"})
        assert len(warnings) == 1
        assert "strip_co_authored_by" in warnings[0][1]


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

    def test_top_level_unlimited_quota_is_deprecated_not_unrecognized(self):
        # unlimited_quota's canonical home is usage.unlimited_quota. A legacy
        # top-level placement is honored but must be flagged with a single
        # migration hint — never as a contradictory "unrecognized key".
        warnings = validate_config({"unlimited_quota": True})
        assert len(warnings) == 1
        assert "moved to 'usage.unlimited_quota'" in warnings[0][1]
        assert not any("unrecognized key 'unlimited_quota'" in m for _, m in warnings)

    def test_review_draft_skip_recognized(self):
        # The new opt-in review gate is a registered nested section, so a valid
        # config must not warn about it.
        warnings = validate_config({"review_draft_skip": {"enabled": True}})
        assert not any("unrecognized key 'review_draft_skip'" in m for _, m in warnings)

    def test_mission_limits_is_recognized_with_size_strings_and_null(self):
        warnings = validate_config({"mission_limits": {
            "enabled": True,
            "memory_reserve": "2G",
            "memory_min": "1G",
            "memory_max": None,
        }})
        assert warnings == []

    def test_mission_limits_accepts_a_bare_byte_count(self):
        warnings = validate_config(
            {"mission_limits": {"memory_max": 6442450944}}
        )
        assert warnings == []

    def test_mission_limits_typo_is_suggested(self):
        warnings = validate_config({"mission_limits": {"memory_resrve": "2G"}})
        assert len(warnings) == 1
        assert "did you mean 'mission_limits.memory_reserve'" in warnings[0][1]


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

    def test_review_draft_skip_enabled_non_bool(self):
        warnings = validate_config({"review_draft_skip": {"enabled": "yes"}})
        assert len(warnings) == 1
        assert "review_draft_skip.enabled" in warnings[0][1]
        assert "should be bool" in warnings[0][1]

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

    def test_top_level_unlimited_quota_warns_to_move_under_usage(self):
        warnings = validate_config({"unlimited_quota": True, "usage": {}})

        assert (
            "unlimited_quota",
            "'unlimited_quota' moved to 'usage.unlimited_quota'; "
            "the top-level form is deprecated",
        ) in warnings


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


class TestValidateConfigJiraMigration:
    def test_jira_projects_produces_deprecation_warning(self):
        warnings = validate_config({
            "jira": {"projects": {"FOO": "my-toolkit"}},
        })

        assert ("jira.projects",) == tuple(
            path for path, msg in warnings if "ignored" in msg
        )


class TestValidateOptimizationsNested:
    def test_rtk_nested_accepts_valid_values(self):
        warnings = validate_config({
            "optimizations": {
                "rtk": {
                    "enabled": "auto",
                    "awareness": True,
                    "require_jq": False,
                }
            }
        })

        assert warnings == []

    def test_rtk_nested_reports_unknown_type_and_bad_enabled_value(self):
        warnings = validate_config({
            "optimizations": {
                "rtk": {
                    "enabled": "yse",
                    "awareness": "yes",
                    "requre_jq": True,
                }
            }
        })

        by_path = {path: msg for path, msg in warnings}
        assert "optimizations.rtk.enabled" in by_path
        assert "one of" in by_path["optimizations.rtk.enabled"]
        assert "optimizations.rtk.awareness" in by_path
        assert "should be bool" in by_path["optimizations.rtk.awareness"]
        assert "optimizations.rtk.requre_jq" in by_path
        assert "did you mean 'optimizations.rtk.require_jq'" in by_path[
            "optimizations.rtk.requre_jq"
        ]

    def test_caveman_nested_reports_non_string_include_items(self):
        warnings = validate_config({
            "optimizations": {
                "caveman": {"enabled": True, "include": ["fix", 123]},
            }
        })

        assert (
            "optimizations.caveman.include[1]",
            "'optimizations.caveman.include[1]' should be str, got int",
        ) in warnings

    def test_review_compressor_nested_reports_unknown_and_type_errors(self):
        warnings = validate_config({
            "optimizations": {
                "review_compressor": {
                    "enabled": "yes",
                    "enabledd": True,
                }
            }
        })

        by_path = {path: msg for path, msg in warnings}
        assert "optimizations.review_compressor.enabled" in by_path
        assert "should be bool" in by_path["optimizations.review_compressor.enabled"]
        assert "optimizations.review_compressor.enabledd" in by_path
        assert "did you mean 'optimizations.review_compressor.enabled'" in by_path[
            "optimizations.review_compressor.enabledd"
        ]

    def test_effort_scalar_shorthand_is_allowed(self):
        assert validate_config({"effort": "high"}) == []

    def test_effort_scalar_invalid_level_warns(self):
        # A scalar typo must warn, not silently drop the flag (mirrors dict form).
        warnings = validate_config({"effort": "hihg"})
        assert any("effort" in path and "invalid effort" in msg
                   for path, msg in warnings)

    def test_effort_scalar_empty_is_allowed(self):
        # "" disables the flag — valid as a scalar too.
        assert validate_config({"effort": ""}) == []

    def test_effort_accepts_any_mission_type_key(self):
        # Mission types are an open set (plan/audit/chat/…) — any key with a
        # valid level is accepted, none should warn as "unrecognized".
        assert validate_config({
            "effort": {"plan": "high", "review": "low", "audit": "medium"},
        }) == []

    def test_effort_accepts_empty_value(self):
        # Empty string disables the flag for that type — still valid.
        assert validate_config({"effort": {"review": ""}}) == []

    def test_effort_rejects_invalid_level(self):
        warnings = validate_config({"effort": {"plan": "turbo"}})
        assert any("effort.plan" in path and "invalid effort" in msg
                   for path, msg in warnings)

    def test_effort_rejects_non_string_value(self):
        warnings = validate_config({"effort": {"review": 5}})
        assert any("effort.review" in path for path, msg in warnings)


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

    def test_invalid_template_yaml_returns_empty(self, tmp_path):
        (tmp_path / "instance.example").mkdir()
        (tmp_path / "instance.example" / "config.yaml").write_text("foo: [unclosed")

        assert detect_config_drift(str(tmp_path), user_config={}) == []

    def test_invalid_user_yaml_returns_empty_when_loading_from_file(self, tmp_path):
        self._setup_configs(tmp_path, {"debug": False}, user_config_text="foo: [unclosed")

        assert detect_config_drift(str(tmp_path)) == []

    def test_non_mapping_user_config_returns_empty(self, tmp_path):
        self._setup_configs(tmp_path, {"debug": False})

        assert detect_config_drift(str(tmp_path), user_config=["not", "a", "dict"]) == []


# ---------------------------------------------------------------------------
# find_extra_config_keys
# ---------------------------------------------------------------------------

class TestFindExtraConfigKeys:
    def _setup_template(self, tmp_path, template_config):
        import yaml
        (tmp_path / "instance.example").mkdir(exist_ok=True)
        (tmp_path / "instance.example" / "config.yaml").write_text(
            yaml.dump(template_config)
        )

    def test_no_extras(self, tmp_path):
        template = {"max_runs_per_day": 20, "debug": False}
        self._setup_template(tmp_path, template)
        extras = find_extra_config_keys(str(tmp_path), user_config=template)
        assert extras == []

    def test_detects_extra_top_level_key(self, tmp_path):
        template = {"max_runs_per_day": 20}
        user = {"max_runs_per_day": 20, "legacy_flag": True}
        self._setup_template(tmp_path, template)
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert extras == ["legacy_flag"]

    def test_detects_extra_nested_key(self, tmp_path):
        template = {"budget": {"warn_at_percent": 70}}
        user = {"budget": {"warn_at_percent": 70, "old_key": 1}}
        self._setup_template(tmp_path, template)
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert "budget.old_key" in extras
        assert "budget" not in extras

    def test_collapses_extra_parent_over_children(self, tmp_path):
        """If a whole section is extra, its children are not reported separately."""
        template = {"max_runs_per_day": 20}
        user = {"max_runs_per_day": 20, "removed": {"a": 1, "b": 2}}
        self._setup_template(tmp_path, template)
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert extras == ["removed"]

    def test_missing_template_returns_empty(self, tmp_path):
        # No template file written
        extras = find_extra_config_keys(str(tmp_path), user_config={"a": 1})
        assert extras == []

    def test_loads_user_config_from_file(self, tmp_path):
        import yaml
        template = {"max_runs_per_day": 20}
        user = {"max_runs_per_day": 20, "extra_key": "x"}
        self._setup_template(tmp_path, template)
        (tmp_path / "instance").mkdir(exist_ok=True)
        (tmp_path / "instance" / "config.yaml").write_text(yaml.dump(user))
        extras = find_extra_config_keys(str(tmp_path))
        assert extras == ["extra_key"]

    def test_missing_user_config_file_returns_empty(self, tmp_path):
        template = {"max_runs_per_day": 20}
        self._setup_template(tmp_path, template)
        extras = find_extra_config_keys(str(tmp_path))
        assert extras == []

    def test_results_are_sorted(self, tmp_path):
        template = {}
        user = {"z_key": 1, "a_key": 2, "m_key": 3}
        self._setup_template(tmp_path, template)
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert extras == ["a_key", "m_key", "z_key"]

    def test_both_drift_directions_independent(self, tmp_path):
        """Template and user configs with drift in both directions: each fn reports its side."""
        template = {"only_in_template": 1, "shared": 2}
        user = {"only_in_user": 3, "shared": 2}
        self._setup_template(tmp_path, template)
        missing = detect_config_drift(str(tmp_path), user_config=user)
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert missing == ["only_in_template"]
        assert extras == ["only_in_user"]

    def test_commented_template_key_not_reported_as_extra(self, tmp_path):
        """A key shown commented-out in the template is an opt-in default, not a typo."""
        (tmp_path / "instance.example").mkdir(exist_ok=True)
        # `auto_pause` is documented in the template as a commented default.
        (tmp_path / "instance.example" / "config.yaml").write_text(
            "max_runs_per_day: 20\n# auto_pause: false\n"
        )
        user = {"max_runs_per_day": 20, "auto_pause": True}
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert extras == []

    def test_commented_nested_template_key_not_reported(self, tmp_path):
        """A nested commented template key is also accepted when the user uncomments it."""
        (tmp_path / "instance.example").mkdir(exist_ok=True)
        (tmp_path / "instance.example" / "config.yaml").write_text(
            "budget:\n  warn_at_percent: 70\n  # stop_at_percent: 85\n"
        )
        user = {"budget": {"warn_at_percent": 70, "stop_at_percent": 85}}
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert extras == []

    def test_uncommented_key_still_detected_as_typo(self, tmp_path):
        """A genuinely unknown key is still reported even when other keys are commented."""
        (tmp_path / "instance.example").mkdir(exist_ok=True)
        (tmp_path / "instance.example" / "config.yaml").write_text(
            "max_runs_per_day: 20\n# auto_pause: false\n"
        )
        user = {"max_runs_per_day": 20, "totally_unknown": 1}
        extras = find_extra_config_keys(str(tmp_path), user_config=user)
        assert extras == ["totally_unknown"]

    def test_invalid_template_yaml_returns_empty(self, tmp_path):
        (tmp_path / "instance.example").mkdir(exist_ok=True)
        (tmp_path / "instance.example" / "config.yaml").write_text("foo: [unclosed")

        assert find_extra_config_keys(str(tmp_path), user_config={"extra": True}) == []

    def test_invalid_user_yaml_returns_empty_when_loading_from_file(self, tmp_path):
        self._setup_template(tmp_path, {"max_runs_per_day": 20})
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "config.yaml").write_text("foo: [unclosed")

        assert find_extra_config_keys(str(tmp_path)) == []

    def test_non_mapping_user_config_returns_empty(self, tmp_path):
        self._setup_template(tmp_path, {"max_runs_per_day": 20})

        assert find_extra_config_keys(str(tmp_path), user_config=["bad"]) == []


# ---------------------------------------------------------------------------
# validate_config_or_raise
# ---------------------------------------------------------------------------

class TestValidateConfigOrRaise:
    """Tests for validate_config_or_raise() — strict startup validation."""

    def _write_config(self, tmp_path, content):
        instance = tmp_path / "instance"
        instance.mkdir(exist_ok=True)
        (instance / "config.yaml").write_text(content)

    def test_missing_file_is_ok(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        validate_config_or_raise(str(tmp_path))

    def test_empty_file_is_ok(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "")
        validate_config_or_raise(str(tmp_path))

    def test_valid_config_passes(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "max_runs_per_day: 20\ninterval_seconds: 300\n")
        validate_config_or_raise(str(tmp_path))

    def test_raises_on_broken_yaml(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, ":\n  bad: [yaml\n  unclosed")
        with pytest.raises(ValueError, match="Invalid YAML"):
            validate_config_or_raise(str(tmp_path))

    def test_raises_on_non_dict_root(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "- this is a list\n- not a dict")
        with pytest.raises(ValueError, match="root must be a mapping"):
            validate_config_or_raise(str(tmp_path))

    def test_raises_on_section_type_mismatch(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, 'tools: "not-a-dict"\n')
        with pytest.raises(ValueError, match="'tools' must be a mapping"):
            validate_config_or_raise(str(tmp_path))

    def test_raises_on_scalar_type_mismatch(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "max_runs_per_day: not-a-number\n")
        with pytest.raises(ValueError, match="'max_runs_per_day' must be int"):
            validate_config_or_raise(str(tmp_path))

    def test_multiple_errors_collected(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, 'tools: "bad"\nmodels: 42\n')
        with pytest.raises(ValueError, match="config.yaml has invalid entries"):
            validate_config_or_raise(str(tmp_path))

    def test_effort_string_shorthand_accepted(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, 'effort: "high"\n')
        validate_config_or_raise(str(tmp_path))

    def test_stagnation_false_accepted(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "stagnation: false\n")
        validate_config_or_raise(str(tmp_path))

    def test_ci_check_bool_shorthand_accepted(self, tmp_path):
        """ci_check: true bool shorthand must not hard-stop startup."""
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "ci_check: true\n")
        validate_config_or_raise(str(tmp_path))

    def test_ci_check_dict_form_accepted(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "ci_check:\n  enabled: true\n  timeout: 1800\n")
        validate_config_or_raise(str(tmp_path))

    def test_unknown_keys_not_flagged(self, tmp_path):
        """Unknown keys are handled by validate_and_warn, not the strict check."""
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "unknown_key: 42\n")
        validate_config_or_raise(str(tmp_path))

    def test_legacy_mcp_list_accepted(self, tmp_path):
        """A pre-MCP-server ``mcp: [paths]`` list must not hard-stop startup.

        Runtime still honors it, and app.config_migration reshapes it — but an
        unmigrated file (read-only mount, manual rollback) must still boot.
        """
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, "mcp:\n- /a.json\n")
        validate_config_or_raise(str(tmp_path))

    def test_mcp_mapping_form_accepted(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(
            tmp_path, "mcp:\n  enabled: true\n  configs:\n    - /a.json\n"
        )
        validate_config_or_raise(str(tmp_path))

    def test_mcp_scalar_still_rejected(self, tmp_path):
        from app.config_validator import validate_config_or_raise
        self._write_config(tmp_path, 'mcp: "/a.json"\n')
        with pytest.raises(ValueError, match="'mcp' must be a mapping"):
            validate_config_or_raise(str(tmp_path))

    def test_shorthands_agree_with_the_advisory_validator(self, tmp_path):
        """Every shape the strict check accepts must also be warning-free.

        The two validators disagreeing is what turned a working ``mcp:`` list
        into a hard startup stop; this pins them together.
        """
        import yaml

        from app.config_validator import validate_config, validate_config_or_raise

        for snippet in (
            "mcp:\n- /a.json\n",
            'effort: "high"\n',
            "stagnation: false\n",
            "ci_check: true\n",
            "running_indicator: true\n",
        ):
            self._write_config(tmp_path, snippet)
            validate_config_or_raise(str(tmp_path))
            assert validate_config(yaml.safe_load(snippet)) == [], snippet


class TestValidateModelsSection:
    """models: accepts flat roles AND per-provider blocks (models.<flavor>).

    Provider block names are an open set; role keys inside a block are
    validated against the known-role set with typo suggestions.
    """

    def test_flat_role_form_accepted(self):
        assert validate_config({"models": {"mission": "opus", "chat": ""}}) == []

    def test_default_block_accepted(self):
        config = {"models": {"default": {"mission": "", "reflect": "sonnet"}}}
        assert validate_config(config) == []

    def test_known_provider_blocks_accepted(self):
        config = {"models": {
            "claude": {"mission": "opus"},
            "codex": {"review_mode": "gpt-5.5"},
            "ollama-launch": {"mission": "some-model:cloud"},
        }}
        assert validate_config(config) == []

    def test_provider_block_underscore_variant_accepted(self):
        assert validate_config({"models": {"ollama_launch": {"mission": "m"}}}) == []

    def test_mixed_flat_and_blocks_accepted(self):
        config = {"models": {
            "lightweight": "haiku",
            "default": {"mission": ""},
            "claude": {"chat": "haiku"},
        }}
        assert validate_config(config) == []

    def test_unknown_block_name_accepted(self):
        # Provider block names are an open set: any dict is treated as a
        # provider block so a newly registered provider needs no validator
        # edit. The trade-off is that a typo'd flavor is not flagged.
        assert validate_config({"models": {"bogusflavor": {"mission": "x"}}}) == []

    def test_unknown_role_inside_block_warned(self):
        warnings = validate_config({"models": {"claude": {"missoin": "opus"}}})
        assert len(warnings) == 1
        assert "unrecognized key 'models.claude.missoin'" in warnings[0][1]
        assert "did you mean 'models.claude.mission'" in warnings[0][1]

    def test_non_dict_non_role_key_warned(self):
        # A scalar under a non-role key is neither a provider block nor a
        # legacy flat role, so it is reported as unrecognized.
        warnings = validate_config({"models": {"claude": "opus"}})
        assert len(warnings) == 1
        assert "unrecognized key 'models.claude'" in warnings[0][1]

    def test_role_value_must_be_str(self):
        warnings = validate_config({"models": {"mission": 5}})
        assert len(warnings) == 1
        assert "'models.mission' should be str" in warnings[0][1]

    def test_role_value_inside_block_must_be_str(self):
        warnings = validate_config({"models": {"claude": {"mission": 5}}})
        assert len(warnings) == 1
        assert "'models.claude.mission' should be str" in warnings[0][1]

    def test_flat_typo_gets_suggestion(self):
        warnings = validate_config({"models": {"missin": "opus"}})
        assert len(warnings) == 1
        assert "did you mean 'models.mission'" in warnings[0][1]


class TestPreflightCacheMinutesKey:
    def test_key_accepted(self):
        assert validate_config({"preflight_cache_minutes": 10}) == []

    def test_type_checked(self):
        warnings = validate_config({"preflight_cache_minutes": "ten"})
        assert len(warnings) == 1
        assert "should be int" in warnings[0][1]


class TestConfigSyncSection:
    def test_config_sync_section_valid(self):
        errors = validate_config({"config_sync": {"enabled": True}})
        assert not errors  # well-formed section produces no errors
