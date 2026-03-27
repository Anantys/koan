"""Tests for review_runner.py — code review pipeline for PRs."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.review_runner import (
    build_review_prompt,
    fetch_repliable_comments,
    run_review,
    _detect_plan_url,
    _fetch_plan_body,
    _truncate_plan,
    _resolve_plan_body,
    _extract_review_body,
    _format_repliable_comments,
    _parse_review_json,
    _format_review_as_markdown,
    _extract_json_text,
    _post_review_comment,
    _post_comment_replies,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pr_context():
    """Minimal PR context dict matching fetch_pr_context output."""
    return {
        "title": "Fix auth bypass",
        "body": "Fixes #42 by adding proper token validation.",
        "branch": "fix-auth",
        "base": "main",
        "state": "OPEN",
        "author": "dev123",
        "url": "https://github.com/owner/repo/pull/42",
        "diff": "--- a/auth.py\n+++ b/auth.py\n@@ -1,3 +1,5 @@\n+import jwt\n",
        "review_comments": "",
        "reviews": "",
        "issue_comments": "",
    }


@pytest.fixture
def review_skill_dir(tmp_path):
    """Create a skill dir with a review prompt."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "review.md").write_text(
        "Review PR: {TITLE}\nAuthor: {AUTHOR}\nBranch: {BRANCH} -> {BASE}\n"
        "Body: {BODY}\nDiff: {DIFF}\n"
        "Reviews: {REVIEWS}\nComments: {REVIEW_COMMENTS}\n"
        "Issue: {ISSUE_COMMENTS}\n"
        "Repliable: {REPLIABLE_COMMENTS}\n"
    )
    return tmp_path


@pytest.fixture
def plan_review_skill_dir(tmp_path):
    """Create a skill dir with both review.md and review-with-plan.md prompts."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    base = (
        "{TITLE}\n{AUTHOR}\n{BRANCH}\n{BASE}\n{BODY}\n{DIFF}\n"
        "{REVIEWS}\n{REVIEW_COMMENTS}\n{ISSUE_COMMENTS}\n{REPLIABLE_COMMENTS}\n"
    )
    (prompts_dir / "review.md").write_text("Review PR: " + base)
    (prompts_dir / "review-with-plan.md").write_text("Plan Review: {PLAN}\n" + base)
    return tmp_path


# ---------------------------------------------------------------------------
# build_review_prompt
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    def test_with_skill_dir(self, pr_context, review_skill_dir):
        """Prompt is built from skill dir template."""
        prompt = build_review_prompt(pr_context, skill_dir=review_skill_dir)
        assert "Fix auth bypass" in prompt
        assert "dev123" in prompt
        assert "fix-auth" in prompt
        assert "main" in prompt

    def test_placeholders_substituted(self, pr_context, review_skill_dir):
        """All {PLACEHOLDER} values are substituted."""
        prompt = build_review_prompt(pr_context, skill_dir=review_skill_dir)
        assert "{TITLE}" not in prompt
        assert "{AUTHOR}" not in prompt
        assert "{BRANCH}" not in prompt
        assert "{BASE}" not in prompt
        assert "{BODY}" not in prompt
        assert "{DIFF}" not in prompt


# ---------------------------------------------------------------------------
# _extract_review_body
# ---------------------------------------------------------------------------

class TestExtractReviewBody:
    def test_extracts_new_format(self):
        """Extracts from ## PR Review onward (new format)."""
        raw = (
            "Some preamble\n\n"
            "## PR Review — Fix auth bypass\n\n"
            "Good PR. One blocking issue.\n\n"
            "### 🔴 Blocking\n\n**1. Missing check** (`auth.py`)\n"
        )
        result = _extract_review_body(raw)
        assert result.startswith("## PR Review")
        assert "Fix auth bypass" in result
        assert "preamble" not in result

    def test_extracts_legacy_format(self):
        """Extracts from ## Summary onward (legacy format)."""
        raw = "Some preamble\n\n## Summary\nLooks good.\n\n## Issues\nNone."
        result = _extract_review_body(raw)
        assert result.startswith("## Summary")
        assert "Looks good." in result
        assert "preamble" not in result

    def test_prefers_new_format_over_legacy(self):
        """When both formats present, prefers ## PR Review."""
        raw = (
            "## PR Review — Title\nSummary here.\n\n"
            "## Summary\nLegacy section."
        )
        result = _extract_review_body(raw)
        assert result.startswith("## PR Review")

    def test_fallback_to_full_output(self):
        """When no structured format found, returns full text."""
        raw = "This is a freeform review. Code is fine."
        result = _extract_review_body(raw)
        assert result == raw

    def test_empty_input(self):
        """Empty input returns empty string."""
        assert _extract_review_body("") == ""

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is removed."""
        raw = "  \n## PR Review — Test\nClean code.\n  "
        result = _extract_review_body(raw)
        assert result.startswith("## PR Review")
        assert not result.endswith(" ")

    def test_raw_json_gets_formatted(self):
        """When output is raw JSON, converts to markdown instead of posting JSON."""
        raw = json.dumps(VALID_REVIEW_JSON)
        result = _extract_review_body(raw)
        assert "## PR Review" in result
        assert "### 🔴 Blocking" in result
        assert "Missing validation" in result
        assert '"file_comments"' not in result  # No raw JSON keys

    def test_json_with_preamble_gets_formatted(self):
        """When output has JSON with preamble, extracts and formats it."""
        raw = "Here is my review:\n\n" + json.dumps(VALID_REVIEW_JSON)
        result = _extract_review_body(raw)
        assert "## PR Review" in result
        assert '"file_comments"' not in result

    def test_captures_checklist_section(self):
        """Extracts review body including ### Checklist section."""
        raw = (
            "Some preamble\n\n"
            "## PR Review — Add validation\n\n"
            "One issue found.\n\n"
            "### 🔴 Blocking\n\n"
            "**1. Missing input validation** (`api.py`, `handle_request`)\n"
            "No validation on user input.\n\n"
            "---\n\n"
            "### Checklist\n\n"
            "- [x] No hardcoded secrets\n"
            "- [x] Error paths handle cleanup\n"
            "- [ ] Missing input validation at API boundary (see 🔴 #1)\n"
            "- [x] Tests cover new branches\n\n"
            "---\n\n"
            "### Summary\n\n"
            "Needs input validation before merge."
        )
        result = _extract_review_body(raw)
        assert result.startswith("## PR Review")
        assert "### Checklist" in result
        assert "- [ ] Missing input validation" in result
        assert "### Summary" in result
        assert "preamble" not in result


# ---------------------------------------------------------------------------
# _extract_json_text
# ---------------------------------------------------------------------------

class TestExtractJsonText:
    def test_pure_json(self):
        result = _extract_json_text('{"a": 1}')
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_json_in_fences(self):
        result = _extract_json_text('```json\n{"a": 1}\n```')
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_json_in_plain_fences(self):
        result = _extract_json_text('```\n{"a": 1}\n```')
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_json_with_preamble(self):
        text = 'Here is my review:\n\n{"file_comments": [], "review_summary": {"lgtm": true, "summary": "ok", "checklist": []}}'
        result = _extract_json_text(text)
        assert result is not None
        data = json.loads(result)
        assert data["review_summary"]["lgtm"] is True

    def test_json_with_preamble_and_postamble(self):
        text = 'I analyzed the code:\n\n{"a": 1}\n\nHope this helps!'
        result = _extract_json_text(text)
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_json_fences_with_surrounding_text(self):
        text = 'Here is the review:\n\n```json\n{"a": 1}\n```\n\nLet me know if you need more.'
        result = _extract_json_text(text)
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_no_json(self):
        result = _extract_json_text("This is plain text with no JSON.")
        assert result is None

    def test_whitespace_only(self):
        result = _extract_json_text("   \n  ")
        assert result is None

    def test_nested_braces(self):
        obj = {"outer": {"inner": {"deep": 42}}}
        text = f"Preamble\n{json.dumps(obj)}\nPostamble"
        result = _extract_json_text(text)
        assert result is not None
        assert json.loads(result) == obj

    def test_review_json_with_preamble(self):
        """The exact bug scenario: valid review JSON with Claude preamble."""
        text = "I'll provide my review as JSON:\n\n" + json.dumps(VALID_REVIEW_JSON)
        result = _extract_json_text(text)
        assert result is not None
        data = json.loads(result)
        assert "file_comments" in data
        assert "review_summary" in data


# ---------------------------------------------------------------------------
# _parse_review_json
# ---------------------------------------------------------------------------

VALID_REVIEW_JSON = {
    "file_comments": [
        {
            "file": "auth.py",
            "line_start": 42,
            "line_end": 42,
            "severity": "critical",
            "title": "Missing validation",
            "comment": "No input validation.",
            "code_snippet": "",
        },
    ],
    "review_summary": {
        "lgtm": False,
        "summary": "Needs validation before merge.",
        "checklist": [
            {"item": "No hardcoded secrets", "passed": True, "finding_ref": ""},
        ],
    },
}

LGTM_REVIEW_JSON = {
    "file_comments": [],
    "review_summary": {
        "lgtm": True,
        "summary": "Clean code. Merge-ready.",
        "checklist": [],
    },
}


class TestParseReviewJson:
    def test_valid_json(self):
        raw = json.dumps(VALID_REVIEW_JSON)
        result = _parse_review_json(raw)
        assert result is not None
        assert result["review_summary"]["lgtm"] is False
        assert len(result["file_comments"]) == 1

    def test_valid_json_in_fences(self):
        raw = f"```json\n{json.dumps(VALID_REVIEW_JSON)}\n```"
        result = _parse_review_json(raw)
        assert result is not None

    def test_lgtm_review(self):
        raw = json.dumps(LGTM_REVIEW_JSON)
        result = _parse_review_json(raw)
        assert result is not None
        assert result["review_summary"]["lgtm"] is True
        assert result["file_comments"] == []

    def test_invalid_json(self):
        result = _parse_review_json("not json at all")
        assert result is None

    def test_truncated_json(self):
        raw = '{"file_comments": [{"file": "a.py"'
        result = _parse_review_json(raw)
        assert result is None

    def test_valid_json_but_wrong_schema(self):
        raw = json.dumps({"unrelated": "data"})
        result = _parse_review_json(raw)
        assert result is None

    def test_missing_severity(self):
        data = {
            "file_comments": [{
                "file": "a.py", "line_start": 1, "line_end": 1,
                "severity": "invalid_severity",
                "title": "t", "comment": "c", "code_snippet": "",
            }],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }
        result = _parse_review_json(json.dumps(data))
        assert result is None

    def test_markdown_text_returns_none(self):
        raw = "## PR Review — Title\n\nGood code.\n\n### Summary\n\nLGTM."
        result = _parse_review_json(raw)
        assert result is None

    def test_json_with_preamble_text(self):
        """Parses valid JSON even when surrounded by preamble text."""
        raw = "Here is my analysis:\n\n" + json.dumps(VALID_REVIEW_JSON)
        result = _parse_review_json(raw)
        assert result is not None
        assert result["review_summary"]["lgtm"] is False

    def test_json_with_preamble_and_postamble(self):
        """Parses valid JSON with both preamble and postamble."""
        raw = (
            "I've analyzed the code changes.\n\n"
            + json.dumps(LGTM_REVIEW_JSON)
            + "\n\nLet me know if you need more details."
        )
        result = _parse_review_json(raw)
        assert result is not None
        assert result["review_summary"]["lgtm"] is True

    def test_json_in_fences_with_surrounding_text(self):
        """Parses JSON from code fences embedded in surrounding text."""
        raw = (
            "Here is the review:\n\n"
            f"```json\n{json.dumps(VALID_REVIEW_JSON)}\n```\n\n"
            "Hope this helps."
        )
        result = _parse_review_json(raw)
        assert result is not None


# ---------------------------------------------------------------------------
# _format_review_as_markdown
# ---------------------------------------------------------------------------

class TestFormatReviewAsMarkdown:
    def test_formats_with_findings(self):
        md = _format_review_as_markdown(VALID_REVIEW_JSON, title="Fix auth")
        assert "## PR Review — Fix auth" in md
        assert "### 🔴 Blocking" in md
        assert "Missing validation" in md
        assert "`auth.py`" in md
        assert "L42" in md
        assert "### Summary" in md

    def test_lgtm_review(self):
        md = _format_review_as_markdown(LGTM_REVIEW_JSON)
        assert "## PR Review" in md
        assert "### 🔴" not in md
        assert "### 🟡" not in md
        assert "### 🟢" not in md
        assert "Merge-ready" in md

    def test_all_severity_levels(self):
        data = {
            "file_comments": [
                {"file": "a.py", "line_start": 1, "line_end": 1,
                 "severity": "critical", "title": "Bug", "comment": "Fix it",
                 "code_snippet": ""},
                {"file": "b.py", "line_start": 10, "line_end": 15,
                 "severity": "warning", "title": "Perf", "comment": "Slow",
                 "code_snippet": "for x in y"},
                {"file": "c.py", "line_start": 0, "line_end": 0,
                 "severity": "suggestion", "title": "Style", "comment": "Rename",
                 "code_snippet": ""},
            ],
            "review_summary": {"lgtm": False, "summary": "Needs work.",
                               "checklist": []},
        }
        md = _format_review_as_markdown(data)
        assert "### 🔴 Blocking" in md
        assert "### 🟡 Important" in md
        assert "### 🟢 Suggestions" in md
        assert "L10-15" in md  # multi-line range

    def test_checklist_rendering(self):
        data = {
            "file_comments": [],
            "review_summary": {
                "lgtm": True,
                "summary": "Good.",
                "checklist": [
                    {"item": "No secrets", "passed": True, "finding_ref": ""},
                    {"item": "Input validated", "passed": False, "finding_ref": "critical #1"},
                ],
            },
        }
        md = _format_review_as_markdown(data)
        assert "- [x] No secrets" in md
        assert "- [ ] Input validated — critical #1" in md

    def test_code_snippet_in_output(self):
        data = {
            "file_comments": [{
                "file": "x.py", "line_start": 5, "line_end": 5,
                "severity": "warning", "title": "Issue",
                "comment": "Problem here",
                "code_snippet": "x = eval(input())",
            }],
            "review_summary": {"lgtm": False, "summary": "Fix eval.",
                               "checklist": []},
        }
        md = _format_review_as_markdown(data)
        assert "x = eval(input())" in md
        assert "```" in md


# ---------------------------------------------------------------------------
# _post_review_comment
# ---------------------------------------------------------------------------

class TestPostReviewComment:
    @patch("app.review_runner.run_gh")
    def test_posts_comment(self, mock_gh):
        """Posts review as PR comment via gh CLI."""
        result = _post_review_comment("owner", "repo", "42", "LGTM")
        assert result is True
        mock_gh.assert_called_once()
        call_args = mock_gh.call_args
        assert "pr" in call_args[0]
        assert "comment" in call_args[0]
        assert "42" in call_args[0]
        # Body should contain the review text
        body_arg = call_args[1].get("body") or call_args[0][-1]
        # The body is passed via --body flag
        assert any("LGTM" in str(a) for a in call_args[0])

    @patch("app.review_runner.run_gh", side_effect=RuntimeError("API error"))
    def test_returns_false_on_error(self, mock_gh):
        """Returns False when gh CLI fails."""
        result = _post_review_comment("owner", "repo", "42", "review")
        assert result is False

    @patch("app.review_runner.run_gh")
    def test_truncates_long_review(self, mock_gh):
        """Reviews longer than 60000 chars are truncated."""
        long_review = "x" * 70000
        _post_review_comment("owner", "repo", "42", long_review)
        call_args = mock_gh.call_args[0]
        body = [a for a in call_args if isinstance(a, str) and len(a) > 1000][0]
        assert len(body) < 65000
        assert "truncated" in body.lower()

    @patch("app.review_runner.run_gh")
    def test_no_double_heading_for_structured_review(self, mock_gh):
        """Reviews starting with ## don't get an extra ## Code Review header."""
        review = "## PR Review — Fix auth\n\nLGTM"
        _post_review_comment("owner", "repo", "42", review)
        call_args = mock_gh.call_args[0]
        body = [a for a in call_args if isinstance(a, str) and "LGTM" in a][0]
        assert "## Code Review" not in body
        assert body.startswith("## PR Review")

    @patch("app.review_runner.run_gh")
    def test_legacy_review_gets_heading(self, mock_gh):
        """Reviews without ## heading get wrapped with ## Code Review."""
        review = "Looks good, no issues found."
        _post_review_comment("owner", "repo", "42", review)
        call_args = mock_gh.call_args[0]
        body = [a for a in call_args if isinstance(a, str) and "Looks good" in a][0]
        assert "## Code Review" in body


# ---------------------------------------------------------------------------
# run_review (integration, mocked externals)
# ---------------------------------------------------------------------------

class TestRunReview:
    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_full_pipeline_with_json(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """Full review pipeline with JSON output: fetch -> claude -> parse -> post."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = (json.dumps(LGTM_REVIEW_JSON), "")
        mock_notify = MagicMock()

        success, summary, review_data = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is True
        assert "42" in summary
        assert review_data is not None
        assert review_data["review_summary"]["lgtm"] is True
        mock_fetch.assert_called_once_with("owner", "repo", "42")
        mock_claude.assert_called_once()
        mock_gh.assert_called_once()  # post comment
        assert mock_notify.call_count >= 2

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_fallback_to_markdown_on_invalid_json(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """Falls back to regex extraction when JSON parsing fails twice."""
        mock_fetch.return_value = pr_context
        # Both attempts return markdown instead of JSON
        mock_claude.return_value = (
            "## PR Review — Fix auth bypass\n\n"
            "Solid fix. No issues found.\n\n---\n\n"
            "### Summary\n\nMerge-ready.",
            "",
        )
        mock_notify = MagicMock()

        success, summary, review_data = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is True
        assert review_data is None  # fallback was used
        # Claude called twice (initial + retry)
        assert mock_claude.call_count == 2
        mock_gh.assert_called_once()

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_retry_succeeds_on_second_attempt(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """Retry produces valid JSON on second attempt."""
        mock_fetch.return_value = pr_context
        # First call returns markdown, second returns JSON
        mock_claude.side_effect = [
            ("Not JSON at all", ""),
            (json.dumps(VALID_REVIEW_JSON), ""),
        ]
        mock_notify = MagicMock()

        success, summary, review_data = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is True
        assert review_data is not None
        assert review_data["review_summary"]["lgtm"] is False
        assert mock_claude.call_count == 2

    @patch("app.review_runner.fetch_pr_context")
    def test_fetch_failure(self, mock_fetch, pr_context):
        """Handles PR context fetch failure."""
        mock_fetch.side_effect = RuntimeError("API down")
        mock_notify = MagicMock()

        success, summary, _rd = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
        )

        assert success is False
        assert "Failed to fetch" in summary

    @patch("app.review_runner.fetch_pr_context")
    def test_empty_diff(self, mock_fetch, pr_context):
        """Returns failure when PR has no diff."""
        pr_context["diff"] = ""
        mock_fetch.return_value = pr_context
        mock_notify = MagicMock()

        success, summary, _rd = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
        )

        assert success is False
        assert "no diff" in summary

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_claude_empty_output(
        self, mock_fetch, mock_claude, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """Returns failure when Claude produces no output."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = ("", "Timeout (300s)")
        mock_notify = MagicMock()

        success, summary, _rd = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is False
        assert "failed" in summary.lower()
        assert "Timeout" in summary

    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_claude_failure_without_error_detail(
        self, mock_fetch, mock_claude, pr_context, review_skill_dir,
    ):
        """Failure without error detail still reports cleanly."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = ("", "")
        mock_notify = MagicMock()

        success, summary, _rd = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is False
        assert "failed" in summary.lower()
        # No error detail — message should not contain "()"
        assert "()" not in summary

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh", side_effect=RuntimeError("post fail"))
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_comment_post_failure(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """Handles comment posting failure."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = ("## PR Review — Fix auth bypass\n\nGood code", "")
        mock_notify = MagicMock()

        success, summary, _rd = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is False
        assert "failed to post" in summary.lower()


# ---------------------------------------------------------------------------
# _run_claude_review
# ---------------------------------------------------------------------------

class TestRunClaudeReview:
    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--test"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_success_returns_output_and_empty_error(
        self, mock_config, mock_build, mock_claude,
    ):
        """On success, returns (output, empty error)."""
        from app.review_runner import _run_claude_review

        mock_claude.return_value = {"success": True, "output": "review text", "error": ""}
        output, error = _run_claude_review("prompt", "/tmp/project")
        assert output == "review text"
        assert error == ""

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--test"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_failure_returns_error_detail(
        self, mock_config, mock_build, mock_claude,
    ):
        """On failure, returns empty output and error detail."""
        from app.review_runner import _run_claude_review

        mock_claude.return_value = {
            "success": False, "output": "", "error": "Timeout (300s)",
        }
        output, error = _run_claude_review("prompt", "/tmp/project")
        assert output == ""
        assert "Timeout" in error

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--test"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_failure_logs_to_stderr(
        self, mock_config, mock_build, mock_claude, capsys,
    ):
        """Failure is logged to stderr for diagnostics."""
        from app.review_runner import _run_claude_review

        mock_claude.return_value = {
            "success": False, "output": "", "error": "Exit code 1: model error",
        }
        _run_claude_review("prompt", "/tmp/project")
        captured = capsys.readouterr()
        assert "Claude review failed" in captured.err
        assert "Exit code 1" in captured.err

    @patch("app.claude_step.run_claude")
    @patch("app.cli_provider.build_full_command", return_value=["claude", "--test"])
    @patch("app.config.get_model_config", return_value={"mission": "m", "fallback": "f"})
    def test_default_timeout_is_600(
        self, mock_config, mock_build, mock_claude,
    ):
        """Default timeout increased from 300 to 600 for large PRs."""
        from app.review_runner import _run_claude_review

        mock_claude.return_value = {"success": True, "output": "ok", "error": ""}
        _run_claude_review("prompt", "/tmp/project")
        # Verify run_claude was called with timeout=600
        _, kwargs = mock_claude.call_args
        assert kwargs.get("timeout") == 600


# ---------------------------------------------------------------------------
# main() CLI entry point
# ---------------------------------------------------------------------------

class TestMainCli:
    @patch("app.review_runner.run_review")
    def test_valid_pr_url(self, mock_run):
        """CLI parses PR URL and calls run_review."""
        from app.review_runner import main

        mock_run.return_value = (True, "Review posted.", None)
        exit_code = main([
            "https://github.com/owner/repo/pull/42",
            "--project-path", "/tmp/project",
        ])

        assert exit_code == 0
        mock_run.assert_called_once_with(
            "owner", "repo", "42", "/tmp/project",
            skill_dir=Path(__file__).resolve().parent.parent / "skills" / "core" / "review",
            architecture=False,
            plan_url=None,
        )

    @patch("app.review_runner.run_review")
    def test_failure_returns_1(self, mock_run):
        """CLI returns exit code 1 on review failure."""
        from app.review_runner import main

        mock_run.return_value = (False, "Claude failed.", None)
        exit_code = main([
            "https://github.com/owner/repo/pull/42",
            "--project-path", "/tmp/project",
        ])

        assert exit_code == 1

    def test_invalid_url(self):
        """CLI returns exit code 1 for non-PR URLs."""
        from app.review_runner import main

        exit_code = main([
            "https://github.com/owner/repo/issues/42",
            "--project-path", "/tmp/project",
        ])
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Skill dispatch integration
# ---------------------------------------------------------------------------

class TestArchitectureFlag:
    """Tests for the --architecture flag."""

    def test_cli_parses_architecture_flag(self):
        """CLI parses --architecture flag."""
        from app.review_runner import main

        with patch("app.review_runner.run_review") as mock_run:
            mock_run.return_value = (True, "Review posted.", None)
            main([
                "https://github.com/owner/repo/pull/42",
                "--project-path", "/tmp/project",
                "--architecture",
            ])

            call_kwargs = mock_run.call_args
            assert call_kwargs[1].get("architecture") is True or (
                len(call_kwargs[0]) >= 6 and call_kwargs[0][5] is True
            )

    def test_cli_default_no_architecture(self):
        """CLI defaults to no architecture flag."""
        from app.review_runner import main

        with patch("app.review_runner.run_review") as mock_run:
            mock_run.return_value = (True, "Review posted.", None)
            main([
                "https://github.com/owner/repo/pull/42",
                "--project-path", "/tmp/project",
            ])

            _, kwargs = mock_run.call_args
            assert kwargs.get("architecture") is False or "architecture" not in kwargs

    def test_build_prompt_architecture_selects_correct_template(
        self, pr_context, tmp_path,
    ):
        """build_review_prompt with architecture=True loads review-architecture template."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review-architecture.md").write_text(
            "ARCH REVIEW: {TITLE}\nAuthor: {AUTHOR}\nBranch: {BRANCH} -> {BASE}\n"
            "Body: {BODY}\nDiff: {DIFF}\n"
            "Reviews: {REVIEWS}\nComments: {REVIEW_COMMENTS}\n"
            "Issue: {ISSUE_COMMENTS}\n"
        )

        prompt = build_review_prompt(
            pr_context, skill_dir=tmp_path, architecture=True,
        )
        assert "ARCH REVIEW:" in prompt
        assert "Fix auth bypass" in prompt

    def test_build_prompt_default_selects_review_template(
        self, pr_context, review_skill_dir,
    ):
        """build_review_prompt without architecture uses standard review template."""
        prompt = build_review_prompt(
            pr_context, skill_dir=review_skill_dir, architecture=False,
        )
        assert "Review PR:" in prompt
        assert "{TITLE}" not in prompt

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_run_review_passes_architecture_to_prompt(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, tmp_path,
    ):
        """run_review with architecture=True uses architecture prompt."""
        # Set up skill dir with both prompts
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review.md").write_text(
            "STANDARD: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} "
            "{REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}"
        )
        (prompts_dir / "review-architecture.md").write_text(
            "ARCHITECTURE: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} "
            "{REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}"
        )

        mock_fetch.return_value = pr_context
        mock_claude.return_value = ("## PR Review — Fix auth bypass\n\nGood", "")
        mock_notify = MagicMock()

        run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=tmp_path,
            architecture=True,
        )

        # Verify the architecture prompt was passed to Claude
        prompt_arg = mock_claude.call_args[0][0]
        assert "ARCHITECTURE:" in prompt_arg
        assert "STANDARD:" not in prompt_arg


class TestSkillDispatchIntegration:
    """Verify /review is properly wired in skill_dispatch.py."""

    def test_review_in_skill_runners(self):
        """'review' is registered in _SKILL_RUNNERS."""
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "review" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["review"] == "app.review_runner"

    def test_review_mission_is_skill(self):
        """is_skill_mission recognizes /review missions."""
        from app.skill_dispatch import is_skill_mission
        assert is_skill_mission("/review https://github.com/o/r/pull/1")

    def test_review_parse(self):
        """parse_skill_mission extracts review command."""
        from app.skill_dispatch import parse_skill_mission
        project, cmd, args = parse_skill_mission(
            "[project:koan] /review https://github.com/o/r/pull/1"
        )
        assert project == "koan"
        assert cmd == "review"
        assert "pull/1" in args

    def test_review_validate_args_valid(self):
        """validate_skill_args accepts PR URL for /review."""
        from app.skill_dispatch import validate_skill_args
        result = validate_skill_args(
            "review", "https://github.com/o/r/pull/1"
        )
        assert result is None

    def test_review_validate_args_invalid(self):
        """validate_skill_args rejects non-PR URLs for /review."""
        from app.skill_dispatch import validate_skill_args
        result = validate_skill_args("review", "no url here")
        assert result is not None
        assert "PR URL" in result

    @patch("app.skill_dispatch.is_known_project", return_value=True)
    def test_dispatch_builds_command(self, mock_known):
        """dispatch_skill_mission builds a command for /review."""
        from app.skill_dispatch import dispatch_skill_mission
        result = dispatch_skill_mission(
            mission_text="/review https://github.com/o/r/pull/1",
            project_name="koan",
            project_path="/tmp/project",
            koan_root="/tmp/koan",
            instance_dir="/tmp/instance",
        )
        assert result is not None
        assert any("review_runner" in str(p) for p in result)
        assert "/tmp/project" in result

    @patch("app.skill_dispatch.is_known_project", return_value=True)
    def test_dispatch_passes_architecture_flag(self, mock_known):
        """dispatch_skill_mission passes --architecture when present."""
        from app.skill_dispatch import dispatch_skill_mission
        result = dispatch_skill_mission(
            mission_text="/review https://github.com/o/r/pull/1 --architecture",
            project_name="koan",
            project_path="/tmp/project",
            koan_root="/tmp/koan",
            instance_dir="/tmp/instance",
        )
        assert result is not None
        assert "--architecture" in result

    @patch("app.skill_dispatch.is_known_project", return_value=True)
    def test_dispatch_no_architecture_by_default(self, mock_known):
        """dispatch_skill_mission does not include --architecture by default."""
        from app.skill_dispatch import dispatch_skill_mission
        result = dispatch_skill_mission(
            mission_text="/review https://github.com/o/r/pull/1",
            project_name="koan",
            project_path="/tmp/project",
            koan_root="/tmp/koan",
            instance_dir="/tmp/instance",
        )
        assert result is not None
        assert "--architecture" not in result

    @patch("app.skill_dispatch.is_known_project", return_value=True)
    def test_dispatch_architecture_before_url(self, mock_known):
        """dispatch_skill_mission handles --architecture before URL."""
        from app.skill_dispatch import dispatch_skill_mission
        result = dispatch_skill_mission(
            mission_text="/review --architecture https://github.com/o/r/pull/1",
            project_name="koan",
            project_path="/tmp/project",
            koan_root="/tmp/koan",
            instance_dir="/tmp/instance",
        )
        assert result is not None
        assert "--architecture" in result
        assert any("pull/1" in str(p) for p in result)


# ---------------------------------------------------------------------------
# fetch_repliable_comments
# ---------------------------------------------------------------------------

class TestFetchRepliableComments:
    @patch("app.review_runner.run_gh")
    def test_fetches_review_and_issue_comments(self, mock_gh):
        """Fetches both review comments and issue comments with IDs."""
        review_json = json.dumps({
            "id": 100, "user": "alice", "body": "Why this approach?",
            "path": "auth.py", "line": 42, "user_type": "User",
        })
        issue_json = json.dumps({
            "id": 200, "user": "bob", "body": "Looks good overall",
            "user_type": "User",
        })
        mock_gh.side_effect = [review_json, issue_json]

        comments = fetch_repliable_comments("owner", "repo", "42")

        assert len(comments) == 2
        assert comments[0]["id"] == 100
        assert comments[0]["type"] == "review_comment"
        assert comments[0]["user"] == "alice"
        assert comments[0]["path"] == "auth.py"
        assert comments[1]["id"] == 200
        assert comments[1]["type"] == "issue_comment"

    @patch("app.review_runner.run_gh")
    def test_skips_bot_comments(self, mock_gh):
        """Bot comments are excluded from repliable list."""
        review_json = json.dumps({
            "id": 100, "user": "github-actions", "body": "CI passed",
            "path": "x.py", "line": 1, "user_type": "Bot",
        })
        mock_gh.side_effect = [review_json, ""]

        comments = fetch_repliable_comments("owner", "repo", "42")

        assert len(comments) == 0

    @patch("app.review_runner.run_gh", side_effect=RuntimeError("API error"))
    def test_handles_api_errors(self, mock_gh):
        """Returns empty list on API errors."""
        comments = fetch_repliable_comments("owner", "repo", "42")
        assert comments == []

    @patch("app.review_runner.run_gh")
    def test_empty_response(self, mock_gh):
        """Handles empty responses gracefully."""
        mock_gh.return_value = ""
        comments = fetch_repliable_comments("owner", "repo", "42")
        assert comments == []


# ---------------------------------------------------------------------------
# _format_repliable_comments
# ---------------------------------------------------------------------------

class TestFormatRepliableComments:
    def test_no_comments(self):
        result = _format_repliable_comments([])
        assert "No comments" in result

    def test_formats_review_comment(self):
        comments = [{
            "id": 100, "type": "review_comment", "user": "alice",
            "body": "Why this approach?", "path": "auth.py", "line": 42,
        }]
        result = _format_repliable_comments(comments)
        assert "[id=100]" in result
        assert "@alice" in result
        assert "auth.py:42" in result
        assert "Why this approach?" in result

    def test_formats_issue_comment(self):
        comments = [{
            "id": 200, "type": "issue_comment", "user": "bob",
            "body": "Overall this looks good",
        }]
        result = _format_repliable_comments(comments)
        assert "[id=200]" in result
        assert "@bob" in result
        assert "Overall this looks good" in result

    def test_truncates_long_bodies(self):
        comments = [{
            "id": 300, "type": "issue_comment", "user": "carol",
            "body": "x" * 600,
        }]
        result = _format_repliable_comments(comments)
        assert "..." in result
        assert len(result) < 700


# ---------------------------------------------------------------------------
# _post_comment_replies
# ---------------------------------------------------------------------------

class TestPostCommentReplies:
    @patch("app.review_runner.run_gh")
    def test_posts_review_comment_reply(self, mock_gh):
        """Replies to review comments via the pull request comment API."""
        replies = [{"comment_id": 100, "reply": "Good question — see L42."}]
        repliable = [{"id": 100, "type": "review_comment", "user": "alice", "body": "Why?"}]

        count = _post_comment_replies("owner", "repo", "42", replies, repliable)

        assert count == 1
        call_args = mock_gh.call_args[0]
        assert "repos/owner/repo/pulls/42/comments" in call_args[1]
        assert "-X" in call_args
        assert "POST" in call_args

    @patch("app.review_runner.run_gh")
    def test_posts_issue_comment_reply(self, mock_gh):
        """Replies to issue comments via gh pr comment with quote."""
        replies = [{"comment_id": 200, "reply": "Thanks for the feedback."}]
        repliable = [{"id": 200, "type": "issue_comment", "user": "bob", "body": "Nice work"}]

        count = _post_comment_replies("owner", "repo", "42", replies, repliable)

        assert count == 1
        call_args = mock_gh.call_args[0]
        assert "pr" in call_args
        assert "comment" in call_args
        # Body should contain quote of original
        body = [a for a in call_args if isinstance(a, str) and "@bob" in a][0]
        assert "> @bob:" in body

    def test_empty_replies(self):
        """No-op when replies list is empty."""
        count = _post_comment_replies("owner", "repo", "42", [], [])
        assert count == 0

    @patch("app.review_runner.run_gh")
    def test_skips_unknown_comment_id(self, mock_gh):
        """Skips replies targeting non-existent comment IDs."""
        replies = [{"comment_id": 999, "reply": "Reply to nothing"}]
        repliable = [{"id": 100, "type": "issue_comment", "user": "alice", "body": "Hello"}]

        count = _post_comment_replies("owner", "repo", "42", replies, repliable)

        assert count == 0
        mock_gh.assert_not_called()

    @patch("app.review_runner.run_gh", side_effect=RuntimeError("API error"))
    def test_handles_post_failure(self, mock_gh):
        """Continues posting other replies when one fails."""
        replies = [{"comment_id": 100, "reply": "Reply"}]
        repliable = [{"id": 100, "type": "issue_comment", "user": "a", "body": "b"}]

        count = _post_comment_replies("owner", "repo", "42", replies, repliable)

        assert count == 0

    @patch("app.review_runner.run_gh")
    def test_skips_empty_reply(self, mock_gh):
        """Skips replies with empty text."""
        replies = [{"comment_id": 100, "reply": ""}]
        repliable = [{"id": 100, "type": "issue_comment", "user": "a", "body": "b"}]

        count = _post_comment_replies("owner", "repo", "42", replies, repliable)

        assert count == 0
        mock_gh.assert_not_called()


# ---------------------------------------------------------------------------
# run_review with comment replies
# ---------------------------------------------------------------------------

class TestRunReviewWithReplies:
    @patch("app.review_runner.fetch_repliable_comments")
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_posts_replies_when_present(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """Posts replies to user comments when review includes comment_replies."""
        mock_fetch.return_value = pr_context
        review_with_replies = {
            **LGTM_REVIEW_JSON,
            "comment_replies": [
                {"comment_id": 100, "reply": "Good question — the reason is X."},
            ],
        }
        mock_claude.return_value = (json.dumps(review_with_replies), "")
        mock_repliable.return_value = [
            {"id": 100, "type": "review_comment", "user": "alice", "body": "Why?"},
        ]
        mock_notify = MagicMock()

        success, summary, review_data = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is True
        assert "Replied to 1 comment" in summary
        # run_gh called: 1 for post_review_comment + 1 for reply
        assert mock_gh.call_count == 2

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_no_replies_when_no_repliable_comments(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """No reply posting when there are no repliable comments."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = (json.dumps(LGTM_REVIEW_JSON), "")
        mock_notify = MagicMock()

        success, summary, _ = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is True
        assert "Replied" not in summary
        mock_gh.assert_called_once()  # Only the review comment post


# ---------------------------------------------------------------------------
# Plan alignment — _detect_plan_url
# ---------------------------------------------------------------------------

class TestDetectPlanUrl:
    def test_finds_issue_url_in_body(self):
        """Extracts the first GitHub issue URL from a PR body."""
        body = "Implements https://github.com/owner/repo/issues/42 as requested."
        result = _detect_plan_url(body)
        assert result == "https://github.com/owner/repo/issues/42"

    def test_returns_none_when_no_issue_url(self):
        """Returns None when the PR body has no issue URL."""
        body = "This PR fixes a bug. No linked issue."
        assert _detect_plan_url(body) is None

    def test_ignores_pr_urls(self):
        """PR URLs (/pull/) are not matched — only issue URLs."""
        body = "Closes https://github.com/owner/repo/pull/10 and updates docs."
        assert _detect_plan_url(body) is None

    def test_returns_first_issue_url_when_multiple(self):
        """Returns the first issue URL when multiple are present."""
        body = (
            "From https://github.com/owner/repo/issues/10 "
            "and https://github.com/owner/repo/issues/20"
        )
        result = _detect_plan_url(body)
        assert result == "https://github.com/owner/repo/issues/10"

    def test_empty_body(self):
        """Empty PR body returns None."""
        assert _detect_plan_url("") is None

    def test_closes_shorthand_not_matched(self):
        """'Closes #42' shorthand (no full URL) returns None."""
        body = "Closes #42."
        assert _detect_plan_url(body) is None

    def test_issue_url_in_multiline_body(self):
        """Finds issue URL in a multi-line PR body."""
        body = (
            "## Summary\n\n"
            "This PR implements the plan.\n\n"
            "Closes https://github.com/acme/app/issues/99\n\n"
            "## Changes\n\n- Added feature\n"
        )
        result = _detect_plan_url(body)
        assert result == "https://github.com/acme/app/issues/99"


# ---------------------------------------------------------------------------
# Plan alignment — _fetch_plan_body
# ---------------------------------------------------------------------------

class TestFetchPlanBody:
    @patch("app.review_runner.run_gh")
    def test_returns_empty_when_no_plan_label(self, mock_gh):
        """Returns empty string if the issue has no 'plan' label."""
        mock_gh.return_value = json.dumps({
            "body": "This is a regular issue.",
            "labels": [{"name": "bug"}, {"name": "enhancement"}],
        })
        result = _fetch_plan_body("owner", "repo", "42")
        assert result == ""

    @patch("app.review_runner.run_gh")
    def test_returns_body_when_plan_label(self, mock_gh):
        """Returns issue body when 'plan' label is present."""
        mock_gh.side_effect = [
            json.dumps({
                "body": "## Summary\n\nPlan content here.",
                "labels": [{"name": "plan"}],
            }),
            "",  # No comments
        ]
        result = _fetch_plan_body("owner", "repo", "42")
        assert result == "## Summary\n\nPlan content here."

    @patch("app.review_runner.run_gh")
    def test_strips_plan_footer(self, mock_gh):
        """Strips the Kōan /plan footer from the returned body."""
        mock_gh.side_effect = [
            json.dumps({
                "body": "## Summary\n\nPlan text.\n---\n*Generated by Kōan /plan — iteration 1*",
                "labels": [{"name": "plan"}],
            }),
            "",  # No comments
        ]
        result = _fetch_plan_body("owner", "repo", "42")
        assert result == "## Summary\n\nPlan text."
        assert "Generated by Kōan" not in result

    @patch("app.review_runner.run_gh")
    def test_uses_latest_comment_with_implementation_phases(self, mock_gh):
        """Uses the last comment body if it contains '### Implementation Phases'."""
        comment_line = json.dumps({"body": "### Implementation Phases\n\nUpdated plan."})
        mock_gh.side_effect = [
            json.dumps({
                "body": "Original plan body.",
                "labels": [{"name": "plan"}],
            }),
            comment_line,
        ]
        result = _fetch_plan_body("owner", "repo", "42")
        assert "Updated plan." in result
        assert "Original plan body." not in result

    @patch("app.review_runner.run_gh")
    def test_returns_empty_on_fetch_error(self, mock_gh):
        """Returns empty string if the GitHub API call fails."""
        mock_gh.side_effect = RuntimeError("API error")
        result = _fetch_plan_body("owner", "repo", "42")
        assert result == ""

    @patch("app.review_runner.run_gh")
    def test_returns_empty_on_json_error(self, mock_gh):
        """Returns empty string if the API response is not valid JSON."""
        mock_gh.return_value = "not json"
        result = _fetch_plan_body("owner", "repo", "42")
        assert result == ""


# ---------------------------------------------------------------------------
# Plan alignment — _truncate_plan
# ---------------------------------------------------------------------------

class TestTruncatePlan:
    def test_extracts_summary_section(self):
        """Extracts ## Summary section from the plan."""
        plan = (
            "## Background\n\nSome history.\n\n"
            "## Summary\n\nThis is the summary.\n\n"
            "## Next Steps\n\nFuture work."
        )
        result = _truncate_plan(plan)
        assert "This is the summary." in result

    def test_extracts_implementation_phases_section(self):
        """Extracts ### Implementation Phases section."""
        plan = (
            "## Summary\n\nBrief.\n\n"
            "### Implementation Phases\n\n#### Phase 1\nDo this.\n\n"
            "### Open Questions\n\nTBD."
        )
        result = _truncate_plan(plan)
        assert "Phase 1" in result

    def test_fallback_to_first_5000_chars(self):
        """Falls back to first 5000 chars when no sections are found."""
        plan = "x" * 10000
        result = _truncate_plan(plan)
        assert len(result) <= 5000 + 30  # 30 chars for the truncation note
        assert "...(plan truncated)" in result


# ---------------------------------------------------------------------------
# Plan alignment — build_review_prompt with plan
# ---------------------------------------------------------------------------

class TestBuildReviewPromptWithPlan:
    def test_selects_plan_prompt_when_plan_body_provided(self, pr_context, tmp_path):
        """Selects review-with-plan.md when plan_body is provided."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review.md").write_text("Standard review: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")
        (prompts_dir / "review-with-plan.md").write_text("Plan review: {PLAN} {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")

        prompt = build_review_prompt(pr_context, skill_dir=tmp_path, plan_body="The plan content.")
        assert "Plan review:" in prompt
        assert "The plan content." in prompt

    def test_selects_standard_prompt_when_no_plan(self, pr_context, tmp_path):
        """Selects review.md when no plan_body is provided."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review.md").write_text("Standard review: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")
        (prompts_dir / "review-with-plan.md").write_text("Plan review: {PLAN} {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")

        prompt = build_review_prompt(pr_context, skill_dir=tmp_path, plan_body=None)
        assert "Standard review:" in prompt
        assert "Plan review:" not in prompt

    def test_plan_overrides_architecture_flag(self, pr_context, tmp_path):
        """Plan alignment takes priority over --architecture flag."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review-architecture.md").write_text("Architecture review: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")
        (prompts_dir / "review-with-plan.md").write_text("Plan review: {PLAN} {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")

        prompt = build_review_prompt(
            pr_context, skill_dir=tmp_path,
            architecture=True, plan_body="The plan.",
        )
        assert "Plan review:" in prompt
        assert "Architecture review:" not in prompt

    def test_truncates_large_plan(self, pr_context, tmp_path):
        """Plan is truncated when combined plan+diff context exceeds 80K chars."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review-with-plan.md").write_text("Plan: {PLAN} Diff: {DIFF} Title: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS} {REPLIABLE_COMMENTS}")

        large_plan = "## Summary\n\nShort summary.\n\n" + "x" * 90_000
        pr_context["diff"] = "small diff"
        prompt = build_review_prompt(pr_context, skill_dir=tmp_path, plan_body=large_plan)
        # The plan should have been truncated — not 90K chars
        assert len(prompt) < 90_000 + 5000


# ---------------------------------------------------------------------------
# Plan alignment — _format_review_as_markdown with plan_alignment
# ---------------------------------------------------------------------------

class TestFormatReviewWithPlanAlignment:
    def test_renders_plan_alignment_section(self):
        """Renders ### Plan Alignment section when plan_alignment is present."""
        review_data = {
            **LGTM_REVIEW_JSON,
            "plan_alignment": {
                "requirements_met": ["Phase 1: _detect_plan_url added"],
                "requirements_missing": ["Phase 3: --plan-url flag missing"],
                "out_of_scope": [],
            },
        }
        result = _format_review_as_markdown(review_data)
        assert "### Plan Alignment" in result
        assert "✅ **Met**" in result
        assert "_detect_plan_url added" in result
        assert "❌ **Missing**" in result
        assert "--plan-url flag missing" in result

    def test_plan_alignment_before_severity_sections(self):
        """Plan alignment section appears before severity sections."""
        review_data = {
            **VALID_REVIEW_JSON,
            "plan_alignment": {
                "requirements_met": ["Req 1"],
                "requirements_missing": [],
                "out_of_scope": [],
            },
        }
        result = _format_review_as_markdown(review_data)
        plan_pos = result.find("### Plan Alignment")
        severity_pos = result.find("### 🔴 Blocking")
        assert plan_pos != -1
        assert severity_pos != -1
        assert plan_pos < severity_pos

    def test_no_plan_alignment_section_when_absent(self):
        """No Plan Alignment section when plan_alignment is not in data."""
        result = _format_review_as_markdown(LGTM_REVIEW_JSON)
        assert "### Plan Alignment" not in result

    def test_renders_out_of_scope_items(self):
        """Out-of-scope items are rendered when present."""
        review_data = {
            **LGTM_REVIEW_JSON,
            "plan_alignment": {
                "requirements_met": [],
                "requirements_missing": [],
                "out_of_scope": ["Extra helper added"],
            },
        }
        result = _format_review_as_markdown(review_data)
        assert "📋 **Out of scope**" in result
        assert "Extra helper added" in result


# ---------------------------------------------------------------------------
# Plan alignment — run_review auto-detection
# ---------------------------------------------------------------------------

class TestRunReviewPlanAlignment:
    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_auto_detects_plan_from_pr_body(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        plan_review_skill_dir,
    ):
        """Auto-detects plan URL from PR body and includes plan in prompt."""
        context = {
            "title": "Implement plan",
            "body": "Implements https://github.com/owner/repo/issues/10 per spec.",
            "branch": "feature/plan",
            "base": "main",
            "state": "OPEN",
            "author": "dev",
            "url": "https://github.com/owner/repo/pull/5",
            "diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n+x = 1",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
        }
        mock_fetch.return_value = context

        # First gh call: detect plan (gh api repos/.../issues/10)
        # Then comment post
        plan_issue = json.dumps({
            "body": "## Summary\n\nPlan here.",
            "labels": [{"name": "plan"}],
        })
        mock_gh.side_effect = [
            plan_issue,  # _fetch_plan_body: issue
            "",          # _fetch_plan_body: comments
            "posted",    # _post_review_comment
        ]
        mock_claude.return_value = (json.dumps(LGTM_REVIEW_JSON), "")

        success, summary, _ = run_review(
            "owner", "repo", "5", "/tmp/project",
            notify_fn=MagicMock(),
            skill_dir=plan_review_skill_dir,
        )
        assert success is True
        # Verify that plan fetching was attempted (gh api called for issues/10)
        assert mock_gh.call_count >= 2

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_no_plan_when_no_issue_in_body(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, review_skill_dir,
    ):
        """No plan alignment when PR body has no linked issue URL."""
        pr_context["body"] = "Refactoring pass. No linked issue."
        mock_fetch.return_value = pr_context
        mock_claude.return_value = (json.dumps(LGTM_REVIEW_JSON), "")

        success, _, _ = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=MagicMock(),
            skill_dir=review_skill_dir,
        )
        assert success is True
        # run_gh only called once: to post the review comment
        assert mock_gh.call_count == 1

    @patch("app.review_runner.fetch_repliable_comments", return_value=[])
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_explicit_plan_url_overrides_auto_detection(
        self, mock_fetch, mock_claude, mock_gh, mock_repliable,
        pr_context, plan_review_skill_dir,
    ):
        """Explicit --plan-url fetches the specified issue, skipping auto-detect."""
        pr_context["body"] = "No issue URLs here."
        mock_fetch.return_value = pr_context
        mock_claude.return_value = (json.dumps(LGTM_REVIEW_JSON), "")

        plan_issue = json.dumps({
            "body": "## Summary\n\nExplicit plan.",
            "labels": [],  # No 'plan' label — explicit URLs skip label check
        })
        mock_gh.side_effect = [
            plan_issue,  # _resolve_plan_body: explicit issue fetch
            "",          # comments
            "posted",    # _post_review_comment
        ]

        success, _, _ = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=MagicMock(),
            skill_dir=plan_review_skill_dir,
            plan_url="https://github.com/owner/repo/issues/99",
        )
        assert success is True
        # Ensure plan issue was fetched
        first_call_args = mock_gh.call_args_list[0]
        assert "issues/99" in " ".join(str(a) for a in first_call_args[0])


# ---------------------------------------------------------------------------
# Plan alignment — CLI --plan-url flag
# ---------------------------------------------------------------------------

class TestPlanUrlCliFlag:
    @patch("app.review_runner.run_review")
    def test_cli_passes_plan_url(self, mock_run):
        """--plan-url is parsed and passed to run_review."""
        from app.review_runner import main

        mock_run.return_value = (True, "Review posted.", None)
        exit_code = main([
            "https://github.com/owner/repo/pull/42",
            "--project-path", "/tmp/project",
            "--plan-url", "https://github.com/owner/repo/issues/10",
        ])

        assert exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["plan_url"] == "https://github.com/owner/repo/issues/10"

    @patch("app.review_runner.run_review")
    def test_cli_plan_url_defaults_to_none(self, mock_run):
        """--plan-url defaults to None when not provided."""
        from app.review_runner import main

        mock_run.return_value = (True, "Review posted.", None)
        main([
            "https://github.com/owner/repo/pull/42",
            "--project-path", "/tmp/project",
        ])

        _, kwargs = mock_run.call_args
        assert kwargs["plan_url"] is None


# ---------------------------------------------------------------------------
# Plan alignment — skill_dispatch --plan-url passthrough
# ---------------------------------------------------------------------------

class TestSkillDispatchPlanUrl:
    def test_passes_plan_url_to_review_cmd(self):
        """_build_review_cmd passes --plan-url when present in args."""
        from app.skill_dispatch import dispatch_skill_mission

        with patch("app.skill_dispatch.is_known_project", return_value=False):
            cmd = dispatch_skill_mission(
                "/review https://github.com/owner/repo/pull/5 "
                "--plan-url https://github.com/owner/repo/issues/3",
                project_name="myproject",
                project_path="/tmp/proj",
                koan_root="/tmp/koan",
                instance_dir="/tmp/instance",
            )

        assert cmd is not None
        assert "--plan-url" in cmd
        idx = cmd.index("--plan-url")
        assert cmd[idx + 1] == "https://github.com/owner/repo/issues/3"

    def test_no_plan_url_when_absent(self):
        """_build_review_cmd does not add --plan-url when not in args."""
        from app.skill_dispatch import dispatch_skill_mission

        with patch("app.skill_dispatch.is_known_project", return_value=False):
            cmd = dispatch_skill_mission(
                "/review https://github.com/owner/repo/pull/5",
                project_name="myproject",
                project_path="/tmp/proj",
                koan_root="/tmp/koan",
                instance_dir="/tmp/instance",
            )

        assert cmd is not None
        assert "--plan-url" not in cmd


# ---------------------------------------------------------------------------
# Concurrency: fetch_repliable_comments parallel parameter
# ---------------------------------------------------------------------------

class TestFetchRepliableCommentsParallel:
    """Tests for the parallel=False / parallel=True modes of fetch_repliable_comments."""

    @patch("app.review_runner._fetch_issue_comments")
    @patch("app.review_runner._fetch_inline_review_comments")
    def test_sequential_mode_calls_helpers_in_order(
        self, mock_inline, mock_issue,
    ):
        """parallel=False calls the two fetch helpers sequentially."""
        mock_inline.return_value = [{"id": 1, "type": "review_comment"}]
        mock_issue.return_value = [{"id": 2, "type": "issue_comment"}]

        comments = fetch_repliable_comments("owner", "repo", "42", parallel=False)

        mock_inline.assert_called_once_with("owner/repo", "42")
        mock_issue.assert_called_once_with("owner/repo", "42")
        assert len(comments) == 2
        # Inline results come first in sequential mode
        assert comments[0]["id"] == 1
        assert comments[1]["id"] == 2

    @patch("app.review_runner._fetch_issue_comments")
    @patch("app.review_runner._fetch_inline_review_comments")
    def test_parallel_mode_collects_both_result_sets(
        self, mock_inline, mock_issue,
    ):
        """parallel=True still collects results from both helpers."""
        mock_inline.return_value = [{"id": 10, "type": "review_comment"}]
        mock_issue.return_value = [{"id": 20, "type": "issue_comment"}]

        comments = fetch_repliable_comments("owner", "repo", "99", parallel=True)

        assert len(comments) == 2
        ids = {c["id"] for c in comments}
        assert ids == {10, 20}

    @patch("app.review_runner._fetch_issue_comments")
    @patch("app.review_runner._fetch_inline_review_comments")
    def test_empty_results_from_both_helpers(self, mock_inline, mock_issue):
        """Returns empty list when both helpers return nothing."""
        mock_inline.return_value = []
        mock_issue.return_value = []

        comments = fetch_repliable_comments("owner", "repo", "1", parallel=False)
        assert comments == []


# ---------------------------------------------------------------------------
# Concurrency config: get_review_concurrency_config
# ---------------------------------------------------------------------------

class TestReviewConcurrencyConfig:
    """Tests for get_review_concurrency_config() in app.config."""

    def test_defaults_when_no_config(self):
        """Returns sensible defaults when review_concurrency is absent from config."""
        from app.config import get_review_concurrency_config

        with patch("app.config._load_config", return_value={}):
            cfg = get_review_concurrency_config()

        assert cfg["enabled"] is True
        assert cfg["github_workers"] == 4

    def test_reads_enabled_flag(self):
        """Reads enabled flag from config."""
        from app.config import get_review_concurrency_config

        with patch("app.config._load_config", return_value={
            "review_concurrency": {"enabled": False, "github_workers": 2},
        }):
            cfg = get_review_concurrency_config()

        assert cfg["enabled"] is False
        assert cfg["github_workers"] == 2

    def test_invalid_workers_falls_back_to_default(self):
        """Non-integer github_workers falls back to 4."""
        from app.config import get_review_concurrency_config

        with patch("app.config._load_config", return_value={
            "review_concurrency": {"github_workers": "not-a-number"},
        }):
            cfg = get_review_concurrency_config()

        assert cfg["github_workers"] == 4

    def test_non_dict_config_uses_defaults(self):
        """A non-dict review_concurrency value uses defaults."""
        from app.config import get_review_concurrency_config

        with patch("app.config._load_config", return_value={
            "review_concurrency": "invalid",
        }):
            cfg = get_review_concurrency_config()

        assert cfg["enabled"] is True
        assert cfg["github_workers"] == 4
