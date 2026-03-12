"""Tests for review_runner.py — code review pipeline for PRs."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.review_runner import (
    build_review_prompt,
    run_review,
    _extract_review_body,
    _post_review_comment,
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
    )
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
    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_full_pipeline_success(
        self, mock_fetch, mock_claude, mock_gh, pr_context, review_skill_dir,
    ):
        """Full review pipeline: fetch -> claude -> post comment."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = (
            "## PR Review — Fix auth bypass\n\n"
            "Solid fix. No issues found.\n\n---\n\n"
            "### Summary\n\nMerge-ready."
        )
        mock_notify = MagicMock()

        success, summary = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is True
        assert "42" in summary
        mock_fetch.assert_called_once_with("owner", "repo", "42")
        mock_claude.assert_called_once()
        mock_gh.assert_called_once()  # post comment
        assert mock_notify.call_count >= 2  # at least "Reviewing" + "Posting"

    @patch("app.review_runner.fetch_pr_context")
    def test_fetch_failure(self, mock_fetch, pr_context):
        """Handles PR context fetch failure."""
        mock_fetch.side_effect = RuntimeError("API down")
        mock_notify = MagicMock()

        success, summary = run_review(
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

        success, summary = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
        )

        assert success is False
        assert "no diff" in summary

    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_claude_empty_output(
        self, mock_fetch, mock_claude, pr_context, review_skill_dir,
    ):
        """Returns failure when Claude produces no output."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = ""
        mock_notify = MagicMock()

        success, summary = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is False
        assert "no output" in summary

    @patch("app.review_runner.run_gh", side_effect=RuntimeError("post fail"))
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_comment_post_failure(
        self, mock_fetch, mock_claude, mock_gh, pr_context, review_skill_dir,
    ):
        """Handles comment posting failure."""
        mock_fetch.return_value = pr_context
        mock_claude.return_value = "## PR Review — Fix auth bypass\n\nGood code"
        mock_notify = MagicMock()

        success, summary = run_review(
            "owner", "repo", "42", "/tmp/project",
            notify_fn=mock_notify,
            skill_dir=review_skill_dir,
        )

        assert success is False
        assert "failed to post" in summary.lower()


# ---------------------------------------------------------------------------
# main() CLI entry point
# ---------------------------------------------------------------------------

class TestMainCli:
    @patch("app.review_runner.run_review")
    def test_valid_pr_url(self, mock_run):
        """CLI parses PR URL and calls run_review."""
        from app.review_runner import main

        mock_run.return_value = (True, "Review posted.")
        exit_code = main([
            "https://github.com/owner/repo/pull/42",
            "--project-path", "/tmp/project",
        ])

        assert exit_code == 0
        mock_run.assert_called_once_with(
            "owner", "repo", "42", "/tmp/project",
            skill_dir=Path(__file__).resolve().parent.parent / "skills" / "core" / "review",
            architecture=False,
        )

    @patch("app.review_runner.run_review")
    def test_failure_returns_1(self, mock_run):
        """CLI returns exit code 1 on review failure."""
        from app.review_runner import main

        mock_run.return_value = (False, "Claude failed.")
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
            mock_run.return_value = (True, "Review posted.")
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
            mock_run.return_value = (True, "Review posted.")
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

    @patch("app.review_runner.run_gh")
    @patch("app.review_runner._run_claude_review")
    @patch("app.review_runner.fetch_pr_context")
    def test_run_review_passes_architecture_to_prompt(
        self, mock_fetch, mock_claude, mock_gh, pr_context, tmp_path,
    ):
        """run_review with architecture=True uses architecture prompt."""
        # Set up skill dir with both prompts
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review.md").write_text(
            "STANDARD: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} "
            "{REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS}"
        )
        (prompts_dir / "review-architecture.md").write_text(
            "ARCHITECTURE: {TITLE} {AUTHOR} {BRANCH} {BASE} {BODY} {DIFF} "
            "{REVIEWS} {REVIEW_COMMENTS} {ISSUE_COMMENTS}"
        )

        mock_fetch.return_value = pr_context
        mock_claude.return_value = "## PR Review — Fix auth bypass\n\nGood"
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
