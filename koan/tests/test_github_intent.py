"""Tests for github_intent.py — NLP intent classifier for GitHub @mentions."""

import json
from unittest.mock import patch

import pytest

from app.github_intent import _parse_classification, classify_intent

pytestmark = pytest.mark.slow


SAMPLE_COMMANDS = [
    ("rebase", "Rebase a PR onto the latest base branch"),
    ("implement", "Implement a feature from an issue"),
    ("fix", "Fix a bug"),
    ("review", "Review a pull request"),
]


class TestParseClassification:
    def test_valid_json(self):
        result = _parse_classification('{"command": "fix", "context": "the login bug"}')
        assert result == {"command": "fix", "context": "the login bug"}

    def test_null_command(self):
        result = _parse_classification('{"command": null, "context": ""}')
        assert result == {"command": None, "context": ""}

    def test_json_in_code_block(self):
        text = '```json\n{"command": "rebase", "context": ""}\n```'
        result = _parse_classification(text)
        assert result == {"command": "rebase", "context": ""}

    def test_json_with_surrounding_text(self):
        text = 'Here is the result:\n{"command": "review", "context": "PR #42"}\nDone.'
        result = _parse_classification(text)
        assert result == {"command": "review", "context": "PR #42"}

    def test_empty_output(self):
        assert _parse_classification("") is None
        assert _parse_classification(None) is None

    def test_invalid_json(self):
        assert _parse_classification("not json at all") is None

    def test_strips_slash_from_command(self):
        result = _parse_classification('{"command": "/fix", "context": ""}')
        assert result == {"command": "fix", "context": ""}

    def test_empty_command_becomes_none(self):
        result = _parse_classification('{"command": "", "context": ""}')
        assert result == {"command": None, "context": ""}

    def test_non_dict_json(self):
        assert _parse_classification("[1, 2, 3]") is None

    def test_missing_context_defaults_empty(self):
        result = _parse_classification('{"command": "fix"}')
        assert result == {"command": "fix", "context": ""}


def _patch_cli_and_prompt(mock_output):
    """Patch both run_command and load_prompt for classify_intent tests."""
    prompt_template = "Commands:\n{COMMANDS}\n\nMessage:\n{MESSAGE}"
    return (
        patch("app.cli_provider.run_command", return_value=mock_output),
        patch("app.prompts.load_prompt", return_value=prompt_template),
    )


class TestClassifyIntent:
    def test_successful_classification(self):
        mock_output = '{"command": "fix", "context": "the login bug"}'
        p1, p2 = _patch_cli_and_prompt(mock_output)
        with p1, p2:
            result = classify_intent(
                "this is a bug, please fix it",
                SAMPLE_COMMANDS,
                "/tmp/project",
            )
        assert result == {"command": "fix", "context": "the login bug"}

    def test_empty_message(self):
        assert classify_intent("", SAMPLE_COMMANDS, "/tmp/project") is None
        assert classify_intent("  ", SAMPLE_COMMANDS, "/tmp/project") is None

    def test_no_commands(self):
        assert classify_intent("fix this", [], "/tmp/project") is None

    def test_cli_failure_returns_none(self):
        prompt_template = "Commands:\n{COMMANDS}\n\nMessage:\n{MESSAGE}"
        with patch("app.cli_provider.run_command", side_effect=RuntimeError("timeout")), \
             patch("app.prompts.load_prompt", return_value=prompt_template):
            result = classify_intent(
                "please review this PR",
                SAMPLE_COMMANDS,
                "/tmp/project",
            )
        assert result is None

    def test_os_error_returns_none(self):
        prompt_template = "Commands:\n{COMMANDS}\n\nMessage:\n{MESSAGE}"
        with patch("app.cli_provider.run_command", side_effect=OSError("no such file")), \
             patch("app.prompts.load_prompt", return_value=prompt_template):
            result = classify_intent(
                "please review this PR",
                SAMPLE_COMMANDS,
                "/tmp/project",
            )
        assert result is None

    def test_prompt_loaded_and_filled(self):
        mock_output = '{"command": "rebase", "context": ""}'
        prompt_template = "Commands:\n{COMMANDS}\n\nMessage:\n{MESSAGE}"
        with patch("app.cli_provider.run_command", return_value=mock_output) as mock_run, \
             patch("app.prompts.load_prompt", return_value=prompt_template):
            classify_intent("rebase this please", SAMPLE_COMMANDS, "/tmp/project")
            call_args = mock_run.call_args
            prompt = call_args.kwargs.get("prompt") or call_args[0][0]
            assert "rebase" in prompt
            assert "implement" in prompt
            assert "rebase this please" in prompt

    def test_uses_lightweight_model(self):
        mock_output = '{"command": null, "context": ""}'
        p1, p2 = _patch_cli_and_prompt(mock_output)
        with p1 as mock_run, p2:
            classify_intent("hello", SAMPLE_COMMANDS, "/tmp/project")
            call_args = mock_run.call_args
            assert call_args.kwargs.get("model_key") == "lightweight"

    def test_ambiguous_returns_null_command(self):
        mock_output = '{"command": null, "context": ""}'
        p1, p2 = _patch_cli_and_prompt(mock_output)
        with p1, p2:
            result = classify_intent("hello there", SAMPLE_COMMANDS, "/tmp/project")
        assert result == {"command": None, "context": ""}

    def test_malformed_output_returns_none(self):
        p1, p2 = _patch_cli_and_prompt("I don't understand")
        # Override p1 with the actual bad output
        with patch("app.cli_provider.run_command", return_value="I don't understand"), p2:
            result = classify_intent("do something", SAMPLE_COMMANDS, "/tmp/project")
        assert result is None

    def test_missing_prompt_returns_none(self):
        with patch("app.prompts.load_prompt", return_value=None):
            result = classify_intent("fix this", SAMPLE_COMMANDS, "/tmp/project")
        assert result is None
