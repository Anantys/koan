"""Tests for app.ai_runner — AI exploration CLI runner."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.ai_runner import (
    run_exploration,
    _clean_response,
    _extract_missions,
    _strip_mission_lines,
    _queue_missions,
    main,
)


# ---------------------------------------------------------------------------
# _clean_response (delegates to text_utils.clean_cli_response)
# ---------------------------------------------------------------------------

class TestCleanResponse:
    def test_strips_markdown_decorators(self):
        text = "### Header\n**bold** and __underline__"
        cleaned = _clean_response(text)
        assert "###" not in cleaned
        assert "**" not in cleaned
        assert "__" not in cleaned

    def test_strips_code_fences(self):
        text = "```python\nprint('hello')\n```"
        cleaned = _clean_response(text)
        assert "```" not in cleaned

    def test_strips_max_turns_error(self):
        text = "Error: max turns reached\nGood content here"
        cleaned = _clean_response(text)
        assert "max turns" not in cleaned
        assert "Good content" in cleaned

    def test_truncates_long_output(self):
        text = "x" * 3000
        cleaned = _clean_response(text)
        assert len(cleaned) <= 2000
        assert cleaned.endswith("...")

    def test_preserves_short_output(self):
        text = "Short and sweet"
        cleaned = _clean_response(text)
        assert cleaned == "Short and sweet"


# ---------------------------------------------------------------------------
# run_command (provider-level helper, tested via ai_runner integration)
# ---------------------------------------------------------------------------

class TestRunCommand:
    """Tests for the shared run_command helper in app.provider."""

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_returns_stdout_on_success(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Exploration results", stderr=""
        )
        result = run_command("test prompt", "/tmp", allowed_tools=["Read"])
        assert result == "Exploration results"

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_raises_on_failure(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="quota exceeded"
        )
        with pytest.raises(RuntimeError, match="CLI invocation failed"):
            run_command("test prompt", "/tmp", allowed_tools=["Read"])

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_passes_allowed_tools(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        run_command("test", "/tmp", allowed_tools=["Read", "Glob", "Grep", "Bash"])
        call_kwargs = mock_cmd.call_args[1]
        assert "Read" in call_kwargs["allowed_tools"]
        assert "Bash" in call_kwargs["allowed_tools"]

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_passes_max_turns(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        run_command("test", "/tmp", allowed_tools=["Read"], max_turns=5)
        call_kwargs = mock_cmd.call_args[1]
        assert call_kwargs["max_turns"] == 5

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_sets_cwd_to_project_path(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        run_command("test", "/my/project", allowed_tools=["Read"])
        assert mock_run.call_args[1]["cwd"] == "/my/project"

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_strips_max_turns_error_from_output(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Error: Reached max turns (1)",
            stderr="",
        )
        result = run_command("test", "/tmp", allowed_tools=[])
        assert result == ""

    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": ""})
    @patch("app.provider.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.provider.subprocess.run")
    def test_strips_max_turns_preserves_real_content(self, mock_run, mock_cmd, mock_model):
        from app.cli_provider import run_command
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Real output here\nError: Reached max turns (5)\n",
            stderr="",
        )
        result = run_command("test", "/tmp", allowed_tools=[])
        assert result == "Real output here"


# ---------------------------------------------------------------------------
# run_exploration
# ---------------------------------------------------------------------------

class TestRunExploration:
    @patch("app.cli_provider.run_command_streaming", return_value="Found 3 issues")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_success_returns_true(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        notify = MagicMock()
        success, summary = run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        assert success is True
        assert "completed" in summary.lower()

    @patch("app.cli_provider.run_command_streaming", return_value="Found 3 issues")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_notifies_start_and_result(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        notify = MagicMock()
        run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        assert notify.call_count == 2
        # First call: "Exploring myapp..."
        assert "Exploring" in notify.call_args_list[0][0][0]
        # Second call: exploration result
        assert "myapp" in notify.call_args_list[1][0][0]

    @patch("app.cli_provider.run_command_streaming", side_effect=RuntimeError("quota exceeded"))
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_failure_returns_false(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        notify = MagicMock()
        success, summary = run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        assert success is False
        assert "failed" in summary.lower()

    @patch("app.cli_provider.run_command_streaming", return_value="")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_empty_result_returns_false(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        notify = MagicMock()
        success, summary = run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        assert success is False
        assert "empty" in summary.lower()

    @patch("app.cli_provider.run_command_streaming", return_value="Found 3 issues")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_loads_prompt_from_skill_dir(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        notify = MagicMock()
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify, skill_dir=custom_dir,
        )
        assert mock_prompt.call_args[0][0] == custom_dir
        assert mock_prompt.call_args[0][1] == "ai-explore"

    @patch("app.cli_provider.run_command_streaming", return_value="Found 3 issues")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_prompt_substitutions(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        """Prompt should receive PROJECT_NAME, GIT_ACTIVITY, etc."""
        notify = MagicMock()
        run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        kwargs = mock_prompt.call_args[1]
        assert kwargs["PROJECT_NAME"] == "myapp"
        assert "GIT_ACTIVITY" in kwargs
        assert "PROJECT_STRUCTURE" in kwargs
        assert "MISSIONS_CONTEXT" in kwargs

    @patch("app.cli_provider.run_command_streaming", return_value="x" * 3000)
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_truncates_telegram_output(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        notify = MagicMock()
        run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        result_msg = notify.call_args_list[1][0][0]
        assert len(result_msg) <= 2100  # header + 2000 content

    @patch("app.cli_provider.run_command_streaming", return_value="Found issues")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_max_turns_is_10(
        self, mock_prompt, mock_git, mock_struct, mock_missions, mock_claude,
        tmp_path
    ):
        """Regression: max_turns=5 was too low for AI exploration sessions.

        AI exploration needs enough turns to read project context and produce
        meaningful output. max_turns=5 caused frequent early termination.
        """
        notify = MagicMock()
        run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["max_turns"] == 10


# ---------------------------------------------------------------------------
# _extract_missions
# ---------------------------------------------------------------------------

class TestExtractMissions:
    def test_extracts_mission_lines(self):
        text = (
            "Found some issues:\n"
            "MISSION: Fix the retry logic in fetch_data()\n"
            "MISSION: Add input validation for user email\n"
            "Some other text\n"
        )
        missions = _extract_missions(text, "myapp")
        assert len(missions) == 2
        assert missions[0] == "- [project:myapp] Fix the retry logic in fetch_data()"
        assert missions[1] == "- [project:myapp] Add input validation for user email"

    def test_no_mission_lines(self):
        text = "No issues found. Everything looks good."
        missions = _extract_missions(text, "myapp")
        assert missions == []

    def test_ignores_empty_mission_lines(self):
        text = "MISSION: \nMISSION:   \nMISSION: Real task"
        missions = _extract_missions(text, "myapp")
        assert len(missions) == 1
        assert "Real task" in missions[0]

    def test_strips_whitespace(self):
        text = "  MISSION:   Fix whitespace issue  \n"
        missions = _extract_missions(text, "myapp")
        assert len(missions) == 1
        assert missions[0] == "- [project:myapp] Fix whitespace issue"

    def test_uses_project_name_in_tag(self):
        text = "MISSION: Do something"
        missions = _extract_missions(text, "backend")
        assert missions[0].startswith("- [project:backend]")

    def test_ignores_non_mission_lines_with_mission_word(self):
        text = "The MISSION: is clear\nMISSION: Actual task"
        missions = _extract_missions(text, "myapp")
        assert len(missions) == 1
        assert "Actual task" in missions[0]

    def test_strips_duplicate_project_tag(self):
        text = "MISSION: [project:myapp] Fix the bug"
        missions = _extract_missions(text, "myapp")
        assert len(missions) == 1
        assert missions[0] == "- [project:myapp] Fix the bug"

    def test_strips_different_project_tag(self):
        """Claude might hallucinate a different project tag — replace it."""
        text = "MISSION: [project:wrong] Fix the bug"
        missions = _extract_missions(text, "myapp")
        assert missions[0] == "- [project:myapp] Fix the bug"

    def test_strips_leading_bullet(self):
        text = "MISSION: - Fix the bug"
        missions = _extract_missions(text, "myapp")
        assert missions[0] == "- [project:myapp] Fix the bug"

    def test_strips_bullet_and_tag_combined(self):
        text = "MISSION: - [project:myapp] Fix the bug"
        missions = _extract_missions(text, "myapp")
        assert missions[0] == "- [project:myapp] Fix the bug"


# ---------------------------------------------------------------------------
# _strip_mission_lines
# ---------------------------------------------------------------------------

class TestStripMissionLines:
    def test_removes_mission_lines(self):
        text = "Report here\nMISSION: Fix something\nMore report"
        result = _strip_mission_lines(text)
        assert "MISSION:" not in result
        assert "Report here" in result
        assert "More report" in result

    def test_no_mission_lines(self):
        text = "Just a normal report"
        result = _strip_mission_lines(text)
        assert result == "Just a normal report"

    def test_strips_trailing_whitespace(self):
        text = "Report\nMISSION: Task\n\n\n"
        result = _strip_mission_lines(text)
        assert result == "Report"


# ---------------------------------------------------------------------------
# _queue_missions
# ---------------------------------------------------------------------------

class TestQueueMissions:
    @patch("app.utils.insert_pending_mission")
    def test_inserts_each_mission(self, mock_insert):
        missions_path = Path("/tmp/missions.md")
        missions = [
            "- [project:myapp] Fix bug A",
            "- [project:myapp] Fix bug B",
        ]
        _queue_missions(missions_path, missions)
        assert mock_insert.call_count == 2
        mock_insert.assert_any_call(missions_path, "- [project:myapp] Fix bug A")
        mock_insert.assert_any_call(missions_path, "- [project:myapp] Fix bug B")

    @patch("app.utils.insert_pending_mission")
    def test_no_missions_no_calls(self, mock_insert):
        _queue_missions(Path("/tmp/missions.md"), [])
        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# run_exploration with missions
# ---------------------------------------------------------------------------

class TestRunExplorationWithMissions:
    @patch("app.utils.insert_pending_mission")
    @patch("app.cli_provider.run_command_streaming",
           return_value="Found issues\nMISSION: Fix bug A\nMISSION: Fix bug B")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_queues_missions_from_output(
        self, mock_prompt, mock_git, mock_struct, mock_missions,
        mock_claude, mock_insert, tmp_path
    ):
        notify = MagicMock()
        success, summary = run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        assert success is True
        assert "2 missions queued" in summary
        assert mock_insert.call_count == 2

    @patch("app.utils.insert_pending_mission")
    @patch("app.cli_provider.run_command_streaming",
           return_value="Found issues\nMISSION: Fix bug A\nMISSION: Fix bug B")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_telegram_shows_mission_count(
        self, mock_prompt, mock_git, mock_struct, mock_missions,
        mock_claude, mock_insert, tmp_path
    ):
        notify = MagicMock()
        run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        result_msg = notify.call_args_list[1][0][0]
        assert "2 mission(s) queued" in result_msg
        assert "MISSION:" not in result_msg

    @patch("app.cli_provider.run_command_streaming", return_value="No issues found")
    @patch("app.ai_runner.get_missions_context", return_value="No active missions.")
    @patch("app.ai_runner.gather_project_structure", return_value="Directories: src/")
    @patch("app.ai_runner.gather_git_activity", return_value="Recent commits: abc")
    @patch("app.ai_runner.load_skill_prompt", return_value="Explore myapp")
    def test_no_missions_no_suffix(
        self, mock_prompt, mock_git, mock_struct, mock_missions,
        mock_claude, tmp_path
    ):
        notify = MagicMock()
        success, summary = run_exploration(
            str(tmp_path), "myapp", str(tmp_path),
            notify_fn=notify,
        )
        assert success is True
        assert "0 missions queued" in summary
        result_msg = notify.call_args_list[1][0][0]
        assert "mission(s) queued" not in result_msg


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

class TestCLI:
    @patch("app.ai_runner.run_exploration", return_value=(True, "Done"))
    def test_main_success_returns_0(self, mock_run):
        exit_code = main([
            "--project-path", "/tmp/myapp",
            "--project-name", "myapp",
            "--instance-dir", "/tmp/instance",
        ])
        assert exit_code == 0
        mock_run.assert_called_once()

    @patch("app.ai_runner.run_exploration", return_value=(False, "Failed"))
    def test_main_failure_returns_1(self, mock_run):
        exit_code = main([
            "--project-path", "/tmp/myapp",
            "--project-name", "myapp",
            "--instance-dir", "/tmp/instance",
        ])
        assert exit_code == 1

    @patch("app.ai_runner.run_exploration", return_value=(True, "Done"))
    def test_main_passes_correct_args(self, mock_run):
        main([
            "--project-path", "/tmp/myapp",
            "--project-name", "myapp",
            "--instance-dir", "/tmp/instance",
        ])
        kwargs = mock_run.call_args[1]
        assert kwargs["project_path"] == "/tmp/myapp"
        assert kwargs["project_name"] == "myapp"
        assert kwargs["instance_dir"] == "/tmp/instance"

    @patch("app.ai_runner.run_exploration", return_value=(True, "Done"))
    def test_main_sets_skill_dir(self, mock_run):
        main([
            "--project-path", "/tmp/myapp",
            "--project-name", "myapp",
            "--instance-dir", "/tmp/instance",
        ])
        kwargs = mock_run.call_args[1]
        skill_dir = kwargs["skill_dir"]
        assert skill_dir.name == "ai"
        assert "skills/core/ai" in str(skill_dir)

    def test_main_requires_project_path(self):
        with pytest.raises(SystemExit):
            main(["--project-name", "myapp", "--instance-dir", "/tmp"])

    def test_main_requires_project_name(self):
        with pytest.raises(SystemExit):
            main(["--project-path", "/tmp", "--instance-dir", "/tmp"])

    def test_main_requires_instance_dir(self):
        with pytest.raises(SystemExit):
            main(["--project-path", "/tmp", "--project-name", "myapp"])
