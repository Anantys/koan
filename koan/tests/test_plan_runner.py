"""Tests for plan_runner.py — the plan execution pipeline."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.plan_runner import (
    run_plan,
    _generate_plan,
    _generate_iteration_plan,
    _run_claude_plan,
    _is_error_output,
    _strip_preamble,
    _get_repo_info,
    _fetch_issue_context,
    _format_comments,
    _extract_title,
    _extract_idea_from_issue,
    _strip_title_line,
    _search_existing_issue,
    _extract_search_keywords,
    _run_new_plan,
    _run_issue_plan,
    _PLAN_LABEL,
    main,
    _review_plan,
    _review_loop,
    _is_simple_plan,
    _review_warning_note,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# run_plan — top-level routing
# ---------------------------------------------------------------------------

class TestRunPlan:
    def test_no_idea_no_url_returns_error(self):
        ok, msg = run_plan("/project")
        assert not ok
        assert "No idea" in msg

    def test_routes_to_new_plan(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "done")) as mock:
            ok, msg = run_plan("/project", idea="Add feature", notify_fn=MagicMock())
            assert ok
            mock.assert_called_once()

    def test_routes_to_issue_plan(self):
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._run_issue_plan", return_value=(True, "done")) as mock:
            ok, msg = run_plan("/project", issue_url=url, notify_fn=MagicMock())
            assert ok
            mock.assert_called_once()

    def test_passes_context_to_new_plan(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "ok")) as mock:
            run_plan("/project", idea="Add X", notify_fn=MagicMock(), context="Phase 2")
            _, kwargs = mock.call_args
            assert kwargs.get("context") == "Phase 2"

    def test_passes_context_to_issue_plan(self):
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._run_issue_plan", return_value=(True, "ok")) as mock:
            run_plan("/project", issue_url=url, notify_fn=MagicMock(), context="Focus on API")
            _, kwargs = mock.call_args
            assert kwargs.get("context") == "Focus on API"

    def test_context_defaults_to_none(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "ok")) as mock:
            run_plan("/project", idea="Add X", notify_fn=MagicMock())
            _, kwargs = mock.call_args
            assert kwargs.get("context") is None

    def test_defaults_notify_fn(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "ok")) as mock, \
             patch("app.notify.send_telegram"):
            run_plan("/project", idea="test")
            # Should not crash — notify_fn defaults to send_telegram


# ---------------------------------------------------------------------------
# _run_new_plan
# ---------------------------------------------------------------------------

class TestRunNewPlan:
    def test_successful_plan_with_issue(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan\nStep 1"), \
             patch("app.plan_runner._get_repo_info", return_value=("sukria", "koan")), \
             patch("app.plan_runner._search_existing_issue", return_value=None), \
             patch("app.github.subprocess.run", return_value=MagicMock(
                 returncode=0, stdout="https://github.com/sukria/koan/issues/99\n"
             )):
            ok, msg = _run_new_plan("/project", "Add feature", notify, None)
            assert ok
            assert "issues/99" in msg
            notify.assert_called()

    def test_no_github_repo_sends_inline(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan\nStep 1"), \
             patch("app.plan_runner._get_repo_info", return_value=(None, None)):
            ok, msg = _run_new_plan("/project", "Add feature", notify, None)
            assert ok
            assert "inline" in msg
            # Plan was sent via notify_fn
            calls = [str(c) for c in notify.call_args_list]
            assert any("Plan" in c for c in calls)

    def test_generate_plan_failure(self):
        notify = MagicMock()
        with patch("app.plan_runner._get_repo_info", return_value=(None, None)), \
             patch("app.plan_runner._generate_plan", side_effect=RuntimeError("timeout")):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert not ok
            assert "failed" in msg.lower()

    def test_empty_plan(self):
        notify = MagicMock()
        with patch("app.plan_runner._get_repo_info", return_value=(None, None)), \
             patch("app.plan_runner._generate_plan", return_value=""):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert not ok
            assert "empty" in msg.lower()

    def test_context_passed_to_generate_plan(self):
        """User context should be forwarded to _generate_plan."""
        notify = MagicMock()
        with patch("app.plan_runner._get_repo_info", return_value=(None, None)), \
             patch("app.plan_runner._generate_plan", return_value="## Plan") as mock_gen:
            _run_new_plan("/project", "Add X", notify, None, context="Phase 2 only")
            _, kwargs = mock_gen.call_args
            assert kwargs.get("context") == "Phase 2 only"

    def test_no_context_passes_empty_string(self):
        """Without context, _generate_plan should receive empty string."""
        notify = MagicMock()
        with patch("app.plan_runner._get_repo_info", return_value=(None, None)), \
             patch("app.plan_runner._generate_plan", return_value="## Plan") as mock_gen:
            _run_new_plan("/project", "Add X", notify, None)
            _, kwargs = mock_gen.call_args
            assert kwargs.get("context") == ""

    def test_issue_creation_failure_with_label_retries_without(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner._get_repo_info", return_value=("o", "r")), \
             patch("app.plan_runner._search_existing_issue", return_value=None), \
             patch("app.github.subprocess.run") as mock_run:
            # First call fails (label issue), second succeeds
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr="label not found"),
                MagicMock(returncode=0, stdout="https://github.com/o/r/issues/5\n"),
            ]
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert ok
            assert "issues/5" in msg

    def test_issue_creation_total_failure(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner._get_repo_info", return_value=("o", "r")), \
             patch("app.plan_runner._search_existing_issue", return_value=None), \
             patch("app.github.subprocess.run", return_value=MagicMock(
                 returncode=1, stderr="no perms"
             )):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert ok
            assert "failed" in msg.lower()

    def test_sends_planning_notification(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner._get_repo_info", return_value=(None, None)):
            _run_new_plan("/project", "Add dark mode to dashboard", notify, None)
            first_msg = notify.call_args_list[0][0][0]
            assert "Planning" in first_msg
            assert "dark mode" in first_msg

    def test_long_idea_truncated_in_notification(self):
        notify = MagicMock()
        long_idea = "A" * 200
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner._get_repo_info", return_value=(None, None)):
            _run_new_plan("/project", long_idea, notify, None)
            first_msg = notify.call_args_list[0][0][0]
            assert "..." in first_msg

    def test_reuses_existing_issue_when_found(self):
        """When an existing issue matches, delegate to _run_issue_plan."""
        notify = MagicMock()
        with patch("app.plan_runner._get_repo_info", return_value=("sukria", "koan")), \
             patch("app.plan_runner._search_existing_issue",
                    return_value=("42", "Add dark mode")), \
             patch("app.plan_runner._run_issue_plan",
                    return_value=(True, "Plan posted on #42")) as mock_issue:
            ok, msg = _run_new_plan("/project", "dark mode feature", notify, None)
            assert ok
            assert "#42" in msg
            mock_issue.assert_called_once()
            # Verify the URL passed to _run_issue_plan
            url_arg = mock_issue.call_args[0][1]
            assert "issues/42" in url_arg

    def test_existing_issue_notification(self):
        """When reusing an issue, notify the user about the redirect."""
        notify = MagicMock()
        with patch("app.plan_runner._get_repo_info", return_value=("o", "r")), \
             patch("app.plan_runner._search_existing_issue",
                    return_value=("7", "Existing plan")), \
             patch("app.plan_runner._run_issue_plan",
                    return_value=(True, "ok")):
            _run_new_plan("/project", "similar idea", notify, None)
            # Should have notified about finding an existing issue
            msgs = [str(c) for c in notify.call_args_list]
            assert any("existing issue" in m.lower() or "Found" in m for m in msgs)

    def test_search_failure_creates_new_issue(self):
        """If search fails, proceed with new issue creation."""
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner._get_repo_info", return_value=("o", "r")), \
             patch("app.plan_runner._search_existing_issue", return_value=None), \
             patch("app.github.subprocess.run", return_value=MagicMock(
                 returncode=0, stdout="https://github.com/o/r/issues/10\n"
             )):
            ok, msg = _run_new_plan("/project", "brand new idea", notify, None)
            assert ok
            assert "issues/10" in msg

    def test_creates_issue_with_plan_label(self):
        """New issues should be created with the 'plan' label."""
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner._get_repo_info", return_value=("o", "r")), \
             patch("app.plan_runner._search_existing_issue", return_value=None), \
             patch("app.plan_runner.issue_create",
                    return_value="https://github.com/o/r/issues/1") as mock_create:
            _run_new_plan("/project", "test idea", notify, None)
            _, kwargs = mock_create.call_args
            assert kwargs.get("labels") == [_PLAN_LABEL]


# ---------------------------------------------------------------------------
# _run_issue_plan
# ---------------------------------------------------------------------------

class TestRunIssuePlan:
    def test_successful_iteration(self):
        notify = MagicMock()
        url = "https://github.com/sukria/koan/issues/64"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Issue Title", "body", "comments")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Updated Plan"), \
             patch("app.plan_runner._comment_on_issue") as mock_comment:
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert ok
            assert "#64" in msg
            mock_comment.assert_called_once()

    def test_invalid_url(self):
        notify = MagicMock()
        ok, msg = _run_issue_plan("/project", "not-a-url", notify, None)
        assert not ok
        assert "Invalid" in msg

    def test_fetch_failure(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    side_effect=RuntimeError("not found")):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "Failed to fetch" in msg

    def test_plan_generation_failure(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan",
                    side_effect=RuntimeError("error")):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "failed" in msg.lower()

    def test_empty_plan(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan", return_value=""):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "empty" in msg.lower()

    def test_comment_failure_sends_inline(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan"), \
             patch("app.plan_runner._comment_on_issue",
                    side_effect=RuntimeError("no perms")):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert ok
            assert "failed" in msg.lower()

    def test_sends_reading_notification(self):
        notify = MagicMock()
        url = "https://github.com/sukria/koan/issues/64"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan"), \
             patch("app.plan_runner._comment_on_issue"):
            _run_issue_plan("/project", url, notify, None)
            first_msg = notify.call_args_list[0][0][0]
            assert "#64" in first_msg

    def test_success_includes_title(self):
        notify = MagicMock()
        url = "https://github.com/sukria/koan/issues/64"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Add dark mode", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan"), \
             patch("app.plan_runner._comment_on_issue"):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert ok
            assert "Add dark mode" in msg

    def test_uses_iteration_prompt(self):
        """Issue plan should use _generate_iteration_plan, not _generate_plan."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body text", "alice: great idea")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Updated Plan") as mock_iter, \
             patch("app.plan_runner._comment_on_issue"):
            _run_issue_plan("/project", url, notify, None)
            mock_iter.assert_called_once()
            # Verify the issue context is passed
            context_arg = mock_iter.call_args[1].get("issue_context") or \
                          mock_iter.call_args[0][1]
            assert "Title" in context_arg
            assert "alice" in context_arg

    def test_no_comments_still_includes_context(self):
        """Even with no comments, the context should note that."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan") as mock_iter, \
             patch("app.plan_runner._comment_on_issue"):
            _run_issue_plan("/project", url, notify, None)
            context_arg = mock_iter.call_args[0][1]
            assert "No comments" in context_arg

    def test_user_context_appended_to_issue_context(self):
        """User context should appear in the issue context passed to Claude."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "comments")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan") as mock_iter, \
             patch("app.plan_runner._comment_on_issue"):
            _run_issue_plan("/project", url, notify, None, context="Focus on phase 2")
            context_arg = mock_iter.call_args[0][1]
            assert "User Instructions" in context_arg
            assert "Focus on phase 2" in context_arg

    def test_no_user_context_omits_instructions_section(self):
        """Without user context, no 'User Instructions' section should appear."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._fetch_issue_context",
                    return_value=("Title", "body", "")), \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan") as mock_iter, \
             patch("app.plan_runner._comment_on_issue"):
            _run_issue_plan("/project", url, notify, None)
            context_arg = mock_iter.call_args[0][1]
            assert "User Instructions" not in context_arg


# ---------------------------------------------------------------------------
# _generate_plan
# ---------------------------------------------------------------------------

class TestGeneratePlan:
    @patch("app.cli_provider.run_command_streaming", return_value="## Plan\n\nStep 1")
    def test_returns_claude_output(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            skill_dir = Path("/fake/skills/core/plan")
            result = _generate_plan("/project", "Add feature", skill_dir=skill_dir)
            assert "Step 1" in result

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_includes_context(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill") as mock_load:
            skill_dir = Path("/fake")
            _generate_plan("/project", "idea", context="prev", skill_dir=skill_dir)
            _, kwargs = mock_load.call_args
            assert kwargs["CONTEXT"] == "prev"

    @patch("app.cli_provider.run_command_streaming",
           side_effect=RuntimeError("CLI invocation failed: rate limited"))
    def test_raises_on_failure(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            with pytest.raises(RuntimeError, match="invocation failed"):
                _generate_plan("/project", "idea", skill_dir=Path("/fake"))

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_uses_read_only_tools(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            _generate_plan("/project", "idea", skill_dir=Path("/fake"))
            call_kwargs = mock_run.call_args[1]
            assert "Read" in call_kwargs.get("allowed_tools", [])

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_no_skill_dir_uses_load_prompt(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt") as mock_load:
            _generate_plan("/project", "idea")
            mock_load.assert_called_once()


# ---------------------------------------------------------------------------
# _generate_iteration_plan
# ---------------------------------------------------------------------------

class TestGenerateIterationPlan:
    @patch("app.cli_provider.run_command_streaming", return_value="## Updated Plan")
    def test_uses_plan_iterate_prompt(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill") as mock_load:
            skill_dir = Path("/fake/skills/core/plan")
            result = _generate_iteration_plan(
                "/project", "issue context here", skill_dir=skill_dir
            )
            assert "Updated Plan" in result
            # Verify it loads plan-iterate, not plan
            mock_load.assert_called_once_with(
                skill_dir, "plan-iterate", ISSUE_CONTEXT="issue context here"
            )

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_no_skill_dir_uses_load_prompt(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill") as mock_load:
            _generate_iteration_plan("/project", "context")
            mock_load.assert_called_once_with(
                None, "plan-iterate", ISSUE_CONTEXT="context"
            )

    @patch("app.cli_provider.run_command_streaming",
           side_effect=RuntimeError("CLI invocation failed: error"))
    def test_raises_on_failure(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            with pytest.raises(RuntimeError):
                _generate_iteration_plan(
                    "/project", "context", skill_dir=Path("/fake")
                )


# ---------------------------------------------------------------------------
# _run_claude_plan — shared Claude invocation
# ---------------------------------------------------------------------------

class TestRunClaudePlan:
    @patch("app.config.get_skill_timeout", return_value=3600)
    @patch("app.cli_provider.run_command_streaming", return_value="result with spaces")
    def test_returns_stripped_output(self, mock_cmd, mock_timeout):
        result = _run_claude_plan("test prompt", "/project")
        assert result == "result with spaces"
        mock_cmd.assert_called_once_with(
            "test prompt", "/project",
            allowed_tools=["Read", "Glob", "Grep", "WebFetch"],
            max_turns=25, timeout=3600,
        )

    @patch("app.cli_provider.run_command_streaming",
           side_effect=RuntimeError("CLI invocation failed: error msg"))
    def test_raises_on_non_zero_exit(self, mock_cmd):
        with pytest.raises(RuntimeError, match="CLI invocation failed"):
            _run_claude_plan("prompt", "/project")

    @patch("app.cli_provider.run_command_streaming",
           return_value="Error: Reached max turns (3)")
    def test_raises_on_max_turns_error(self, mock_cmd):
        with pytest.raises(RuntimeError, match="Reached max turns"):
            _run_claude_plan("prompt", "/project")

    @patch("app.cli_provider.run_command_streaming",
           return_value="Error: Something went wrong")
    def test_raises_on_short_error_output(self, mock_cmd):
        with pytest.raises(RuntimeError, match="Something went wrong"):
            _run_claude_plan("prompt", "/project")

    @patch("app.cli_provider.run_command_streaming",
           return_value=(
               "● Read files\nExcellent! Now I have all the context I need.\n"
               "\nClean title\n\n### Summary"
           ))
    def test_strips_preamble_from_output(self, mock_cmd):
        result = _run_claude_plan("prompt", "/project")
        assert result.startswith("Clean title")
        assert "● Read" not in result


# ---------------------------------------------------------------------------
# _is_error_output
# ---------------------------------------------------------------------------

class TestIsErrorOutput:
    def test_empty_string(self):
        assert _is_error_output("") is False

    def test_none(self):
        assert _is_error_output(None) is False

    def test_valid_plan_output(self):
        assert _is_error_output("### Summary\n\nThis plan does X.") is False

    def test_max_turns_error(self):
        assert _is_error_output("Error: Reached max turns (3)") is True

    def test_max_turns_error_with_prefix(self):
        assert _is_error_output("Some text\nReached max turns (25)\nmore") is True

    def test_short_error_message(self):
        assert _is_error_output("Error: Connection refused") is True

    def test_whitespace_prefixed_error(self):
        assert _is_error_output("  Error: Reached max turns (3)") is True

    def test_long_error_not_flagged(self):
        # A long "Error:" string is likely plan content mentioning errors
        long_text = "Error: " + "x" * 300
        assert _is_error_output(long_text) is False

    def test_error_in_plan_content_not_flagged(self):
        # An error word in normal plan content should not trigger
        assert _is_error_output(
            "### Error Handling\n\nWe should handle errors gracefully."
        ) is False


# ---------------------------------------------------------------------------
# _strip_preamble
# ---------------------------------------------------------------------------

class TestStripPreamble:
    def test_strips_now_i_have_context(self):
        output = (
            "I searched the codebase for relevant files.\n"
            "Excellent! Now I have all the context I need. "
            "Let me create the comprehensive plan:\n"
            "\n"
            "Add dark mode support\n"
            "\n"
            "### Summary\n"
            "\nThis plan adds dark mode."
        )
        result = _strip_preamble(output)
        assert result.startswith("Add dark mode support")
        assert "I searched" not in result
        assert "Excellent" not in result

    def test_strips_let_me_create_plan(self):
        output = (
            "Reading files...\n"
            "Let me create the structured plan:\n"
            "\n"
            "Fix auth module\n"
            "\n"
            "### Summary"
        )
        result = _strip_preamble(output)
        assert result.startswith("Fix auth module")

    def test_strips_heres_the_plan(self):
        output = (
            "Some exploration output\n"
            "Here's the comprehensive plan:\n"
            "\n"
            "Improve logging\n"
            "\n"
            "### Summary"
        )
        result = _strip_preamble(output)
        assert result.startswith("Improve logging")

    def test_strips_here_is_the_plan(self):
        output = "Here is the implementation plan:\n\nTitle\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Title")

    def test_no_preamble_returns_unchanged(self):
        output = "Add dark mode\n\n### Summary\n\nDetails"
        assert _strip_preamble(output) == output

    def test_empty_string(self):
        assert _strip_preamble("") == ""

    def test_none_returns_none(self):
        assert _strip_preamble(None) is None

    def test_multiple_preamble_lines_uses_last(self):
        output = (
            "Let me create the plan:\n"
            "Actually, let me generate the plan with more detail:\n"
            "\n"
            "Real title\n"
            "### Summary"
        )
        result = _strip_preamble(output)
        assert result.startswith("Real title")

    def test_preamble_only_returns_original(self):
        """If stripping leaves nothing, return original."""
        output = "Now I have all the context I need."
        result = _strip_preamble(output)
        assert result == output

    def test_long_copilot_preamble(self):
        """Simulate Copilot tool-use output followed by plan."""
        lines = [
            "● Read README.md",
            "  Contents of README...",
            "● Glob **/*.py",
            "  Found 42 files",
            "● Read src/main.py",
            "  def main():",
            "    pass",
            "",
            "Excellent! Now I have the context I need. "
            "Let me create the comprehensive plan:",
            "",
            "Add comprehensive test suite",
            "",
            "### Summary",
            "",
            "This plan adds tests.",
        ]
        output = "\n".join(lines)
        result = _strip_preamble(output)
        assert result.startswith("Add comprehensive test suite")
        assert "● Read" not in result

    def test_case_insensitive(self):
        output = "HERE IS THE PLAN:\n\nTitle\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Title")

    def test_ill_create_plan(self):
        output = "I'll create the plan now.\n\nTitle here\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Title here")

    def test_let_me_draft_the_plan(self):
        output = "Let me draft the plan:\n\nDraft title\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Draft title")


# ---------------------------------------------------------------------------
# _search_existing_issue
# ---------------------------------------------------------------------------

class TestSearchExistingIssue:
    def test_finds_matching_issue(self):
        results = json.dumps([
            {"number": 42, "title": "Plan: Add dark mode"},
        ])
        with patch("app.github.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout=results)):
            result = _search_existing_issue("sukria", "koan", "dark mode feature")
            assert result is not None
            assert result[0] == "42"
            assert result[1] == "Plan: Add dark mode"

    def test_no_matching_issues(self):
        with patch("app.github.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="[]")):
            result = _search_existing_issue("sukria", "koan", "unique idea")
            assert result is None

    def test_api_failure_returns_none(self):
        with patch("app.github.subprocess.run",
                    return_value=MagicMock(returncode=1, stderr="API error")):
            result = _search_existing_issue("sukria", "koan", "some idea")
            assert result is None

    def test_timeout_returns_none(self):
        with patch("app.github.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30)):
            result = _search_existing_issue("o", "r", "idea")
            assert result is None

    def test_empty_keywords_returns_none(self):
        """If idea is only stop words, don't search."""
        result = _search_existing_issue("o", "r", "the a an is")
        assert result is None

    def test_returns_first_match_only(self):
        results = json.dumps([
            {"number": 10, "title": "First match"},
            {"number": 20, "title": "Second match"},
        ])
        with patch("app.github.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout=results)):
            result = _search_existing_issue("o", "r", "keyword test")
            assert result[0] == "10"


# ---------------------------------------------------------------------------
# _extract_search_keywords
# ---------------------------------------------------------------------------

class TestExtractSearchKeywords:
    def test_filters_stop_words(self):
        result = _extract_search_keywords("Add a dark mode to the dashboard")
        assert "dark" in result
        assert "mode" in result
        assert "dashboard" in result
        assert "the" not in result
        assert "add" not in result

    def test_limits_to_4_keywords(self):
        result = _extract_search_keywords(
            "Implement authentication authorization caching logging monitoring"
        )
        words = result.split()
        assert len(words) <= 4

    def test_empty_string(self):
        assert _extract_search_keywords("") == ""

    def test_only_stop_words(self):
        assert _extract_search_keywords("the a an is are") == ""

    def test_case_insensitive(self):
        result = _extract_search_keywords("DARK MODE Feature")
        assert "dark" in result
        assert "mode" in result
        assert "feature" in result

    def test_short_words_excluded(self):
        """Single-letter words should be excluded."""
        result = _extract_search_keywords("X Y Z authentication")
        assert "authentication" in result


# ---------------------------------------------------------------------------
# _get_repo_info
# ---------------------------------------------------------------------------

class TestGetRepoInfo:
    def test_successful_gh_call(self):
        gh_output = json.dumps({"owner": {"login": "sukria"}, "name": "koan"})
        with patch("app.github.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout=gh_output)):
            owner, repo = _get_repo_info("/path")
            assert owner == "sukria"
            assert repo == "koan"

    def test_gh_failure_returns_none(self):
        with patch("app.github.subprocess.run",
                    return_value=MagicMock(returncode=1, stderr="err")):
            owner, repo = _get_repo_info("/path")
            assert owner is None
            assert repo is None

    def test_timeout_returns_none(self):
        with patch("app.github.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15)):
            owner, repo = _get_repo_info("/path")
            assert owner is None
            assert repo is None


# ---------------------------------------------------------------------------
# _fetch_issue_context
# ---------------------------------------------------------------------------

class TestFetchIssueContext:
    @patch("app.plan_runner.fetch_issue_with_comments")
    def test_returns_title_body_and_comments(self, mock_fetch):
        mock_fetch.return_value = (
            "My Issue", "Body",
            [{"author": "alice", "date": "2026-02-01T10:00:00Z", "body": "Looks good"}],
        )
        title, body, comments = _fetch_issue_context("sukria", "koan", "64")
        assert title == "My Issue"
        assert body == "Body"
        assert "alice" in comments
        mock_fetch.assert_called_once_with("sukria", "koan", "64")

    @patch("app.plan_runner.fetch_issue_with_comments")
    def test_handles_empty_comments(self, mock_fetch):
        mock_fetch.return_value = ("Title", "Body", [])
        title, body, comments = _fetch_issue_context("o", "r", "1")
        assert title == "Title"
        assert comments == ""

    @patch("app.plan_runner.fetch_issue_with_comments")
    def test_propagates_runtime_error(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("gh failed")
        with pytest.raises(RuntimeError):
            _fetch_issue_context("o", "r", "1")


# ---------------------------------------------------------------------------
# _format_comments
# ---------------------------------------------------------------------------

class TestFormatComments:
    def test_formats_with_author_and_date(self):
        data = [
            {"author": "alice", "date": "2026-02-01T10:00:00Z", "body": "Good"},
        ]
        result = _format_comments(data)
        assert "alice" in result
        assert "2026-02-01" in result

    def test_empty_list(self):
        assert _format_comments([]) == ""

    def test_none_input(self):
        assert _format_comments(None) == ""

    def test_non_list_input(self):
        assert _format_comments("not a list") == ""

    def test_skips_empty_body(self):
        data = [
            {"author": "a", "date": "2026-01-01T00:00:00Z", "body": ""},
            {"author": "b", "date": "2026-01-02T00:00:00Z", "body": "useful"},
        ]
        result = _format_comments(data)
        assert "useful" in result
        assert result.count("**") == 2


# ---------------------------------------------------------------------------
# _extract_title
# ---------------------------------------------------------------------------

class TestExtractTitle:
    def test_from_heading(self):
        assert _extract_title("## Dark mode\n\nDetails") == "Dark mode"

    def test_first_non_empty_line(self):
        assert _extract_title("\n\nThis is the plan") == "This is the plan"

    def test_truncates(self):
        assert len(_extract_title("# " + "A" * 200)) <= 120

    def test_fallback(self):
        assert _extract_title("") == "Implementation Plan"

    def test_skips_generic_headings(self):
        """Generic section headings like 'Summary' are skipped."""
        assert _extract_title("### Summary\nReal plan title") == "Real plan title"
        assert _extract_title("### Summary") == "Implementation Plan"

    def test_first_line_title(self):
        """Title as plain first line (new prompt format)."""
        plan = "Add dark mode with theme persistence\n\n### Summary\n\nDetails"
        assert _extract_title(plan) == "Add dark mode with theme persistence"

    def test_strips_bullet_prefix(self):
        """Copilot-style ● prefix is stripped from title."""
        assert _extract_title("● GitHub notifications\n\n### Summary") == "GitHub notifications"

    def test_strips_arrow_prefix(self):
        assert _extract_title("→ Fix auth module\n\nDetails") == "Fix auth module"
        assert _extract_title("► Improve performance\n\nDetails") == "Improve performance"

    def test_strips_multiple_noise_chars(self):
        assert _extract_title(">> Some title\n\nBody") == "Some title"
        assert _extract_title("●● Double bullet\n\nBody") == "Double bullet"

    def test_noise_char_with_heading(self):
        assert _extract_title("# ● Noisy heading\n\nBody") == "Noisy heading"


# ---------------------------------------------------------------------------
# _strip_title_line
# ---------------------------------------------------------------------------

class TestStripTitleLine:
    def test_removes_first_line(self):
        text = "My title\n\n### Summary\n\nDetails here"
        result = _strip_title_line(text)
        assert "My title" not in result
        assert "### Summary" in result
        assert "Details here" in result

    def test_preserves_body(self):
        text = "Title\n\n### Summary\n\nA paragraph.\n\n### Phases\n\nPhase 1"
        result = _strip_title_line(text)
        assert result.startswith("### Summary")

    def test_empty_string(self):
        assert _strip_title_line("") == ""

    def test_title_only(self):
        assert _strip_title_line("Just a title") == "Just a title"

    def test_skips_leading_blank_lines(self):
        text = "\n\nActual title\n\nBody content"
        result = _strip_title_line(text)
        assert "Actual title" not in result
        assert "Body content" in result


# ---------------------------------------------------------------------------
# _extract_idea_from_issue
# ---------------------------------------------------------------------------

class TestExtractIdeaFromIssue:
    def test_first_paragraph(self):
        assert "Add dark mode" in _extract_idea_from_issue(
            "## Plan: Add dark mode\n\nDetails"
        )

    def test_skips_metadata(self):
        assert "real idea" in _extract_idea_from_issue(
            "---\n*Generated by Kōan*\n\nThe real idea"
        )

    def test_empty_body(self):
        assert "Review" in _extract_idea_from_issue("")
        assert "Review" in _extract_idea_from_issue(None)

    def test_strips_plan_prefix(self):
        idea = _extract_idea_from_issue("Plan: Implement X\n\nDetails")
        assert idea.startswith("Implement X")

    def test_truncates(self):
        assert len(_extract_idea_from_issue("A" * 600)) <= 500


# ---------------------------------------------------------------------------
# CLI entry point — main()
# ---------------------------------------------------------------------------

class TestCLI:
    def test_idea_mode(self):
        with patch("app.plan_runner.run_plan",
                    return_value=(True, "Plan created")) as mock:
            code = main(["--project-path", "/proj", "--idea", "Add auth"])
            assert code == 0
            mock.assert_called_once()
            assert mock.call_args.kwargs["idea"] == "Add auth"
            assert mock.call_args.kwargs["project_path"] == "/proj"

    def test_issue_url_mode(self):
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner.run_plan",
                    return_value=(True, "Posted")) as mock:
            code = main(["--project-path", "/proj", "--issue-url", url])
            assert code == 0
            assert mock.call_args.kwargs["issue_url"] == url

    def test_failure_returns_1(self):
        with patch("app.plan_runner.run_plan",
                    return_value=(False, "error")):
            code = main(["--project-path", "/proj", "--idea", "bad"])
            assert code == 1

    def test_missing_args_exits(self):
        with pytest.raises(SystemExit):
            main([])

    def test_both_idea_and_url_exits(self):
        with pytest.raises(SystemExit):
            main(["--project-path", "/p", "--idea", "x",
                   "--issue-url", "https://github.com/o/r/issues/1"])

    def test_skill_dir_resolved(self):
        with patch("app.plan_runner.run_plan",
                    return_value=(True, "ok")) as mock:
            main(["--project-path", "/proj", "--idea", "test"])
            skill_dir = mock.call_args.kwargs["skill_dir"]
            assert skill_dir.name == "plan"
            assert "skills/core/plan" in str(skill_dir)


# ---------------------------------------------------------------------------
# Prompt files — structure validation
# ---------------------------------------------------------------------------

PROMPTS_DIR = (
    Path(__file__).parent.parent / "skills" / "core" / "plan" / "prompts"
)


class TestPromptFiles:
    def test_plan_prompt_exists(self):
        assert (PROMPTS_DIR / "plan.md").exists()

    def test_plan_prompt_has_placeholders(self):
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "{IDEA}" in content
        assert "{CONTEXT}" in content

    def test_plan_prompt_has_phases(self):
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "phase" in content.lower()

    def test_plan_iterate_prompt_exists(self):
        assert (PROMPTS_DIR / "plan-iterate.md").exists()

    def test_plan_iterate_prompt_has_placeholders(self):
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "{ISSUE_CONTEXT}" in content

    def test_plan_iterate_prompt_has_required_sections(self):
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "Changes in this iteration" in content
        assert "comments" in content.lower()
        assert "Implementation Phases" in content
        assert "phase" in content.lower()

    def test_plan_iterate_prompt_instructs_feedback_processing(self):
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "suggestion" in content.lower()
        assert "question" in content.lower()

    def test_plan_prompt_requires_title_line(self):
        """Plan prompt instructs Claude to write a descriptive title as first line."""
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "FIRST LINE" in content
        assert "title" in content.lower()

    def test_plan_iterate_prompt_requires_title_line(self):
        """Iterate prompt also requires a title first line."""
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "FIRST LINE" in content

    def test_plan_prompt_has_phase_format(self):
        """Plan prompt uses #### Phase format with structured fields."""
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "#### Phase" in content
        assert "**What**" in content
        assert "**Done when**" in content

    def test_plan_iterate_prompt_has_phase_format(self):
        """Iterate prompt uses same #### Phase format."""
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "#### Phase" in content
        assert "**What**" in content
        assert "**Done when**" in content


# ---------------------------------------------------------------------------
# main() CLI — --context flag
# ---------------------------------------------------------------------------

class TestMainCLI:
    def test_context_flag_passed_to_run_plan(self):
        """--context flag should be forwarded to run_plan."""
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--context", "Focus on phase 2",
            ])
            _, kwargs = mock.call_args
            assert kwargs["context"] == "Focus on phase 2"

    def test_context_flag_optional(self):
        """Omitting --context should pass None."""
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main(["--project-path", "/project", "--idea", "Add feature"])
            _, kwargs = mock.call_args
            assert kwargs["context"] is None

    def test_context_with_idea(self):
        """--context can be used with --idea too."""
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--idea", "Add feature",
                "--context", "Must support dark mode",
            ])
            _, kwargs = mock.call_args
            assert kwargs["idea"] == "Add feature"
            assert kwargs["context"] == "Must support dark mode"


# ---------------------------------------------------------------------------
# _is_simple_plan
# ---------------------------------------------------------------------------

class TestIsSimplePlan:
    def test_single_phase_short_plan_is_simple(self):
        plan = "Rename function foo to bar in utils.py\n\nEdit the file."
        assert _is_simple_plan(plan)

    def test_multi_phase_plan_is_not_simple(self):
        plan = (
            "Implement feature\n\n"
            "#### Phase 1\nDo this.\n\n"
            "#### Phase 2\nDo that.\n"
        )
        assert not _is_simple_plan(plan)

    def test_single_phase_long_plan_is_not_simple(self):
        # Single phase but many lines — not simple enough to skip review
        plan = "#### Phase 1\n" + "\n".join(f"Step {i}" for i in range(25))
        assert not _is_simple_plan(plan)

    def test_empty_plan_is_simple(self):
        assert _is_simple_plan("")

    def test_exactly_two_phases_not_simple(self):
        plan = (
            "Title\n\n"
            "#### Phase 1\nDo A.\n\n"
            "#### Phase 2\nDo B.\n"
        )
        assert not _is_simple_plan(plan)


# ---------------------------------------------------------------------------
# _review_plan
# ---------------------------------------------------------------------------

class TestReviewPlan:
    def _skill_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_approved_on_approved_output(self):
        with patch("app.cli_provider.run_command", return_value="APPROVED\n"):
            approved, issues = _review_plan("## Plan\nStep 1", "/project", self._skill_dir())
        assert approved
        assert issues == ""

    def test_issues_found_returns_false_and_issues(self):
        reviewer_output = "ISSUES_FOUND\n- Phase 1: no file path\n- Phase 2: missing tests"
        with patch("app.cli_provider.run_command", return_value=reviewer_output):
            approved, issues = _review_plan("## Plan\nStep 1", "/project", self._skill_dir())
        assert not approved
        assert "no file path" in issues

    def test_malformed_output_treated_as_approved(self):
        with patch("app.cli_provider.run_command", return_value="Maybe looks ok"):
            approved, issues = _review_plan("## Plan\nStep 1", "/project", self._skill_dir())
        assert approved

    def test_run_command_exception_fails_open(self):
        with patch("app.cli_provider.run_command", side_effect=RuntimeError("timeout")):
            approved, issues = _review_plan("## Plan", "/project", self._skill_dir())
        assert approved

    def test_empty_output_treated_as_approved(self):
        with patch("app.cli_provider.run_command", return_value=""):
            approved, issues = _review_plan("## Plan", "/project", self._skill_dir())
        assert approved


# ---------------------------------------------------------------------------
# _review_loop
# ---------------------------------------------------------------------------

class TestReviewLoop:
    def _skill_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_approved_first_round_returns_plan(self):
        with patch("app.plan_runner._review_plan", return_value=(True, "")) as mock_review:
            result = _review_loop(
                "my plan", "/project", idea="idea", context="", skill_dir=self._skill_dir(),
                max_rounds=3,
            )
        assert result == "my plan"
        assert mock_review.call_count == 1

    def test_approved_second_round_after_regen(self):
        review_results = [(False, "- Missing file path"), (True, "")]
        with patch("app.plan_runner._review_plan", side_effect=review_results), \
             patch("app.plan_runner._run_claude_plan", return_value="improved plan"):
            result = _review_loop(
                "initial plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
            )
        assert result == "improved plan"

    def test_max_rounds_exhausted_returns_plan_with_warning(self):
        review_results = [
            (False, "- Phase 1: no file path"),
            (False, "- Phase 1: no file path"),
            (False, "- Phase 1: no file path"),
        ]
        with patch("app.plan_runner._review_plan", side_effect=review_results), \
             patch("app.plan_runner._run_claude_plan", return_value="regen plan"):
            result = _review_loop(
                "initial plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
            )
        assert "⚠️" in result
        assert "human review recommended" in result

    def test_regen_failure_keeps_previous_plan(self):
        with patch("app.plan_runner._review_plan", return_value=(False, "- issue")), \
             patch("app.plan_runner._run_claude_plan", side_effect=RuntimeError("boom")):
            result = _review_loop(
                "original plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=2,
            )
        # Should not crash; should contain warning after max rounds
        assert "original plan" in result or "⚠️" in result

    def test_regen_empty_keeps_previous_plan(self):
        review_results = [(False, "- issue"), (True, "")]
        with patch("app.plan_runner._review_plan", side_effect=review_results), \
             patch("app.plan_runner._run_claude_plan", return_value=""):
            result = _review_loop(
                "original plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
            )
        # Empty regen keeps original; then approved on round 2 with original
        assert result == "original plan"

    def test_iteration_mode_uses_plan_iterate_prompt(self):
        with patch("app.plan_runner._review_plan", side_effect=[(False, "- issue"), (True, "")]), \
             patch("app.plan_runner._run_claude_plan", return_value="iter plan") as mock_run, \
             patch("app.plan_runner.load_prompt_or_skill", return_value="prompt text") as mock_load:
            result = _review_loop(
                "initial", "/project", idea="", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
                is_iteration=True, issue_context="issue ctx",
            )
        # Should have called load_prompt_or_skill with "plan-iterate"
        calls = [c[0][1] for c in mock_load.call_args_list]
        assert "plan-iterate" in calls


# ---------------------------------------------------------------------------
# _generate_plan — review loop integration
# ---------------------------------------------------------------------------

class TestGeneratePlanWithReview:
    def _skill_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_review_skipped_for_simple_plan(self):
        short_plan = "Do one thing quickly."
        with patch("app.plan_runner._run_claude_plan", return_value=short_plan), \
             patch("app.plan_runner._review_loop") as mock_loop, \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": True, "max_rounds": 3}):
            result = _generate_plan("/project", "rename X", skill_dir=self._skill_dir())
        mock_loop.assert_not_called()
        assert result == short_plan

    def test_review_runs_for_multi_phase_plan(self):
        big_plan = (
            "Multi-phase feature\n\n"
            "#### Phase 1\nDo A.\n\n"
            "#### Phase 2\nDo B.\n"
        )
        reviewed_plan = big_plan + "\n(reviewed)"
        with patch("app.plan_runner._run_claude_plan", return_value=big_plan), \
             patch("app.plan_runner._review_loop", return_value=reviewed_plan) as mock_loop, \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": True, "max_rounds": 3}):
            result = _generate_plan("/project", "big feature", skill_dir=self._skill_dir())
        mock_loop.assert_called_once()
        assert result == reviewed_plan

    def test_review_disabled_skips_loop(self):
        big_plan = (
            "Multi-phase feature\n\n"
            "#### Phase 1\nDo A.\n\n"
            "#### Phase 2\nDo B.\n"
        )
        with patch("app.plan_runner._run_claude_plan", return_value=big_plan), \
             patch("app.plan_runner._review_loop") as mock_loop, \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": False, "max_rounds": 3}):
            _generate_plan("/project", "big feature", skill_dir=self._skill_dir())
        mock_loop.assert_not_called()
