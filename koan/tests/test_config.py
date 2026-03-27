"""Tests for app.config — configuration loading and access."""

import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest


@contextmanager
def _mock_config(data: dict):
    """Mock load_config to return a specific config dict."""
    with patch("app.config._load_config", return_value=data):
        yield


# --- get_chat_tools ---


class TestGetChatTools:
    def test_default(self):
        from app.config import get_chat_tools

        with _mock_config({}):
            assert get_chat_tools() == "Read,Glob,Grep"

    def test_custom(self):
        from app.config import get_chat_tools

        with _mock_config({"tools": {"chat": ["Read", "Write"]}}):
            assert get_chat_tools() == "Read,Write"

    def test_string_value_passed_through(self):
        from app.config import get_chat_tools

        with _mock_config({"tools": {"chat": "Read,Custom"}}):
            assert get_chat_tools() == "Read,Custom"

    def test_non_list_non_string_uses_default(self):
        from app.config import get_chat_tools

        with _mock_config({"tools": {"chat": 42}}):
            assert get_chat_tools() == "Read,Glob,Grep"


# --- get_mission_tools ---


class TestGetMissionTools:
    def test_default(self):
        from app.config import get_mission_tools

        with _mock_config({}):
            assert get_mission_tools() == "Read,Glob,Grep,Edit,Write,Bash,Skill"

    def test_custom(self):
        from app.config import get_mission_tools

        with _mock_config({"tools": {"mission": ["Read", "Bash"]}}):
            assert get_mission_tools() == "Read,Bash"


# --- get_contemplative_tools ---


class TestGetContemplativeTools:
    def test_default(self):
        from app.config import get_contemplative_tools

        with _mock_config({}):
            assert get_contemplative_tools() == "Read,Write,Glob,Grep"

    def test_custom(self):
        from app.config import get_contemplative_tools

        with _mock_config({"tools": {"contemplative": ["Read", "Glob", "Grep", "Bash"]}}):
            assert get_contemplative_tools() == "Read,Glob,Grep,Bash"

    def test_string_value_passed_through(self):
        from app.config import get_contemplative_tools

        with _mock_config({"tools": {"contemplative": "Read,Write"}}):
            assert get_contemplative_tools() == "Read,Write"


# --- get_allowed_tools (backward compat) ---


class TestGetAllowedTools:
    def test_delegates_to_mission_tools(self):
        from app.config import get_allowed_tools

        with _mock_config({}):
            assert get_allowed_tools() == "Read,Glob,Grep,Edit,Write,Bash,Skill"


# --- get_tools_description ---


class TestGetToolsDescription:
    def test_default_empty(self):
        from app.config import get_tools_description

        with _mock_config({}):
            assert get_tools_description() == ""

    def test_custom(self):
        from app.config import get_tools_description

        with _mock_config({"tools": {"description": "Tools info"}}):
            assert get_tools_description() == "Tools info"


# --- get_model_config ---


class TestGetModelConfig:
    def test_defaults(self):
        from app.config import get_model_config

        with _mock_config({}):
            result = get_model_config()
        assert result["mission"] == ""
        assert result["chat"] == ""
        assert result["lightweight"] == "haiku"
        assert result["fallback"] == "sonnet"
        assert result["review_mode"] == ""

    def test_custom_models(self):
        from app.config import get_model_config

        with _mock_config({"models": {"mission": "opus", "chat": "sonnet"}}):
            result = get_model_config()
        assert result["mission"] == "opus"
        assert result["chat"] == "sonnet"
        assert result["lightweight"] == "haiku"  # not overridden


# --- get_start_on_pause ---


class TestGetStartOnPause:
    def test_default_false(self):
        from app.config import get_start_on_pause

        with _mock_config({}):
            assert get_start_on_pause() is False

    def test_enabled(self):
        from app.config import get_start_on_pause

        with _mock_config({"start_on_pause": True}):
            assert get_start_on_pause() is True


# --- get_auto_pause ---


class TestGetAutoPause:
    def test_default_true(self):
        from app.config import get_auto_pause

        with _mock_config({}):
            assert get_auto_pause() is True

    def test_disabled(self):
        from app.config import get_auto_pause

        with _mock_config({"auto_pause": False}):
            assert get_auto_pause() is False

    def test_explicit_true(self):
        from app.config import get_auto_pause

        with _mock_config({"auto_pause": True}):
            assert get_auto_pause() is True


# --- get_skip_permissions ---


class TestGetSkipPermissions:
    def test_default_false(self):
        from app.config import get_skip_permissions

        with _mock_config({}):
            assert get_skip_permissions() is False

    def test_enabled(self):
        from app.config import get_skip_permissions

        with _mock_config({"skip_permissions": True}):
            assert get_skip_permissions() is True

    def test_explicit_false(self):
        from app.config import get_skip_permissions

        with _mock_config({"skip_permissions": False}):
            assert get_skip_permissions() is False


# --- get_debug_enabled ---


class TestGetDebugEnabled:
    def test_default_false(self):
        from app.config import get_debug_enabled

        with _mock_config({}):
            assert get_debug_enabled() is False

    def test_explicit_true(self):
        from app.config import get_debug_enabled

        with _mock_config({"debug": True}):
            assert get_debug_enabled() is True

    def test_explicit_false(self):
        from app.config import get_debug_enabled

        with _mock_config({"debug": False}):
            assert get_debug_enabled() is False


# --- get_max_runs ---


class TestGetMaxRuns:
    def test_default(self):
        from app.config import get_max_runs

        with _mock_config({}):
            assert get_max_runs() == 20

    def test_custom(self):
        from app.config import get_max_runs

        with _mock_config({"max_runs_per_day": 50}):
            assert get_max_runs() == 50

    def test_string_value_coerced(self):
        from app.config import get_max_runs

        with _mock_config({"max_runs_per_day": "30"}):
            assert get_max_runs() == 30


# --- get_interval_seconds ---


class TestGetIntervalSeconds:
    def test_default(self):
        from app.config import get_interval_seconds

        with _mock_config({}):
            assert get_interval_seconds() == 300

    def test_custom(self):
        from app.config import get_interval_seconds

        with _mock_config({"interval_seconds": 120}):
            assert get_interval_seconds() == 120


# --- get_same_project_stickiness_percent ---


class TestGetSameProjectStickinessPercent:
    def test_default_disabled(self):
        from app.config import get_same_project_stickiness_percent

        with _mock_config({}):
            assert get_same_project_stickiness_percent() == 0

    def test_reads_nested_prompt_caching_value(self):
        from app.config import get_same_project_stickiness_percent

        with _mock_config({"prompt_caching": {"same_project_stickiness_percent": 35}}):
            assert get_same_project_stickiness_percent() == 35

    def test_clamps_out_of_range_values(self):
        from app.config import get_same_project_stickiness_percent

        with _mock_config({"prompt_caching": {"same_project_stickiness_percent": 999}}):
            assert get_same_project_stickiness_percent() == 100

        with _mock_config({"prompt_caching": {"same_project_stickiness_percent": -5}}):
            assert get_same_project_stickiness_percent() == 0


# --- get_fast_reply_model ---


class TestGetFastReplyModel:
    def test_disabled_by_default(self):
        from app.config import get_fast_reply_model

        with _mock_config({}):
            assert get_fast_reply_model() == ""

    def test_enabled_returns_lightweight(self):
        from app.config import get_fast_reply_model

        with _mock_config({"fast_reply": True, "models": {"lightweight": "flash"}}):
            assert get_fast_reply_model() == "flash"

    def test_enabled_uses_default_lightweight(self):
        from app.config import get_fast_reply_model

        with _mock_config({"fast_reply": True}):
            assert get_fast_reply_model() == "haiku"


# --- get_branch_prefix ---


class TestGetBranchPrefix:
    def test_default(self):
        from app.config import get_branch_prefix

        with _mock_config({}):
            assert get_branch_prefix() == "koan/"

    def test_custom(self):
        from app.config import get_branch_prefix

        with _mock_config({"branch_prefix": "mybot"}):
            assert get_branch_prefix() == "mybot/"

    def test_strips_trailing_slash(self):
        from app.config import get_branch_prefix

        with _mock_config({"branch_prefix": "agent/"}):
            assert get_branch_prefix() == "agent/"

    def test_empty_string_defaults_to_koan(self):
        from app.config import get_branch_prefix

        with _mock_config({"branch_prefix": ""}):
            assert get_branch_prefix() == "koan/"


# --- get_contemplative_chance ---


class TestGetContemplativeChance:
    def test_default(self):
        from app.config import get_contemplative_chance

        with _mock_config({}):
            assert get_contemplative_chance() == 10

    def test_custom(self):
        from app.config import get_contemplative_chance

        with _mock_config({"contemplative_chance": 25}):
            assert get_contemplative_chance() == 25

    def test_zero(self):
        from app.config import get_contemplative_chance

        with _mock_config({"contemplative_chance": 0}):
            assert get_contemplative_chance() == 0


# --- get_skill_timeout ---


class TestGetSkillTimeout:
    def test_default(self):
        from app.config import get_skill_timeout

        with _mock_config({}):
            assert get_skill_timeout() == 3600

    def test_custom(self):
        from app.config import get_skill_timeout

        with _mock_config({"skill_timeout": 1800}):
            assert get_skill_timeout() == 1800

    def test_string_value_coerced(self):
        from app.config import get_skill_timeout

        with _mock_config({"skill_timeout": "7200"}):
            assert get_skill_timeout() == 7200

    def test_invalid_string_returns_default(self):
        from app.config import get_skill_timeout

        with _mock_config({"skill_timeout": "forever"}):
            assert get_skill_timeout() == 3600

    def test_none_returns_default(self):
        from app.config import get_skill_timeout

        with _mock_config({"skill_timeout": None}):
            assert get_skill_timeout() == 3600


# --- get_skill_max_turns ---


class TestGetSkillMaxTurns:
    def test_default(self):
        from app.config import get_skill_max_turns

        with _mock_config({}):
            assert get_skill_max_turns() == 200

    def test_custom(self):
        from app.config import get_skill_max_turns

        with _mock_config({"skill_max_turns": 100}):
            assert get_skill_max_turns() == 100

    def test_string_value_coerced(self):
        from app.config import get_skill_max_turns

        with _mock_config({"skill_max_turns": "300"}):
            assert get_skill_max_turns() == 300

    def test_invalid_string_returns_default(self):
        from app.config import get_skill_max_turns

        with _mock_config({"skill_max_turns": "infinite"}):
            assert get_skill_max_turns() == 200


# --- get_mission_timeout ---


class TestGetMissionTimeout:
    def test_default(self):
        from app.config import get_mission_timeout

        with _mock_config({}):
            assert get_mission_timeout() == 3600

    def test_custom(self):
        from app.config import get_mission_timeout

        with _mock_config({"mission_timeout": 1800}):
            assert get_mission_timeout() == 1800

    def test_zero_disables(self):
        from app.config import get_mission_timeout

        with _mock_config({"mission_timeout": 0}):
            assert get_mission_timeout() == 0


# --- get_post_mission_timeout ---


class TestGetPostMissionTimeout:
    def test_default(self):
        from app.config import get_post_mission_timeout

        with _mock_config({}):
            assert get_post_mission_timeout() == 300

    def test_custom(self):
        from app.config import get_post_mission_timeout

        with _mock_config({"post_mission_timeout": 600}):
            assert get_post_mission_timeout() == 600

    def test_string_parsed(self):
        from app.config import get_post_mission_timeout

        with _mock_config({"post_mission_timeout": "120"}):
            assert get_post_mission_timeout() == 120

    def test_invalid_returns_default(self):
        from app.config import get_post_mission_timeout

        with _mock_config({"post_mission_timeout": "nope"}):
            assert get_post_mission_timeout() == 300


# --- build_claude_flags ---


class TestBuildClaudeFlags:
    def test_empty_returns_empty(self):
        from app.config import build_claude_flags

        with patch("app.cli_provider.build_cli_flags", return_value=[]):
            result = build_claude_flags()
        assert result == []

    def test_with_model(self):
        from app.config import build_claude_flags

        with patch("app.cli_provider.build_cli_flags", return_value=["--model", "opus"]) as mock:
            result = build_claude_flags(model="opus")
        mock.assert_called_once_with(model="opus", fallback="", disallowed_tools=None)
        assert result == ["--model", "opus"]


# --- get_auto_merge_config ---


class TestGetAutoMergeConfig:
    def test_defaults(self):
        from app.config import get_auto_merge_config

        config = {}
        result = get_auto_merge_config(config, "myproject")
        assert result["enabled"] is True
        assert result["base_branch"] == "main"
        assert result["strategy"] == "squash"
        assert result["rules"] == []

    def test_global_config(self):
        from app.config import get_auto_merge_config

        config = {"git_auto_merge": {"enabled": False, "strategy": "rebase"}}
        result = get_auto_merge_config(config, "myproject")
        assert result["enabled"] is False
        assert result["strategy"] == "rebase"

    def test_config_yaml_projects_section_ignored(self):
        """config.yaml projects: section is no longer used for per-project overrides.

        Per-project auto-merge config is now exclusively in projects.yaml.
        """
        from app.config import get_auto_merge_config

        config = {
            "git_auto_merge": {"enabled": True, "strategy": "squash"},
            "projects": {"myproject": {"git_auto_merge": {"strategy": "merge"}}},
        }
        result = get_auto_merge_config(config, "myproject")
        assert result["enabled"] is True
        # Should use global config, not the projects section override
        assert result["strategy"] == "squash"


# --- _safe_int ---


class TestSafeInt:
    def test_int_value(self):
        from app.config import _safe_int
        assert _safe_int(42, 0) == 42

    def test_string_int_value(self):
        from app.config import _safe_int
        assert _safe_int("30", 0) == 30

    def test_invalid_string_returns_default(self):
        from app.config import _safe_int
        assert _safe_int("abc", 20) == 20

    def test_none_returns_default(self):
        from app.config import _safe_int
        assert _safe_int(None, 10) == 10

    def test_float_string_returns_default(self):
        from app.config import _safe_int
        assert _safe_int("3.14", 5) == 5

    def test_empty_string_returns_default(self):
        from app.config import _safe_int
        assert _safe_int("", 7) == 7


class TestGetMaxRunsInvalidConfig:
    def test_invalid_string_returns_default(self):
        from app.config import get_max_runs
        with _mock_config({"max_runs_per_day": "not_a_number"}):
            assert get_max_runs() == 20

    def test_none_returns_default(self):
        from app.config import get_max_runs
        with _mock_config({"max_runs_per_day": None}):
            assert get_max_runs() == 20


class TestGetIntervalSecondsInvalidConfig:
    def test_invalid_string_returns_default(self):
        from app.config import get_interval_seconds
        with _mock_config({"interval_seconds": "slow"}):
            assert get_interval_seconds() == 300


class TestGetContemplativeChanceInvalidConfig:
    def test_invalid_string_returns_default(self):
        from app.config import get_contemplative_chance
        with _mock_config({"contemplative_chance": "high"}):
            assert get_contemplative_chance() == 10


# --- get_claude_flags_for_role ---


class TestGetClaudeFlagsForRole:
    def test_mission_role(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "haiku", "lightweight": "haiku",
                 "fallback": "opus", "review_mode": "",
             }), \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = ["--model", "sonnet", "--fallback", "opus"]
            result = get_claude_flags_for_role("mission")
            mock_prov.return_value.build_extra_flags.assert_called_once_with(
                model="sonnet", fallback="opus", disallowed_tools=None
            )
            assert result == "--model sonnet --fallback opus"

    def test_mission_review_mode(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "haiku", "lightweight": "haiku",
                 "fallback": "opus", "review_mode": "haiku",
             }), \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = []
            get_claude_flags_for_role("mission", autonomous_mode="review")
            call_kwargs = mock_prov.return_value.build_extra_flags.call_args[1]
            assert call_kwargs["model"] == "haiku"
            assert call_kwargs["disallowed_tools"] == ["Bash", "Edit", "Write"]

    def test_contemplative_role(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "haiku", "lightweight": "haiku",
                 "fallback": "opus", "review_mode": "",
             }), \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = ["--model", "haiku"]
            get_claude_flags_for_role("contemplative")
            call_kwargs = mock_prov.return_value.build_extra_flags.call_args[1]
            assert call_kwargs["model"] == "haiku"
            assert call_kwargs["fallback"] == ""

    def test_chat_role(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "opus", "lightweight": "haiku",
                 "fallback": "sonnet", "review_mode": "",
             }), \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = []
            get_claude_flags_for_role("chat")
            call_kwargs = mock_prov.return_value.build_extra_flags.call_args[1]
            assert call_kwargs["model"] == "opus"
            assert call_kwargs["fallback"] == "sonnet"
            assert call_kwargs["disallowed_tools"] is None

    def test_unknown_role_passes_empty(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "haiku", "lightweight": "haiku",
                 "fallback": "opus", "review_mode": "",
             }), \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = []
            result = get_claude_flags_for_role("lightweight")
            call_kwargs = mock_prov.return_value.build_extra_flags.call_args[1]
            # "lightweight" has no explicit branch — model stays ""
            assert call_kwargs["model"] == ""
            assert result == ""

    def test_project_name_passed_to_model_config(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "haiku", "lightweight": "haiku",
                 "fallback": "", "review_mode": "",
             }) as mock_models, \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = []
            get_claude_flags_for_role("mission", project_name="myapp")
            mock_models.assert_called_once_with("myapp")

    def test_mission_review_mode_empty_uses_mission_model(self):
        from app.config import get_claude_flags_for_role
        with _mock_config({}), \
             patch("app.config.get_model_config", return_value={
                 "mission": "sonnet", "chat": "haiku", "lightweight": "haiku",
                 "fallback": "opus", "review_mode": "",
             }), \
             patch("app.cli_provider.get_provider") as mock_prov:
            mock_prov.return_value.build_extra_flags.return_value = []
            get_claude_flags_for_role("mission", autonomous_mode="review")
            call_kwargs = mock_prov.return_value.build_extra_flags.call_args[1]
            # review_mode="" means keep mission model
            assert call_kwargs["model"] == "sonnet"


# --- backward compatibility ---


class TestDashboardConfig:
    """Tests for dashboard config getters."""

    def test_dashboard_disabled_by_default(self):
        from app.config import is_dashboard_enabled
        with _mock_config({}):
            assert not is_dashboard_enabled()

    def test_dashboard_enabled(self):
        from app.config import is_dashboard_enabled
        with _mock_config({"dashboard": {"enabled": True}}):
            assert is_dashboard_enabled()

    def test_dashboard_disabled_explicitly(self):
        from app.config import is_dashboard_enabled
        with _mock_config({"dashboard": {"enabled": False}}):
            assert not is_dashboard_enabled()

    def test_dashboard_non_dict_value(self):
        from app.config import is_dashboard_enabled
        with _mock_config({"dashboard": "yes"}):
            assert not is_dashboard_enabled()

    def test_dashboard_port_default(self):
        from app.config import get_dashboard_port
        with _mock_config({}):
            assert get_dashboard_port() == 5001

    def test_dashboard_port_custom(self):
        from app.config import get_dashboard_port
        with _mock_config({"dashboard": {"port": 8080}}):
            assert get_dashboard_port() == 8080


class TestBackwardCompat:
    """Verify that importing from app.utils still works."""

    def test_config_functions_accessible_from_utils(self):
        from app.utils import get_chat_tools, get_model_config, get_branch_prefix
        # Just verify they're importable (not None)
        assert callable(get_chat_tools)
        assert callable(get_model_config)
        assert callable(get_branch_prefix)
