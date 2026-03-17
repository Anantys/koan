"""Tests for github_reply.py — AI-powered reply handler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.github_reply import (
    clean_reply,
    build_reply_prompt,
    extract_mention_text,
    fetch_thread_context,
    generate_reply,
    post_reply,
)


# ---------------------------------------------------------------------------
# extract_mention_text
# ---------------------------------------------------------------------------


class TestExtractMentionText:
    def test_simple_question(self):
        result = extract_mention_text("@bot what do you think?", "bot")
        assert result == "what do you think?"

    def test_with_context(self):
        result = extract_mention_text(
            "@koan-bot can you review this approach?", "koan-bot"
        )
        assert result == "can you review this approach?"

    def test_multiline_text(self):
        body = "@bot what do you think about\nthis change?\nIt looks complex."
        result = extract_mention_text(body, "bot")
        assert "what do you think about" in result
        assert "this change?" in result

    def test_no_mention(self):
        assert extract_mention_text("hello world", "bot") is None

    def test_mention_in_code_block(self):
        body = "```\n@bot rebase\n```"
        assert extract_mention_text(body, "bot") is None

    def test_mention_in_inline_code(self):
        body = "Use `@bot rebase` to trigger"
        assert extract_mention_text(body, "bot") is None

    def test_empty_body(self):
        assert extract_mention_text("", "bot") is None

    def test_none_body(self):
        assert extract_mention_text(None, "bot") is None

    def test_empty_nickname(self):
        assert extract_mention_text("@bot hello", "") is None

    def test_mention_with_no_text(self):
        assert extract_mention_text("@bot", "bot") is None

    def test_mention_with_only_whitespace(self):
        assert extract_mention_text("@bot   ", "bot") is None

    def test_case_insensitive(self):
        result = extract_mention_text("@BOT what do you think?", "bot")
        assert result == "what do you think?"

    def test_special_chars_in_nickname(self):
        result = extract_mention_text("@koan-bot hello", "koan-bot")
        assert result == "hello"

    def test_mention_after_text(self):
        body = "Hey team, @bot can you help with this?"
        result = extract_mention_text(body, "bot")
        assert result == "can you help with this?"


# ---------------------------------------------------------------------------
# fetch_thread_context
# ---------------------------------------------------------------------------


class TestFetchThreadContext:
    @patch("app.github_reply.api")
    def test_fetches_issue_context(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "Fix bug", "body": "Description here", "pull_request": None}),
            json.dumps([{"author": "alice", "body": "I agree"}]),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["title"] == "Fix bug"
        assert ctx["body"] == "Description here"
        assert ctx["is_pr"] is False
        assert len(ctx["comments"]) == 1
        assert ctx["comments"][0]["author"] == "alice"

    @patch("app.github_reply.api")
    def test_fetches_pr_context_with_files(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "Add feature", "body": "PR body", "pull_request": {"url": "..."}}),
            json.dumps([]),
            json.dumps([{"filename": "foo.py", "status": "modified", "additions": 10, "deletions": 2}]),
        ]
        ctx = fetch_thread_context("owner", "repo", "7")
        assert ctx["is_pr"] is True
        assert "foo.py" in ctx["diff_summary"]

    @patch("app.github_reply.api")
    def test_api_failure_returns_defaults(self, mock_api):
        mock_api.side_effect = RuntimeError("API down")
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["title"] == ""
        assert ctx["body"] == ""
        assert ctx["comments"] == []
        assert ctx["is_pr"] is False

    @patch("app.github_reply.api")
    def test_invalid_json_returns_defaults(self, mock_api):
        mock_api.side_effect = ["not json", "not json"]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["title"] == ""
        assert ctx["comments"] == []

    @patch("app.github_reply.api")
    def test_truncates_long_body(self, mock_api):
        long_body = "x" * 10000
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": long_body, "pull_request": None}),
            json.dumps([]),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert len(ctx["body"]) < len(long_body)
        assert "(truncated)" in ctx["body"]

    @patch("app.github_reply.api")
    def test_null_body_handled(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": None, "pull_request": None}),
            json.dumps([]),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["body"] == ""


# ---------------------------------------------------------------------------
# build_reply_prompt
# ---------------------------------------------------------------------------


class TestBuildReplyPrompt:
    @patch("app.github_reply.load_prompt")
    def test_builds_prompt_with_context(self, mock_load):
        mock_load.return_value = "prompt text"
        thread_context = {
            "title": "Fix bug",
            "body": "Description",
            "comments": [{"author": "alice", "body": "I agree"}],
            "is_pr": False,
            "diff_summary": "",
        }
        result = build_reply_prompt(
            "what do you think?", thread_context,
            "owner", "repo", "42", "alice",
        )
        assert result == "prompt text"
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["KIND"] == "issue"
        assert call_kwargs["QUESTION"] == "what do you think?"
        assert call_kwargs["AUTHOR"] == "alice"

    @patch("app.github_reply.load_prompt")
    def test_pr_kind(self, mock_load):
        mock_load.return_value = "prompt"
        thread_context = {
            "title": "PR title",
            "body": "",
            "comments": [],
            "is_pr": True,
            "diff_summary": "file.py +10/-2",
        }
        build_reply_prompt("question", thread_context, "o", "r", "1", "bob")
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["KIND"] == "pull request"
        assert call_kwargs["DIFF_SUMMARY"] == "file.py +10/-2"

    @patch("app.github_reply.load_prompt")
    def test_formats_comments(self, mock_load):
        mock_load.return_value = "prompt"
        thread_context = {
            "title": "T",
            "body": "B",
            "comments": [
                {"author": "alice", "body": "first"},
                {"author": "bob", "body": "second"},
            ],
            "is_pr": False,
            "diff_summary": "",
        }
        build_reply_prompt("q", thread_context, "o", "r", "1", "alice")
        call_kwargs = mock_load.call_args[1]
        assert "@alice: first" in call_kwargs["COMMENTS"]
        assert "@bob: second" in call_kwargs["COMMENTS"]


# ---------------------------------------------------------------------------
# generate_reply
# ---------------------------------------------------------------------------


class TestGenerateReply:
    @patch("app.github_reply.load_prompt", return_value="prompt")
    @patch("app.github_reply.run_command", return_value="This is my reply")
    def test_successful_reply(self, mock_run, mock_prompt):
        result = generate_reply(
            "what do you think?",
            {"title": "T", "body": "", "comments": [], "is_pr": False, "diff_summary": ""},
            "owner", "repo", "42", "alice", "/tmp/project",
        )
        assert result == "This is my reply"
        mock_run.assert_called_once()
        # Verify read-only tools
        call_args = mock_run.call_args
        assert call_args[1]["allowed_tools"] == ["Read", "Glob", "Grep"]
        assert call_args[1]["max_turns"] == 1

    @patch("app.github_reply.load_prompt", return_value="prompt")
    @patch("app.github_reply.run_command", side_effect=RuntimeError("timeout"))
    def test_failure_returns_none(self, mock_run, mock_prompt):
        result = generate_reply(
            "question", {"title": "", "body": "", "comments": [], "is_pr": False, "diff_summary": ""},
            "o", "r", "1", "a", "/tmp",
        )
        assert result is None

    @patch("app.github_reply.load_prompt", return_value="prompt")
    @patch("app.github_reply.run_command", return_value="")
    def test_empty_reply_returns_none(self, mock_run, mock_prompt):
        result = generate_reply(
            "q", {"title": "", "body": "", "comments": [], "is_pr": False, "diff_summary": ""},
            "o", "r", "1", "a", "/tmp",
        )
        assert result is None


# ---------------------------------------------------------------------------
# post_reply
# ---------------------------------------------------------------------------


class TestPostReply:
    @patch("app.github_reply.api")
    def test_successful_post(self, mock_api):
        assert post_reply("owner", "repo", "42", "My reply") is True
        mock_api.assert_called_once()
        args = mock_api.call_args
        assert args[0][0] == "repos/owner/repo/issues/42/comments"
        assert args[1]["method"] == "POST"

    @patch("app.github_reply.api", side_effect=RuntimeError("API error"))
    def test_failure_returns_false(self, mock_api):
        assert post_reply("owner", "repo", "42", "reply") is False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestCleanReply:
    def test_strips_whitespace(self):
        assert clean_reply("  hello  \n") == "hello"

    def test_removes_cli_noise(self):
        text = "Good reply\nError: Reached max turns (1)"
        assert clean_reply(text) == "Good reply"

    def test_preserves_normal_content(self):
        text = "Line 1\nLine 2\nLine 3"
        assert clean_reply(text) == text


class TestCleanReplyEdgeCases:
    """Additional edge cases for clean_reply."""

    def test_only_noise_lines_returns_empty(self):
        text = "Error: Reached max turns (1)\nError: Reached max turns (5)"
        assert clean_reply(text) == ""

    def test_empty_string(self):
        assert clean_reply("") == ""

    def test_multiline_with_noise_in_middle(self):
        text = "Good line\nError: Reached max turns (1)\nAnother good line"
        result = clean_reply(text)
        assert "Good line" in result
        assert "Another good line" in result
        assert "max turns" not in result


# ---------------------------------------------------------------------------
# extract_mention_text — additional edge cases
# ---------------------------------------------------------------------------


class TestExtractMentionTextEdgeCases:
    """Additional edge cases for mention extraction."""

    def test_mention_after_code_block(self):
        """Mention text after a code block should be extracted."""
        body = "```python\nfoo()\n```\n@bot what do you think?"
        result = extract_mention_text(body, "bot")
        assert result == "what do you think?"

    def test_multiple_mentions_takes_first(self):
        """Multiple @mentions should extract from the first one."""
        body = "@bot first question\n@bot second question"
        result = extract_mention_text(body, "bot")
        # With DOTALL, captures from first @bot to end
        assert "first question" in result

    def test_mention_with_regex_special_chars_in_nick(self):
        """Nicknames with regex special chars should be escaped properly."""
        body = "@bot.name what about this?"
        result = extract_mention_text(body, "bot.name")
        assert result == "what about this?"

    def test_mention_with_parentheses_in_nick(self):
        """Nickname with parentheses should be regex-safe."""
        result = extract_mention_text("@bot(1) hello", "bot(1)")
        assert result == "hello"


# ---------------------------------------------------------------------------
# fetch_thread_context — additional edge cases
# ---------------------------------------------------------------------------


class TestFetchThreadContextEdgeCases:
    """Additional edge cases for thread context fetching."""

    @patch("app.github_reply.api")
    def test_null_api_response(self, mock_api):
        """None response from API should return defaults."""
        mock_api.side_effect = [None, None]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["title"] == ""
        assert ctx["comments"] == []

    @patch("app.github_reply.api")
    def test_comments_not_a_list(self, mock_api):
        """If comments API returns non-list JSON, should handle gracefully."""
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B", "pull_request": None}),
            json.dumps({"error": "not a list"}),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["comments"] == []

    @patch("app.github_reply.api")
    def test_pr_files_not_a_list(self, mock_api):
        """If PR files API returns non-list JSON, diff_summary should be empty."""
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B", "pull_request": {"url": "..."}}),
            json.dumps([]),
            json.dumps({"error": "not a list"}),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert ctx["diff_summary"] == ""

    @patch("app.github_reply.api")
    def test_pr_with_many_files_capped_at_30(self, mock_api):
        """PR file list should be capped at 30 entries."""
        files = [
            {"filename": f"file{i}.py", "status": "modified", "additions": 1, "deletions": 0}
            for i in range(50)
        ]
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B", "pull_request": {"url": "..."}}),
            json.dumps([]),
            json.dumps(files),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        # Count lines in diff_summary
        lines = ctx["diff_summary"].strip().split("\n")
        assert len(lines) == 30

    @patch("app.github_reply.api")
    def test_comment_body_truncated(self, mock_api):
        """Individual comment bodies should be truncated."""
        long_body = "x" * 1000
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B", "pull_request": None}),
            json.dumps([{"author": "user", "body": long_body}]),
        ]
        ctx = fetch_thread_context("owner", "repo", "42")
        assert len(ctx["comments"][0]["body"]) < len(long_body)


# ---------------------------------------------------------------------------
# generate_reply — additional edge cases
# ---------------------------------------------------------------------------


class TestGenerateReplyEdgeCases:
    """Additional edge cases for reply generation."""

    @patch("app.github_reply.load_prompt", return_value="prompt")
    @patch("app.github_reply.run_command", return_value=None)
    def test_none_reply_returns_none(self, mock_run, mock_prompt):
        """run_command returning None should return None."""
        result = generate_reply(
            "q", {"title": "", "body": "", "comments": [], "is_pr": False, "diff_summary": ""},
            "o", "r", "1", "a", "/tmp",
        )
        assert result is None

    @patch("app.github_reply.load_prompt", return_value="prompt")
    @patch("app.github_reply.run_command",
           return_value="Good reply\nError: Reached max turns (1)")
    def test_reply_with_noise_is_cleaned(self, mock_run, mock_prompt):
        """Reply containing CLI noise should be cleaned."""
        result = generate_reply(
            "q", {"title": "", "body": "", "comments": [], "is_pr": False, "diff_summary": ""},
            "o", "r", "1", "a", "/tmp",
        )
        assert result == "Good reply"
        assert "max turns" not in result


# ---------------------------------------------------------------------------
# build_reply_prompt — edge cases
# ---------------------------------------------------------------------------


class TestBuildReplyPromptEdgeCases:
    """Edge cases for prompt building."""

    @patch("app.github_reply.load_prompt")
    def test_empty_comments_produces_empty_comments_text(self, mock_load):
        """No comments should result in empty COMMENTS field."""
        mock_load.return_value = "prompt"
        thread_context = {
            "title": "T", "body": "", "comments": [],
            "is_pr": False, "diff_summary": "",
        }
        build_reply_prompt("q", thread_context, "o", "r", "1", "alice")
        assert mock_load.call_args[1]["COMMENTS"] == ""

    @patch("app.github_reply.load_prompt")
    def test_missing_context_keys_use_defaults(self, mock_load):
        """Thread context with missing keys should use empty defaults."""
        mock_load.return_value = "prompt"
        # Minimal context — some keys missing
        thread_context = {}
        build_reply_prompt("q", thread_context, "o", "r", "1", "alice")
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["KIND"] == "issue"
        assert call_kwargs["TITLE"] == ""
        assert call_kwargs["BODY"] == ""
        assert call_kwargs["COMMENTS"] == ""
        assert call_kwargs["DIFF_SUMMARY"] == ""


class TestTruncateText:
    def test_short_text_unchanged(self):
        from app.utils import truncate_text
        assert truncate_text("hello", 100) == "hello"

    def test_long_text_truncated(self):
        from app.utils import truncate_text
        result = truncate_text("x" * 200, 100)
        assert len(result) < 200
        assert "(truncated)" in result
