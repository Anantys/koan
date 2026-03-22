"""Tests for LLM-driven mission decomposition (app.decompose + missions.inject_subtasks)."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure KOAN_ROOT is set before any imports
os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")


# ---------------------------------------------------------------------------
# app.decompose — unit tests
# ---------------------------------------------------------------------------

class TestParseDecomposeOutput:
    """Tests for _parse_decompose_output — pure function, no mocking needed."""

    def setup_method(self):
        from app.decompose import _parse_decompose_output
        self.parse = _parse_decompose_output

    def test_atomic_returns_none(self):
        output = json.dumps({"type": "atomic"})
        assert self.parse(output) is None

    def test_composite_returns_subtasks(self):
        data = {"type": "composite", "subtasks": ["Task A", "Task B", "Task C"]}
        result = self.parse(json.dumps(data))
        assert result == ["Task A", "Task B", "Task C"]

    def test_empty_output_returns_none(self):
        assert self.parse("") is None

    def test_malformed_json_returns_none(self):
        assert self.parse("not json at all") is None

    def test_composite_empty_subtasks_returns_none(self):
        output = json.dumps({"type": "composite", "subtasks": []})
        assert self.parse(output) is None

    def test_caps_subtasks_at_six(self):
        data = {"type": "composite", "subtasks": [f"Task {i}" for i in range(10)]}
        result = self.parse(json.dumps(data))
        assert result is not None
        assert len(result) == 6

    def test_json_embedded_in_text(self):
        """Claude may wrap JSON in prose — we extract it."""
        embedded = 'Here is my analysis:\n{"type": "atomic"}\nEnd.'
        assert self.parse(embedded) is None

    def test_composite_embedded_in_text(self):
        data = {"type": "composite", "subtasks": ["Do X", "Do Y"]}
        wrapped = f"My response:\n{json.dumps(data)}\nThat's it."
        result = self.parse(wrapped)
        assert result == ["Do X", "Do Y"]

    def test_filters_blank_subtasks(self):
        data = {"type": "composite", "subtasks": ["Real task", "", "  ", "Another task"]}
        result = self.parse(json.dumps(data))
        assert result == ["Real task", "Another task"]

    def test_unknown_type_returns_none(self):
        output = json.dumps({"type": "unknown"})
        assert self.parse(output) is None


class TestShouldDecompose:
    """Tests for the should_decompose() tag detector."""

    def setup_method(self):
        from app.decompose import should_decompose
        self.check = should_decompose

    def test_tagged_mission_returns_true(self):
        assert self.check("[project:koan] [decompose] Fix the auth module")

    def test_untagged_mission_returns_false(self):
        assert not self.check("Fix the auth module")

    def test_case_insensitive(self):
        assert self.check("[DECOMPOSE] Fix the auth module")

    def test_empty_string_returns_false(self):
        assert not self.check("")


class TestIsAlreadyDecomposed:
    """Tests for the is_already_decomposed() guard."""

    def setup_method(self):
        from app.decompose import is_already_decomposed
        self.check = is_already_decomposed

    def test_group_tag_returns_true(self):
        assert self.check("[project:koan] [group:abc12345] Fix middleware")

    def test_decomposed_tag_returns_true(self):
        assert self.check("[project:koan] [decomposed:abc12345] Fix middleware")

    def test_plain_mission_returns_false(self):
        assert not self.check("[project:koan] Fix middleware")

    def test_decompose_tag_without_id_returns_false(self):
        """[decompose] (no colon+id) is a request, not a result."""
        assert not self.check("[project:koan] [decompose] Fix middleware")


class TestDecomposeMission:
    """Tests for decompose_mission() — mocks CLI subprocess.

    Imports in decompose_mission() are lazy (inside the function), so we patch
    the source modules rather than app.decompose attributes.
    """

    def _mock_success(self, output: str):
        result = MagicMock()
        result.returncode = 0
        result.stdout = output
        result.stderr = ""
        return result

    def _mock_failure(self, stderr: str = "error"):
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = stderr
        return result

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--print"])
    @patch("app.prompts.load_prompt", return_value="prompt text")
    @patch("app.config.get_model_config", return_value={"lightweight": "haiku", "fallback": "sonnet"})
    def test_atomic_mission_returns_none(self, mock_models, mock_prompt, mock_cmd, mock_cli):
        from app.decompose import decompose_mission
        mock_cli.return_value = self._mock_success(json.dumps({"type": "atomic"}))
        result = decompose_mission("Fix a small bug", "/tmp/project")
        assert result is None

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--print"])
    @patch("app.prompts.load_prompt", return_value="prompt text")
    @patch("app.config.get_model_config", return_value={"lightweight": "haiku", "fallback": "sonnet"})
    def test_composite_mission_returns_subtasks(self, mock_models, mock_prompt, mock_cmd, mock_cli):
        from app.decompose import decompose_mission
        data = {"type": "composite", "subtasks": ["Step 1", "Step 2"]}
        mock_cli.return_value = self._mock_success(json.dumps(data))
        result = decompose_mission("Refactor the entire auth system", "/tmp/project")
        assert result == ["Step 1", "Step 2"]

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--print"])
    @patch("app.prompts.load_prompt", return_value="prompt text")
    @patch("app.config.get_model_config", return_value={"lightweight": "haiku", "fallback": "sonnet"})
    def test_malformed_json_returns_none(self, mock_models, mock_prompt, mock_cmd, mock_cli):
        from app.decompose import decompose_mission
        mock_cli.return_value = self._mock_success("this is not json")
        result = decompose_mission("Some mission", "/tmp/project")
        assert result is None

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--print"])
    @patch("app.prompts.load_prompt", return_value="prompt text")
    @patch("app.config.get_model_config", return_value={"lightweight": "haiku", "fallback": "sonnet"})
    def test_cli_failure_returns_none(self, mock_models, mock_prompt, mock_cmd, mock_cli):
        from app.decompose import decompose_mission
        mock_cli.return_value = self._mock_failure("quota exceeded")
        result = decompose_mission("Some mission", "/tmp/project")
        assert result is None

    def test_empty_mission_text_returns_none(self):
        from app.decompose import decompose_mission
        assert decompose_mission("", "/tmp/project") is None

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--print"])
    @patch("app.prompts.load_prompt", return_value="prompt text")
    @patch("app.config.get_model_config", return_value={"lightweight": "haiku", "fallback": "sonnet"})
    def test_cli_exception_returns_none(self, mock_models, mock_prompt, mock_cmd, mock_cli):
        from app.decompose import decompose_mission
        mock_cli.side_effect = OSError("process failed")
        result = decompose_mission("Some mission", "/tmp/project")
        assert result is None


# ---------------------------------------------------------------------------
# missions.inject_subtasks — unit tests
# ---------------------------------------------------------------------------

class TestInjectSubtasks:
    """Tests for missions.inject_subtasks()."""

    def setup_method(self):
        from app.missions import inject_subtasks
        self.inject = inject_subtasks

    CONTENT = """\
# Missions

## Pending
- [project:koan] [decompose] Refactor the auth module ⏳(2026-03-20T19:00)
- [project:koan] Fix bug ⏳(2026-03-20T19:01)

## In Progress

## Done
"""

    def test_replaces_parent_with_subtasks(self):
        content = self.inject(
            self.CONTENT,
            "[project:koan] [decompose] Refactor the auth module ⏳(2026-03-20T19:00)",
            ["Add middleware", "Update tests"],
            "abc12345",
        )
        assert "Add middleware" in content
        assert "Update tests" in content
        assert "[group:abc12345]" in content
        # Parent line removed
        assert "[decompose] Refactor the auth module" not in content

    def test_subtasks_inherit_project_tag(self):
        content = self.inject(
            self.CONTENT,
            "[project:koan] [decompose] Refactor the auth module ⏳(2026-03-20T19:00)",
            ["Fix middleware", "Write tests"],
            "grp001",
        )
        assert "[project:koan] [group:grp001] Fix middleware" in content
        assert "[project:koan] [group:grp001] Write tests" in content

    def test_subtasks_get_queued_timestamps(self):
        content = self.inject(
            self.CONTENT,
            "[project:koan] [decompose] Refactor the auth module ⏳(2026-03-20T19:00)",
            ["Task A"],
            "ts001",
        )
        # Each subtask should have a ⏳ timestamp
        assert "⏳(" in content

    def test_other_missions_preserved(self):
        content = self.inject(
            self.CONTENT,
            "[project:koan] [decompose] Refactor the auth module ⏳(2026-03-20T19:00)",
            ["Sub A"],
            "xyz",
        )
        assert "Fix bug" in content

    def test_no_project_tag_on_parent(self):
        no_project_content = """\
# Missions

## Pending
- [decompose] Do something complex ⏳(2026-03-20T19:00)

## In Progress

## Done
"""
        content = self.inject(
            no_project_content,
            "[decompose] Do something complex ⏳(2026-03-20T19:00)",
            ["Step 1", "Step 2"],
            "noprj",
        )
        # Sub-tasks should NOT have a project tag
        assert "[project:" not in content
        assert "[group:noprj] Step 1" in content
        assert "[group:noprj] Step 2" in content

    def test_empty_subtasks_returns_unchanged(self):
        content = self.inject(self.CONTENT, "anything", [], "grp")
        assert content == self.CONTENT

    def test_matching_without_timestamp(self):
        """Parent line should be found even if timestamps differ slightly."""
        content = self.inject(
            self.CONTENT,
            # Provide text without the timestamp — should still match
            "[project:koan] [decompose] Refactor the auth module",
            ["Task X"],
            "notimestamp",
        )
        assert "[group:notimestamp] Task X" in content
