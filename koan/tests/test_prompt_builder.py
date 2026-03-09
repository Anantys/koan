"""Tests for prompt_builder.py — agent and contemplative prompt assembly."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.prompt_builder import (
    build_agent_prompt,
    build_contemplative_prompt,
    _is_auto_merge_enabled,
    _get_merge_policy,
    _get_submit_pr_section,
    _get_deep_research,
    _get_staleness_section,
    _get_mission_type_section,
    _get_tdd_section,
    _get_verbose_section,
)


@pytest.fixture
def prompt_env(tmp_path):
    """Create a minimal environment for prompt builder testing."""
    instance = tmp_path / "instance"
    project_path = tmp_path / "project"
    project_name = "testproj"

    # Create directory structure
    (instance / "memory" / "projects" / project_name).mkdir(parents=True)
    (instance / "journal").mkdir(parents=True)
    project_path.mkdir()

    return {
        "instance": str(instance),
        "project_path": str(project_path),
        "project_name": project_name,
        "koan_root": str(tmp_path),
    }


# --- Tests for _is_auto_merge_enabled ---


class TestIsAutoMergeEnabled:
    """Tests for auto-merge config detection."""

    @patch("app.config.get_auto_merge_config")
    @patch("app.prompt_builder._load_config_safe")
    def test_enabled_with_rules(self, mock_load_config, mock_merge_cfg):
        mock_load_config.return_value = {}
        mock_merge_cfg.return_value = {"enabled": True, "rules": ["*"]}
        assert _is_auto_merge_enabled("myproject") is True

    @patch("app.config.get_auto_merge_config")
    @patch("app.prompt_builder._load_config_safe")
    def test_disabled(self, mock_load_config, mock_merge_cfg):
        mock_load_config.return_value = {}
        mock_merge_cfg.return_value = {"enabled": False, "rules": ["*"]}
        assert _is_auto_merge_enabled("myproject") is False

    @patch("app.config.get_auto_merge_config")
    @patch("app.prompt_builder._load_config_safe")
    def test_enabled_no_rules(self, mock_load_config, mock_merge_cfg):
        mock_load_config.return_value = {}
        mock_merge_cfg.return_value = {"enabled": True, "rules": []}
        assert _is_auto_merge_enabled("myproject") is False

    @patch("app.prompt_builder._load_config_safe", side_effect=ImportError("no config"))
    def test_config_error_returns_false(self, _mock):
        assert _is_auto_merge_enabled("myproject") is False


# --- Tests for _get_merge_policy ---


class TestGetMergePolicy:
    """Tests for merge policy text generation."""

    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=True)
    def test_auto_merge_enabled(self, _mock_merge, _mock_prefix):
        policy = _get_merge_policy("proj")
        assert "Auto-Merge Enabled" in policy
        assert "auto-merge system handles the merge" in policy

    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=False)
    def test_auto_merge_disabled(self, _mock_merge, _mock_prefix):
        policy = _get_merge_policy("proj")
        assert "NOT configured" in policy
        assert "DO NOT merge yourself" in policy


# --- Tests for _get_submit_pr_section ---


class TestGetSubmitPrSection:
    """Tests for submit-pull-request section (always included)."""

    def test_includes_section(self):
        result = _get_submit_pr_section("/tmp/project")
        assert "Audit Missions" in result
        assert "GitHub Issue Follow-up" in result
        assert "/tmp/project" in result

    def test_substitutes_project_path(self):
        result = _get_submit_pr_section("/home/user/myproject")
        assert "/home/user/myproject" in result


# --- Tests for _get_deep_research ---


class TestGetDeepResearch:
    """Tests for deep research injection."""

    @patch("app.deep_research.DeepResearch")
    def test_with_suggestions(self, mock_cls, prompt_env):
        mock_instance = MagicMock()
        mock_instance.format_for_agent.return_value = "## Topic 1\n- Something"
        mock_cls.return_value = mock_instance

        result = _get_deep_research(
            prompt_env["instance"],
            prompt_env["project_name"],
            prompt_env["project_path"],
        )
        assert "# Deep Research Analysis" in result
        assert "Topic 1" in result

    @patch("app.deep_research.DeepResearch")
    def test_no_suggestions(self, mock_cls, prompt_env):
        mock_instance = MagicMock()
        mock_instance.format_for_agent.return_value = ""
        mock_cls.return_value = mock_instance

        result = _get_deep_research(
            prompt_env["instance"],
            prompt_env["project_name"],
            prompt_env["project_path"],
        )
        assert result == ""

    @patch("app.deep_research.DeepResearch", side_effect=Exception("boom"))
    def test_error_returns_empty(self, _mock, prompt_env):
        result = _get_deep_research(
            prompt_env["instance"],
            prompt_env["project_name"],
            prompt_env["project_path"],
        )
        assert result == ""


# --- Tests for _get_staleness_section ---


class TestGetStalenessSection:
    """Tests for staleness warning injection."""

    @patch("app.session_tracker.get_staleness_warning", return_value="")
    def test_no_warning_when_fresh(self, mock_warning, prompt_env):
        result = _get_staleness_section(prompt_env["instance"], "testproj")
        assert result == ""

    @patch("app.session_tracker.get_staleness_warning")
    def test_warning_when_stale(self, mock_warning, prompt_env):
        mock_warning.return_value = (
            "### WARNING: Project Staleness Detected\n\n"
            "Last 3 sessions found nothing actionable."
        )
        result = _get_staleness_section(prompt_env["instance"], "testproj")
        assert "# Session History Feedback" in result
        assert "WARNING" in result
        assert "3 sessions" in result

    @patch("app.session_tracker.get_staleness_warning", side_effect=Exception("boom"))
    def test_error_returns_empty(self, mock_warning, prompt_env):
        result = _get_staleness_section(prompt_env["instance"], "testproj")
        assert result == ""

    @patch("app.session_tracker.get_staleness_warning")
    def test_critical_warning(self, mock_warning, prompt_env):
        mock_warning.return_value = (
            "### CRITICAL: Project Staleness Detected\n\n"
            "STOP doing verification/housekeeping."
        )
        result = _get_staleness_section(prompt_env["instance"], "testproj")
        assert "CRITICAL" in result
        assert "STOP" in result


# --- Tests for _get_verbose_section ---


class TestGetVerboseSection:
    """Tests for verbose mode section injection."""

    def test_verbose_active(self, prompt_env):
        # Create .koan-verbose file
        verbose_flag = Path(prompt_env["koan_root"]) / ".koan-verbose"
        verbose_flag.touch()

        result = _get_verbose_section(prompt_env["instance"])
        assert "# Verbose Mode (ACTIVE)" in result
        assert "outbox.md" in result
        assert prompt_env["instance"] in result

    def test_verbose_inactive(self, prompt_env):
        result = _get_verbose_section(prompt_env["instance"])
        assert result == ""


# --- Tests for build_agent_prompt ---


class TestBuildAgentPrompt:
    """Tests for the main agent prompt builder."""

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_mission_type_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\n# Git Merge\nStandard.\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt")
    def test_basic_mission_prompt(
        self, mock_load, mock_prefix, mock_merge, mock_type, mock_deep,
        mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        mock_load.return_value = "Template with {placeholder}"

        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=3,
            max_runs=25,
            autonomous_mode="implement",
            focus_area="Medium-cost implementation",
            available_pct=42,
            mission_title="Fix the bug",
        )

        # Verify load_prompt was called with correct args
        mock_load.assert_called_once_with(
            "agent",
            INSTANCE=prompt_env["instance"],
            PROJECT_PATH=prompt_env["project_path"],
            PROJECT_NAME="testproj",
            RUN_NUM="3",
            MAX_RUNS="25",
            AUTONOMOUS_MODE="implement",
            FOCUS_AREA="Medium-cost implementation",
            AVAILABLE_PCT="42",
            MISSION_INSTRUCTION=(
                "Your assigned mission is: **Fix the bug** "
                "The mission is already marked In Progress. "
                "Follow the Mission Execution Workflow below."
            ),
            BRANCH_PREFIX="koan/",
        )

        # Merge policy appended
        assert "Git Merge" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt")
    def test_autonomous_mode_instruction(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        mock_load.return_value = "Template"

        build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="deep",
            focus_area="Deep work",
            available_pct=60,
            mission_title="",
        )

        call_kwargs = mock_load.call_args[1]
        instruction = call_kwargs["MISSION_INSTRUCTION"]
        assert "No specific mission assigned" in instruction
        assert "testproj" in instruction
        assert "[project:testproj]" in instruction

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research", return_value="\n# Deep\nTopics\n")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_deep_mode_includes_research(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="deep",
            focus_area="Deep work",
            available_pct=60,
            mission_title="",
        )

        mock_deep.assert_called_once()
        assert "Deep" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_deep_mode_with_mission_skips_research(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        """Deep mode with assigned mission should NOT inject deep research."""
        build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="deep",
            focus_area="Execute assigned mission",
            available_pct=60,
            mission_title="Do this specific thing",
        )

        mock_deep.assert_not_called()

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_implement_mode_skips_research(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        """Non-deep modes should NOT inject deep research."""
        build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Medium-cost",
            available_pct=35,
            mission_title="",
        )

        mock_deep.assert_not_called()

    @patch("app.prompt_builder._get_verbose_section", return_value="\n# Verbose\nActive\n")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_verbose_mode_appended(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Work",
            available_pct=50,
        )

        assert "Verbose" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base prompt")
    def test_prompt_assembly_order(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        """Sections are appended in correct order: template, merge, submit-pr, deep, verbose."""
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Work",
            available_pct=50,
        )

        assert result.startswith("Base prompt")
        assert result.index("Merge") > result.index("Base prompt")

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="\n# PR\nSection\n")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_submit_pr_section_always_appended(
        self, mock_load, mock_prefix, mock_merge, mock_deep, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        """Submit-PR section should be appended for all missions."""
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Execute assigned mission",
            available_pct=50,
            mission_title="Fix the login bug",
        )

        mock_submit_pr.assert_called_once_with(prompt_env["project_path"])
        assert "PR" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_staleness_section")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_staleness_injected_in_implement_mode(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_staleness, mock_verbose, prompt_env
    ):
        """Staleness warning should be injected in implement autonomous mode."""
        mock_staleness.return_value = "\n\n# Session History Feedback\n\nWARNING\n"

        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=5,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Medium work",
            available_pct=35,
            mission_title="",
        )

        mock_staleness.assert_called_once_with(prompt_env["instance"], "testproj")
        assert "Session History Feedback" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_staleness_section")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_staleness_injected_in_review_mode(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_staleness, mock_verbose, prompt_env
    ):
        """Staleness warning should be injected in review autonomous mode."""
        mock_staleness.return_value = "\n\n# Session History Feedback\n\nCRITICAL\n"

        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=3,
            max_runs=20,
            autonomous_mode="review",
            focus_area="Low work",
            available_pct=10,
            mission_title="",
        )

        mock_staleness.assert_called_once()
        assert "CRITICAL" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_staleness_section")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_staleness_skipped_with_mission(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_staleness, mock_verbose, prompt_env
    ):
        """Staleness warning should NOT be injected when a mission is assigned."""
        build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=5,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Execute mission",
            available_pct=35,
            mission_title="Fix the auth bug",
        )

        mock_staleness.assert_not_called()

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_staleness_section")
    @patch("app.prompt_builder._get_deep_research")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_staleness_before_deep_research_in_deep_mode(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_staleness, mock_verbose, prompt_env
    ):
        """In deep mode, staleness should be injected AND deep research too."""
        mock_staleness.return_value = "\n\n# Staleness\nWarning\n"
        mock_deep.return_value = "\n\n# Deep\nResearch\n"

        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="deep",
            focus_area="Deep work",
            available_pct=60,
            mission_title="",
        )

        mock_staleness.assert_called_once()
        mock_deep.assert_called_once()
        # Both should be in the result
        assert "Staleness" in result
        assert "Deep" in result
        # Staleness appears before deep research
        assert result.index("Staleness") < result.index("Deep")


# --- Tests for build_contemplative_prompt ---


class TestBuildContemplativePrompt:
    """Tests for contemplative prompt building."""

    @patch("app.prompts.load_prompt")
    def test_basic_contemplative(self, mock_load, prompt_env):
        mock_load.return_value = "Contemplative template"

        result = build_contemplative_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            session_info="Pause mode. Run loop paused.",
        )

        mock_load.assert_called_once_with(
            "contemplative",
            INSTANCE=prompt_env["instance"],
            PROJECT_NAME="testproj",
            SESSION_INFO="Pause mode. Run loop paused.",
        )
        assert result == "Contemplative template"

    @patch("app.prompts.load_prompt")
    def test_active_mode_session_info(self, mock_load, prompt_env):
        mock_load.return_value = "Result"

        build_contemplative_prompt(
            instance=prompt_env["instance"],
            project_name="koan",
            session_info="Run 5/25 on koan. Mode: deep. Triggered by 10% contemplative chance.",
        )

        call_kwargs = mock_load.call_args[1]
        assert "Run 5/25" in call_kwargs["SESSION_INFO"]
        assert "deep" in call_kwargs["SESSION_INFO"]


# --- Tests for CLI interface ---


class TestCLI:
    """Tests for the CLI entry point."""

    @patch("app.prompt_builder.build_agent_prompt", return_value="Agent prompt output")
    def test_agent_subcommand(self, mock_build, prompt_env, capsys):
        import sys
        from app.prompt_builder import main

        with patch.object(sys, "argv", [
            "prompt_builder",
            "agent",
            "--instance", prompt_env["instance"],
            "--project-name", "testproj",
            "--project-path", prompt_env["project_path"],
            "--run-num", "3",
            "--max-runs", "25",
            "--autonomous-mode", "deep",
            "--focus-area", "Deep work",
            "--available-pct", "47",
            "--mission-title", "Fix bug",
        ]):
            main()

        mock_build.assert_called_once_with(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=3,
            max_runs=25,
            autonomous_mode="deep",
            focus_area="Deep work",
            available_pct=47,
            mission_title="Fix bug",
        )
        captured = capsys.readouterr()
        assert "Agent prompt output" in captured.out

    @patch("app.prompt_builder.build_contemplative_prompt", return_value="Contemplate output")
    def test_contemplative_subcommand(self, mock_build, prompt_env, capsys):
        import sys
        from app.prompt_builder import main

        with patch.object(sys, "argv", [
            "prompt_builder",
            "contemplative",
            "--instance", prompt_env["instance"],
            "--project-name", "koan",
            "--session-info", "Pause mode",
        ]):
            main()

        mock_build.assert_called_once_with(
            instance=prompt_env["instance"],
            project_name="koan",
            session_info="Pause mode",
        )
        captured = capsys.readouterr()
        assert "Contemplate output" in captured.out

    @patch("app.prompt_builder.build_agent_prompt", return_value="Prompt")
    def test_agent_defaults(self, mock_build, prompt_env, capsys):
        """Test that defaults are applied when optional args are omitted."""
        import sys
        from app.prompt_builder import main

        with patch.object(sys, "argv", [
            "prompt_builder",
            "agent",
            "--instance", prompt_env["instance"],
            "--project-name", "testproj",
            "--project-path", prompt_env["project_path"],
            "--run-num", "1",
            "--max-runs", "20",
        ]):
            main()

        mock_build.assert_called_once_with(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="General autonomous work",
            available_pct=50,
            mission_title="",
        )


# --- Integration-style tests ---


class TestIntegration:
    """Tests that verify the full prompt assembly with real templates."""

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=False)
    @patch("app.prompt_builder._get_deep_research", return_value="")
    def test_full_agent_prompt_with_real_template(
        self, mock_deep, mock_merge, mock_verbose, prompt_env
    ):
        """Build a full prompt using the real agent.md template."""
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=5,
            max_runs=25,
            autonomous_mode="implement",
            focus_area="Quick wins",
            available_pct=35,
            mission_title="Fix auth bug",
        )

        # Verify placeholders were substituted
        assert "{INSTANCE}" not in result
        assert "{PROJECT_NAME}" not in result
        assert "{PROJECT_PATH}" not in result
        assert "{RUN_NUM}" not in result
        assert "{MAX_RUNS}" not in result
        assert "{AUTONOMOUS_MODE}" not in result
        assert "{FOCUS_AREA}" not in result
        assert "{AVAILABLE_PCT}" not in result
        assert "{MISSION_INSTRUCTION}" not in result
        assert "{BRANCH_PREFIX}" not in result

        # Verify substituted values are present
        assert prompt_env["instance"] in result
        assert "testproj" in result
        assert prompt_env["project_path"] in result
        assert "implement" in result
        assert "Quick wins" in result
        assert "35" in result
        assert "Fix auth bug" in result

        # Verify merge policy was appended
        assert "NOT configured" in result

        # Verify new prompt structure
        assert "Mission Execution Workflow" in result
        assert "Pull Request Quality" in result
        assert "Mission Completion Checklist" in result

        # Verify old misleading instruction is gone
        assert "Mark it In Progress in missions.md" not in result
        assert "already marked In Progress" in result

        # Verify no hardcoded names in generic prompt
        assert "Alexis" not in result

        # Verify submit-PR section is always included
        assert "Audit Missions" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=False)
    @patch("app.prompt_builder._get_deep_research", return_value="")
    def test_full_autonomous_prompt(
        self, mock_deep, mock_merge, mock_verbose, prompt_env
    ):
        """Build autonomous mode prompt — no mission title."""
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="koan",
            project_path=prompt_env["project_path"],
            run_num=10,
            max_runs=50,
            autonomous_mode="deep",
            focus_area="High-cost deep work",
            available_pct=47,
        )

        assert "No specific mission assigned" in result
        assert "[project:koan]" in result
        assert "deep" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=False)
    @patch("app.prompt_builder._get_deep_research", return_value="")
    def test_submit_pr_section_always_included(
        self, mock_deep, mock_merge, mock_verbose, prompt_env
    ):
        """Submit-PR section should be included for all missions via real template."""
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Execute assigned mission",
            available_pct=50,
            mission_title="Fix the login page CSS",
        )

        assert "Audit Missions" in result
        assert "GitHub Issue Follow-up" in result
        assert "gh issue create" in result

    def test_full_contemplative_prompt(self, prompt_env):
        """Build a full contemplative prompt using the real template."""
        result = build_contemplative_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            session_info="Run 3/25 on testproj. Mode: deep.",
        )

        # Verify placeholders were substituted
        assert "{INSTANCE}" not in result
        assert "{PROJECT_NAME}" not in result
        assert "{SESSION_INFO}" not in result

        # Verify content
        assert prompt_env["instance"] in result
        assert "testproj" in result
        assert "Run 3/25" in result
        assert "contemplative" in result.lower()


# --- Tests for _get_deep_research error handling ---


class TestGetDeepResearchErrors:
    """Additional error path tests for _get_deep_research."""

    @patch("app.deep_research.DeepResearch")
    def test_format_for_agent_returns_none(self, mock_cls, prompt_env):
        """format_for_agent returning None should not crash."""
        mock_instance = MagicMock()
        mock_instance.format_for_agent.return_value = None
        mock_cls.return_value = mock_instance

        result = _get_deep_research(
            prompt_env["instance"],
            prompt_env["project_name"],
            prompt_env["project_path"],
        )
        assert result == ""

    @patch("app.deep_research.DeepResearch")
    def test_format_for_agent_raises(self, mock_cls, prompt_env, capsys):
        """Exception from format_for_agent is caught and logged."""
        mock_instance = MagicMock()
        mock_instance.format_for_agent.side_effect = RuntimeError("API timeout")
        mock_cls.return_value = mock_instance

        result = _get_deep_research(
            prompt_env["instance"],
            prompt_env["project_name"],
            prompt_env["project_path"],
        )
        assert result == ""
        captured = capsys.readouterr()
        assert "Deep research failed" in captured.err


# --- Tests for _get_staleness_section error handling ---


class TestGetStalenessSectionErrors:
    """Additional error path tests for _get_staleness_section."""

    @patch("app.session_tracker.get_staleness_warning", return_value=None)
    def test_none_warning_returns_empty(self, mock_warning, prompt_env):
        """None (as opposed to empty string) returns empty section."""
        result = _get_staleness_section(prompt_env["instance"], "testproj")
        assert result == ""

    @patch("app.session_tracker.get_staleness_warning", side_effect=ImportError("no module"))
    def test_import_error_returns_empty(self, mock_warning, prompt_env, capsys):
        """ImportError (module missing) is caught gracefully."""
        result = _get_staleness_section(prompt_env["instance"], "testproj")
        assert result == ""
        captured = capsys.readouterr()
        assert "Staleness check failed" in captured.err


# --- Tests for _get_focus_section ---


class TestGetFocusSection:
    """Tests for focus mode section injection."""

    @patch("app.focus_manager.check_focus", return_value=None)
    def test_no_focus_returns_empty(self, mock_check, prompt_env):
        from app.prompt_builder import _get_focus_section

        result = _get_focus_section(prompt_env["instance"])
        assert result == ""

    @patch("app.focus_manager.check_focus")
    def test_active_focus_includes_remaining(self, mock_check, prompt_env):
        from app.prompt_builder import _get_focus_section

        mock_state = MagicMock()
        mock_state.remaining_display.return_value = "2h 30m"
        mock_check.return_value = mock_state

        result = _get_focus_section(prompt_env["instance"])
        assert "2h 30m" in result

    @patch("app.focus_manager.check_focus", side_effect=Exception("broken"))
    def test_error_returns_empty(self, mock_check, prompt_env, capsys):
        from app.prompt_builder import _get_focus_section

        result = _get_focus_section(prompt_env["instance"])
        assert result == ""
        captured = capsys.readouterr()
        assert "Focus check failed" in captured.err


# --- Tests for _load_config_safe ---


class TestLoadConfigSafe:
    """Tests for config loading safety wrapper."""

    @patch("app.utils.load_config", return_value={"key": "value"})
    def test_returns_config(self, mock_load):
        from app.prompt_builder import _load_config_safe

        result = _load_config_safe()
        assert result == {"key": "value"}

    @patch("app.utils.load_config", side_effect=FileNotFoundError("no file"))
    def test_returns_empty_on_error(self, mock_load):
        from app.prompt_builder import _load_config_safe

        result = _load_config_safe()
        assert result == {}


# --- Tests for _get_branch_prefix ---


class TestGetBranchPrefix:
    """Tests for branch prefix retrieval."""

    @patch("app.config.get_branch_prefix", return_value="custom/")
    def test_returns_configured_prefix(self, mock_prefix):
        from app.prompt_builder import _get_branch_prefix

        assert _get_branch_prefix() == "custom/"

    @patch("app.config.get_branch_prefix", side_effect=OSError("no config"))
    def test_returns_default_on_error(self, mock_prefix):
        from app.prompt_builder import _get_branch_prefix

        assert _get_branch_prefix() == "koan/"


# --- Tests for build_agent_prompt section ordering ---


class TestBuildAgentPromptSections:
    """Test that sections are appended in correct order."""

    @patch("app.prompt_builder._get_verbose_section", return_value="\n# Verbose\n")
    @patch("app.prompt_builder._get_focus_section", return_value="\n# Focus\n")
    @patch("app.prompt_builder._get_staleness_section", return_value="\n# Stale\n")
    @patch("app.prompt_builder._get_deep_research", return_value="\n# Deep\n")
    @patch("app.prompt_builder._get_merge_policy", return_value="\n# Merge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="BASE")
    def test_deep_autonomous_includes_all_sections(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_stale, mock_focus, mock_verbose, prompt_env
    ):
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=25,
            autonomous_mode="deep",
            focus_area="test",
            available_pct=80,
            mission_title="",  # autonomous
        )

        # All sections should be present
        assert "BASE" in result
        assert "# Merge" in result
        assert "# Stale" in result
        assert "# Deep" in result
        assert "# Focus" in result
        assert "# Verbose" in result

        # Sections in correct order: base, merge, stale, deep, focus, verbose
        merge_idx = result.index("# Merge")
        stale_idx = result.index("# Stale")
        deep_idx = result.index("# Deep")
        focus_idx = result.index("# Focus")
        verbose_idx = result.index("# Verbose")
        assert merge_idx < stale_idx < deep_idx < focus_idx < verbose_idx

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_focus_section", return_value="")
    @patch("app.prompt_builder._get_staleness_section", return_value="\n# Stale\n")
    @patch("app.prompt_builder._get_deep_research", return_value="\n# Deep\n")
    @patch("app.prompt_builder._get_merge_policy", return_value="\n# Merge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="BASE")
    def test_mission_excludes_staleness_and_deep(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_stale, mock_focus, mock_verbose, prompt_env
    ):
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=25,
            autonomous_mode="deep",
            focus_area="test",
            available_pct=80,
            mission_title="Fix bug",  # has mission
        )

        # Staleness and deep should NOT be called for missions
        mock_stale.assert_not_called()
        mock_deep.assert_not_called()

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_focus_section", return_value="")
    @patch("app.prompt_builder._get_staleness_section", return_value="\n# Stale\n")
    @patch("app.prompt_builder._get_deep_research", return_value="\n# Deep\n")
    @patch("app.prompt_builder._get_merge_policy", return_value="\n# Merge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="BASE")
    def test_implement_mode_excludes_deep_research(
        self, mock_load, mock_prefix, mock_merge, mock_deep,
        mock_stale, mock_focus, mock_verbose, prompt_env
    ):
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=25,
            autonomous_mode="implement",
            focus_area="test",
            available_pct=50,
            mission_title="",  # autonomous
        )

        # Deep research only in deep mode
        mock_deep.assert_not_called()
        # Staleness should be included for autonomous
        mock_stale.assert_called_once()


# --- Tests for _get_tdd_section ---


class TestGetTddSection:
    """Tests for TDD mode prompt injection."""

    def test_tdd_tag_injects_prompt(self):
        """Mission tagged [tdd] should inject TDD prompt section."""
        result = _get_tdd_section("[tdd] Add user validation")
        assert "TDD Mode" in result
        assert "Red-Green-Refactor" in result

    def test_no_tdd_tag_returns_empty(self):
        """Mission without [tdd] tag should return empty string."""
        result = _get_tdd_section("Add user validation")
        assert result == ""

    def test_empty_mission_returns_empty(self):
        """Empty mission title should return empty string."""
        assert _get_tdd_section("") == ""

    def test_tdd_tag_case_insensitive(self):
        """[TDD] should also trigger injection."""
        result = _get_tdd_section("[TDD] Add tests")
        assert "TDD Mode" in result

    def test_tdd_with_project_tag(self):
        """[tdd] alongside [project:X] should still inject."""
        result = _get_tdd_section("[tdd] [project:koan] Fix bug")
        assert "TDD Mode" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt")
    def test_build_agent_prompt_includes_tdd(
        self, mock_load, mock_prefix, mock_merge, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        """build_agent_prompt should include TDD section when mission is tagged."""
        mock_load.side_effect = lambda name, **kw: (
            "Base prompt" if name == "agent" else
            "TDD Mode — Red-Green-Refactor" if name == "tdd-mode" else
            ""
        )

        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Test area",
            available_pct=50,
            mission_title="[tdd] Add user validation",
        )

        assert "TDD Mode" in result

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt", return_value="Base prompt")
    def test_build_agent_prompt_no_tdd_without_tag(
        self, mock_load, mock_prefix, mock_merge, mock_submit_pr, mock_verbose,
        prompt_env,
    ):
        """build_agent_prompt should NOT include TDD section without tag."""
        result = build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Test area",
            available_pct=50,
            mission_title="Add user validation",
        )

        assert "TDD Mode" not in result


# --- Tests for _get_mission_type_section ---


class TestGetMissionTypeSection:
    """Tests for mission-type-aware prompt injection."""

    def test_returns_empty_for_no_mission(self):
        assert _get_mission_type_section("") == ""

    @patch("app.prompts.load_prompt")
    def test_returns_empty_for_general_type(self, mock_load):
        """No injection for unclassified missions."""
        assert _get_mission_type_section("migrate to Python 3.12") == ""
        mock_load.assert_not_called()

    @patch("app.prompts.load_prompt")
    def test_returns_debug_hint(self, mock_load):
        mock_load.return_value = (
            "## debug\n\nReproduce the bug first.\n\n## implement\n\nBuild it.\n"
        )
        result = _get_mission_type_section("fix auth token refresh")
        assert "Mission Approach Guidance" in result
        assert "**debug**" in result
        assert "Reproduce the bug first." in result

    @patch("app.prompts.load_prompt")
    def test_returns_implement_hint(self, mock_load):
        mock_load.return_value = (
            "## debug\n\nReproduce the bug first.\n\n"
            "## implement\n\nBuild incrementally.\n"
        )
        result = _get_mission_type_section("add webhook support")
        assert "**implement**" in result
        assert "Build incrementally." in result

    @patch("app.prompts.load_prompt")
    def test_graceful_on_exception(self, mock_load):
        """Classification failure should not crash."""
        mock_load.side_effect = FileNotFoundError("missing")
        result = _get_mission_type_section("fix something")
        assert result == ""

    @patch("app.prompt_builder._get_mission_type_section")
    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\n# Policy\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt")
    def test_integration_mission_type_called_with_title(
        self, mock_load, mock_prefix, mock_merge, mock_submit,
        mock_verbose, mock_type_section, prompt_env,
    ):
        """build_agent_prompt calls _get_mission_type_section with title."""
        mock_load.return_value = "TEMPLATE"
        mock_type_section.return_value = ""
        build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Test area",
            available_pct=50,
            mission_title="fix a bug",
        )
        mock_type_section.assert_called_once_with("fix a bug")

    @patch("app.prompt_builder._get_mission_type_section")
    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\n# Policy\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompts.load_prompt")
    def test_integration_mission_type_called_without_title(
        self, mock_load, mock_prefix, mock_merge, mock_submit,
        mock_verbose, mock_type_section, prompt_env,
    ):
        """build_agent_prompt calls _get_mission_type_section with empty string."""
        mock_load.return_value = "TEMPLATE"
        mock_type_section.return_value = ""
        build_agent_prompt(
            instance=prompt_env["instance"],
            project_name="testproj",
            project_path=prompt_env["project_path"],
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Test area",
            available_pct=50,
        )
        mock_type_section.assert_called_once_with("")
