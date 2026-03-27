"""Tests for app.contemplative_runner — contemplative session management."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from app.contemplative_runner import (
    build_contemplative_command,
    get_contemplative_flags,
    main,
    run_contemplative_session,
    should_run_contemplative,
)


# --- should_run_contemplative ---


class TestShouldRunContemplative:
    """Tests for probability roll logic."""

    def test_zero_chance_never_runs(self):
        """0% chance should never trigger."""
        for _ in range(100):
            assert should_run_contemplative(0) is False

    def test_hundred_chance_always_runs(self):
        """100% chance should always trigger."""
        for _ in range(100):
            assert should_run_contemplative(100) is True

    def test_negative_chance_never_runs(self):
        """Negative chance should never trigger."""
        assert should_run_contemplative(-10) is False

    def test_over_hundred_always_runs(self):
        """>100% chance should always trigger."""
        assert should_run_contemplative(200) is True

    @patch("app.contemplative_runner.random.randint")
    def test_fifty_chance_rolls_under(self, mock_randint):
        """50% chance with roll=30 should trigger."""
        mock_randint.return_value = 30
        assert should_run_contemplative(50) is True

    @patch("app.contemplative_runner.random.randint")
    def test_fifty_chance_rolls_over(self, mock_randint):
        """50% chance with roll=70 should not trigger."""
        mock_randint.return_value = 70
        assert should_run_contemplative(50) is False

    @patch("app.contemplative_runner.random.randint")
    def test_boundary_exact_match(self, mock_randint):
        """Roll exactly equal to chance should NOT trigger (< not <=)."""
        mock_randint.return_value = 50
        assert should_run_contemplative(50) is False

    @patch("app.contemplative_runner.random.randint")
    def test_boundary_one_below(self, mock_randint):
        """Roll one below chance should trigger."""
        mock_randint.return_value = 49
        assert should_run_contemplative(50) is True


# --- build_contemplative_command ---


class TestBuildContemplativeCommand:
    """Tests for CLI command construction."""

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_basic_command(self, mock_prompt, mock_tools):
        """Produces correct base command structure."""
        mock_prompt.return_value = "test prompt"
        mock_tools.return_value = "Read,Write,Glob,Grep"
        cmd = build_contemplative_command(
            instance="/path/instance",
            project_name="koan",
            session_info="test session",
        )
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "test prompt"
        assert "--allowedTools" in cmd
        assert "Read,Write,Glob,Grep" in cmd
        assert "--max-turns" in cmd
        assert "10" in cmd

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_custom_tools_from_config(self, mock_prompt, mock_tools):
        """Tools are read from config, not hardcoded."""
        mock_prompt.return_value = "test prompt"
        mock_tools.return_value = "Read,Glob,Grep,Bash"
        cmd = build_contemplative_command(
            instance="/path/instance",
            project_name="koan",
            session_info="test session",
        )
        assert "--allowedTools" in cmd
        assert "Read,Glob,Grep,Bash" in cmd

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_max_turns_is_10(self, mock_prompt, mock_tools):
        """Regression: max_turns=5 was too low for contemplative prompts.

        The contemplative prompt requires reading 5 files (soul.md, summary.md,
        emotional-memory.md, personality-evolution.md, learnings.md) plus writing
        output — 6-7 tool calls minimum. max_turns=5 caused ~40% of contemplative
        sessions to hit the limit before producing any output.
        """
        mock_prompt.return_value = "test prompt"
        mock_tools.return_value = "Read,Write,Glob,Grep"
        cmd = build_contemplative_command(
            instance="/path/instance",
            project_name="koan",
            session_info="test session",
        )
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "10"

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_passes_args_to_prompt_builder(self, mock_prompt, mock_tools):
        """Forwards instance, project_name, session_info to prompt builder."""
        mock_prompt.return_value = "prompt"
        mock_tools.return_value = "Read,Write,Glob,Grep"
        build_contemplative_command(
            instance="/my/instance",
            project_name="myproject",
            session_info="my info",
        )
        mock_prompt.assert_called_once_with(
            instance="/my/instance",
            project_name="myproject",
            session_info="my info",
        )

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_forwards_project_name_to_get_tools(self, mock_prompt, mock_tools):
        """project_name is forwarded to get_contemplative_tools for per-project overrides."""
        mock_prompt.return_value = "prompt"
        mock_tools.return_value = "Read,Write"
        build_contemplative_command(
            instance="/path",
            project_name="myproject",
            session_info="info",
        )
        mock_tools.assert_called_once_with(project_name="myproject")

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_extra_flags_appended(self, mock_prompt, mock_tools):
        """Extra flags are appended to the command."""
        mock_prompt.return_value = "prompt"
        mock_tools.return_value = "Read,Write,Glob,Grep"
        cmd = build_contemplative_command(
            instance="/path",
            project_name="koan",
            session_info="info",
            extra_flags=["--model", "claude-sonnet-4-5-20250929"],
        )
        assert "--model" in cmd
        assert "claude-sonnet-4-5-20250929" in cmd

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_no_extra_flags(self, mock_prompt, mock_tools):
        """Without extra flags, command is base only."""
        mock_prompt.return_value = "prompt"
        mock_tools.return_value = "Read,Write,Glob,Grep"
        cmd = build_contemplative_command(
            instance="/path",
            project_name="koan",
            session_info="info",
        )
        assert "--model" not in cmd
        assert "--fallback-model" not in cmd

    @patch("app.config.get_contemplative_tools")
    @patch("app.prompt_builder.build_contemplative_prompt")
    def test_none_extra_flags(self, mock_prompt, mock_tools):
        """None extra_flags treated same as no flags."""
        mock_prompt.return_value = "prompt"
        mock_tools.return_value = "Read,Write,Glob,Grep"
        cmd = build_contemplative_command(
            instance="/path",
            project_name="koan",
            session_info="info",
            extra_flags=None,
        )
        # Should not raise and should not include model flags
        assert cmd[0] == "claude"


# --- get_contemplative_flags ---


class TestGetContemplativeFlags:
    """Tests for flag retrieval from config."""

    @patch("app.config.get_claude_flags_for_role")
    def test_returns_split_flags(self, mock_flags):
        """Non-empty flags string is split into list."""
        mock_flags.return_value = "--model claude-sonnet-4-5-20250929"
        result = get_contemplative_flags()
        assert result == ["--model", "claude-sonnet-4-5-20250929"]
        mock_flags.assert_called_once_with("contemplative")

    @patch("app.config.get_claude_flags_for_role")
    def test_empty_flags(self, mock_flags):
        """Empty flags string returns empty list."""
        mock_flags.return_value = ""
        result = get_contemplative_flags()
        assert result == []

    @patch("app.config.get_claude_flags_for_role")
    def test_whitespace_only(self, mock_flags):
        """Whitespace-only string returns empty list."""
        mock_flags.return_value = "   "
        result = get_contemplative_flags()
        assert result == []

    @patch("app.config.get_claude_flags_for_role")
    def test_multiple_flags(self, mock_flags):
        """Multiple flags are properly split."""
        mock_flags.return_value = "--model sonnet --fallback-model haiku"
        result = get_contemplative_flags()
        assert result == ["--model", "sonnet", "--fallback-model", "haiku"]


# --- run_contemplative_session ---


class TestRunContemplativeSession:
    """Tests for the full session runner."""

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_success(self, mock_cmd, mock_flags, mock_run_claude):
        """Successful session forwards run_claude result."""
        mock_flags.return_value = []
        mock_cmd.return_value = ["claude", "-p", "prompt"]
        mock_run_claude.return_value = {
            "success": True, "output": "session output", "error": ""
        }

        result = run_contemplative_session(
            instance="/path/instance",
            project_name="koan",
            session_info="test",
        )

        assert result["success"] is True
        assert result["output"] == "session output"
        mock_run_claude.assert_called_once_with(
            ["claude", "-p", "prompt"], cwd="/path/instance", timeout=300
        )

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_failure_forwarded(self, mock_cmd, mock_flags, mock_run_claude):
        """Failure result from run_claude is forwarded as-is."""
        mock_flags.return_value = []
        mock_cmd.return_value = ["claude", "-p", "prompt"]
        mock_run_claude.return_value = {
            "success": False, "output": "", "error": "Exit code 1: some error"
        }

        result = run_contemplative_session(
            instance="/path",
            project_name="koan",
            session_info="test",
        )

        assert result["success"] is False
        assert "some error" in result["error"]

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_custom_cwd(self, mock_cmd, mock_flags, mock_run_claude):
        """Custom cwd overrides instance path."""
        mock_flags.return_value = []
        mock_cmd.return_value = ["claude", "-p", "prompt"]
        mock_run_claude.return_value = {
            "success": True, "output": "", "error": ""
        }

        run_contemplative_session(
            instance="/path/instance",
            project_name="koan",
            session_info="test",
            cwd="/other/dir",
        )

        assert mock_run_claude.call_args.kwargs["cwd"] == "/other/dir"

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_custom_timeout(self, mock_cmd, mock_flags, mock_run_claude):
        """Custom timeout is passed to run_claude."""
        mock_flags.return_value = []
        mock_cmd.return_value = ["claude", "-p", "prompt"]
        mock_run_claude.return_value = {
            "success": True, "output": "", "error": ""
        }

        run_contemplative_session(
            instance="/path",
            project_name="koan",
            session_info="test",
            timeout=600,
        )

        assert mock_run_claude.call_args.kwargs["timeout"] == 600

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_flags_passed_to_build(self, mock_cmd, mock_flags, mock_run_claude):
        """Flags from config are passed to build_contemplative_command."""
        mock_flags.return_value = ["--model", "sonnet"]
        mock_cmd.return_value = ["claude", "-p", "prompt", "--model", "sonnet"]
        mock_run_claude.return_value = {
            "success": True, "output": "", "error": ""
        }

        run_contemplative_session(
            instance="/path",
            project_name="koan",
            session_info="test",
        )

        mock_cmd.assert_called_once_with(
            instance="/path",
            project_name="koan",
            session_info="test",
            extra_flags=["--model", "sonnet"],
        )


# --- CLI interface ---


class TestCLIShouldRun:
    """Tests for the should-run CLI subcommand."""

    @patch("app.contemplative_runner.should_run_contemplative")
    def test_should_run_exits_0(self, mock_roll):
        """Exit code 0 when should run."""
        mock_roll.return_value = True
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["should-run", "50"])
        assert exc_info.value.code == 0

    @patch("app.contemplative_runner.should_run_contemplative")
    def test_should_not_run_exits_1(self, mock_roll):
        """Exit code 1 when should not run."""
        mock_roll.return_value = False
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["should-run", "50"])
        assert exc_info.value.code == 1

    def test_should_run_missing_arg(self):
        """Missing chance argument exits with error."""
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["should-run"])
        assert exc_info.value.code == 1

    def test_should_run_invalid_chance(self):
        """Non-integer chance argument exits with error."""
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["should-run", "abc"])
        assert exc_info.value.code == 1

    @patch("app.contemplative_runner.should_run_contemplative")
    def test_should_run_parses_int(self, mock_roll):
        """Chance argument is parsed as integer."""
        mock_roll.return_value = True
        with pytest.raises(SystemExit):
            main_with_args(["should-run", "75"])
        mock_roll.assert_called_once_with(75)


class TestCLIRun:
    """Tests for the run CLI subcommand."""

    @patch("app.contemplative_runner.run_contemplative_session")
    def test_run_success(self, mock_session):
        """Successful run prints output and exits 0."""
        mock_session.return_value = {
            "success": True,
            "output": "contemplation result",
            "error": "",
        }
        # Should not raise SystemExit (exits normally)
        main_with_args([
            "run",
            "--instance", "/path/instance",
            "--project-name", "koan",
            "--session-info", "test session",
        ])
        mock_session.assert_called_once_with(
            instance="/path/instance",
            project_name="koan",
            session_info="test session",
            timeout=300,
        )

    @patch("app.contemplative_runner.run_contemplative_session")
    def test_run_failure_exits_1(self, mock_session):
        """Failed run exits with code 1."""
        mock_session.return_value = {
            "success": False,
            "output": "",
            "error": "timeout",
        }
        with pytest.raises(SystemExit) as exc_info:
            main_with_args([
                "run",
                "--instance", "/path",
                "--project-name", "koan",
                "--session-info", "test",
            ])
        assert exc_info.value.code == 1

    @patch("app.contemplative_runner.run_contemplative_session")
    def test_run_custom_timeout(self, mock_session):
        """Custom timeout is forwarded to session runner."""
        mock_session.return_value = {
            "success": True,
            "output": "",
            "error": "",
        }
        main_with_args([
            "run",
            "--instance", "/path",
            "--project-name", "koan",
            "--session-info", "test",
            "--timeout", "600",
        ])
        assert mock_session.call_args.kwargs["timeout"] == 600


class TestCLIMain:
    """Tests for the main() entry point."""

    def test_no_args_exits(self):
        """No arguments shows usage and exits."""
        with pytest.raises(SystemExit) as exc_info:
            main_with_args([])
        assert exc_info.value.code == 1

    def test_unknown_subcommand_exits(self):
        """Unknown subcommand exits with error."""
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["bogus"])
        assert exc_info.value.code == 1


# --- Additional edge case tests ---


class TestShouldRunContemplativeEdgeCases:
    """Additional boundary tests for probability roll."""

    @patch("app.contemplative_runner.random.randint")
    def test_chance_99_roll_98_triggers(self, mock_randint):
        """99% chance with roll=98 should trigger (98 < 99)."""
        mock_randint.return_value = 98
        assert should_run_contemplative(99) is True

    @patch("app.contemplative_runner.random.randint")
    def test_chance_99_roll_99_no_trigger(self, mock_randint):
        """99% chance with roll=99 should NOT trigger (99 < 99 is False)."""
        mock_randint.return_value = 99
        assert should_run_contemplative(99) is False

    @patch("app.contemplative_runner.random.randint")
    def test_chance_1_roll_0_triggers(self, mock_randint):
        """1% chance with roll=0 should trigger (0 < 1)."""
        mock_randint.return_value = 0
        assert should_run_contemplative(1) is True

    @patch("app.contemplative_runner.random.randint")
    def test_chance_1_roll_1_no_trigger(self, mock_randint):
        """1% chance with roll=1 should NOT trigger (1 < 1 is False)."""
        mock_randint.return_value = 1
        assert should_run_contemplative(1) is False


class TestRunContemplativeSessionEdgeCases:
    """Additional edge case tests for the session runner."""

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_default_cwd_is_instance(self, mock_cmd, mock_flags, mock_run_claude):
        """When cwd is not provided, instance path is used as working directory."""
        mock_flags.return_value = []
        mock_cmd.return_value = ["claude", "-p", "prompt"]
        mock_run_claude.return_value = {
            "success": True, "output": "", "error": ""
        }

        run_contemplative_session(
            instance="/path/to/instance",
            project_name="koan",
            session_info="test",
        )

        assert mock_run_claude.call_args.kwargs["cwd"] == "/path/to/instance"

    @patch("app.claude_step.run_claude")
    @patch("app.contemplative_runner.get_contemplative_flags")
    @patch("app.contemplative_runner.build_contemplative_command")
    def test_default_timeout_is_300(self, mock_cmd, mock_flags, mock_run_claude):
        """Default timeout should be 300 seconds."""
        mock_flags.return_value = []
        mock_cmd.return_value = ["claude", "-p", "prompt"]
        mock_run_claude.return_value = {
            "success": True, "output": "", "error": ""
        }

        run_contemplative_session(
            instance="/path",
            project_name="koan",
            session_info="test",
        )

        assert mock_run_claude.call_args.kwargs["timeout"] == 300


class TestCLIShouldRunEdgeCases:
    """Additional CLI edge case tests."""

    @patch("app.contemplative_runner.should_run_contemplative")
    def test_zero_chance_parsed(self, mock_roll):
        """'0' argument is parsed correctly."""
        mock_roll.return_value = False
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["should-run", "0"])
        assert exc_info.value.code == 1
        mock_roll.assert_called_once_with(0)

    def test_float_chance_rejected(self):
        """Float chance argument exits with error."""
        with pytest.raises(SystemExit) as exc_info:
            main_with_args(["should-run", "3.14"])
        assert exc_info.value.code == 1


class TestCLIRunEdgeCases:
    """Additional CLI run subcommand edge cases."""

    @patch("app.contemplative_runner.run_contemplative_session")
    def test_run_no_output_no_crash(self, mock_session):
        """Empty output and empty error should not crash."""
        mock_session.return_value = {
            "success": True,
            "output": "",
            "error": "",
        }
        # Should not raise
        main_with_args([
            "run",
            "--instance", "/path",
            "--project-name", "koan",
            "--session-info", "test",
        ])

    @patch("app.contemplative_runner.run_contemplative_session")
    def test_run_default_timeout_300(self, mock_session):
        """CLI default timeout should be 300."""
        mock_session.return_value = {
            "success": True, "output": "", "error": ""
        }
        main_with_args([
            "run",
            "--instance", "/path",
            "--project-name", "koan",
            "--session-info", "test",
        ])
        assert mock_session.call_args.kwargs["timeout"] == 300


# --- Helpers ---


def main_with_args(args: list):
    """Run main() with custom sys.argv."""
    with patch.object(sys, "argv", ["contemplative_runner"] + args):
        main()
