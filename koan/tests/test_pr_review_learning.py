"""Tests for pr_review_learning.py — PR review learning for autonomous alignment."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.pr_review_learning import (
    _FEEDBACK_CATEGORIES,
    _category_label,
    _infer_rejection_reason,
    _parse_iso,
    categorize_feedback,
    extract_lessons,
    fetch_pr_reviews,
    format_lessons_for_prompt,
    get_review_lessons,
)


# ─── _parse_iso ──────────────────────────────────────────────────────────


class TestParseIso:
    def test_z_suffix(self):
        dt = _parse_iso("2026-03-01T12:00:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3

    def test_offset_suffix(self):
        dt = _parse_iso("2026-03-01T12:00:00+00:00")
        assert dt is not None
        assert dt.hour == 12

    def test_empty_string(self):
        assert _parse_iso("") is None

    def test_none(self):
        assert _parse_iso(None) is None

    def test_invalid(self):
        assert _parse_iso("not-a-date") is None


# ─── categorize_feedback ──────────────────────────────────────────────────


class TestCategorizeFeedback:
    def test_scope_too_big(self):
        cats = categorize_feedback("This PR is too big, please split it")
        assert "scope" in cats

    def test_scope_smaller_pr(self):
        cats = categorize_feedback("Can you make a smaller PR?")
        assert "scope" in cats

    def test_testing_feedback(self):
        cats = categorize_feedback("Missing tests for the new helper")
        assert "testing" in cats

    def test_style_naming(self):
        cats = categorize_feedback("The naming convention here is wrong")
        assert "style" in cats

    def test_approach_alternative(self):
        cats = categorize_feedback("I'd prefer a simpler approach here")
        assert "approach" in cats

    def test_dont_touch(self):
        cats = categorize_feedback("Don't touch this file, it's fine as-is")
        assert "dont_touch" in cats

    def test_praise_lgtm(self):
        cats = categorize_feedback("LGTM, great work!")
        assert "praise" in cats

    def test_praise_emoji(self):
        cats = categorize_feedback("Nice improvement 👍")
        assert "praise" in cats

    def test_multiple_categories(self):
        cats = categorize_feedback("Good approach but missing tests")
        assert "praise" in cats
        assert "testing" in cats

    def test_empty_string(self):
        assert categorize_feedback("") == []

    def test_none(self):
        assert categorize_feedback(None) == []

    def test_no_match(self):
        cats = categorize_feedback("I have no specific feedback on this")
        assert cats == []

    def test_overkill(self):
        cats = categorize_feedback("This seems like overkill for the problem")
        assert "approach" in cats

    def test_revert(self):
        cats = categorize_feedback("Please revert this change")
        assert "dont_touch" in cats

    def test_leave_it(self):
        cats = categorize_feedback("Leave this as-is for now")
        assert "dont_touch" in cats


# ─── _infer_rejection_reason ──────────────────────────────────────────────


class TestInferRejectionReason:
    def test_no_comments(self):
        assert _infer_rejection_reason([]) == "no review comments"

    def test_scope_reason(self):
        reason = _infer_rejection_reason(["This PR is too big, split it please"])
        assert reason == "scope too large"

    def test_approach_reason(self):
        reason = _infer_rejection_reason(["I'd prefer a different approach"])
        assert reason == "approach disagreement"

    def test_dont_touch_reason(self):
        reason = _infer_rejection_reason(["Don't touch this area"])
        assert reason == "area should not be touched"

    def test_style_reason(self):
        reason = _infer_rejection_reason(["The naming convention is inconsistent"])
        assert reason == "style/convention issues"

    def test_generic_feedback(self):
        reason = _infer_rejection_reason(["Missing tests for the handler"])
        assert "testing" in reason

    def test_unclear_feedback(self):
        reason = _infer_rejection_reason(["I have thoughts about this"])
        assert reason == "unclear (review had comments)"


# ─── extract_lessons ──────────────────────────────────────────────────────


class TestExtractLessons:
    def _make_pr(self, number, title, was_merged=True, reviews=None, comments=None):
        return {
            "number": number,
            "title": title,
            "was_merged": was_merged,
            "reviews": reviews or [],
            "review_comments": comments or [],
        }

    def test_empty_prs(self):
        lessons = extract_lessons([])
        assert lessons["feedback_counts"] == {}
        assert lessons["rejected_prs"] == []
        assert lessons["positive_patterns"] == []

    def test_rejected_pr_tracked(self):
        prs = [self._make_pr(1, "refactor: extract module", was_merged=False)]
        lessons = extract_lessons(prs)
        assert len(lessons["rejected_prs"]) == 1
        assert lessons["rejected_prs"][0]["number"] == 1
        assert lessons["rejected_prs"][0]["reason"] == "no review comments"

    def test_rejected_pr_with_feedback(self):
        prs = [self._make_pr(
            1, "refactor: overhaul everything",
            was_merged=False,
            reviews=[{
                "state": "CHANGES_REQUESTED",
                "body": "This is too big, please split into smaller PRs",
                "user": "reviewer",
            }],
        )]
        lessons = extract_lessons(prs)
        assert lessons["rejected_prs"][0]["reason"] == "scope too large"

    def test_feedback_counts_accumulated(self):
        prs = [
            self._make_pr(1, "feat: add X", reviews=[
                {"state": "COMMENTED", "body": "Missing tests for this", "user": "r"},
            ]),
            self._make_pr(2, "feat: add Y", reviews=[
                {"state": "COMMENTED", "body": "Where are the tests?", "user": "r"},
            ]),
        ]
        lessons = extract_lessons(prs)
        assert lessons["feedback_counts"].get("testing", 0) >= 2

    def test_positive_pattern_from_approval(self):
        prs = [self._make_pr(1, "fix: resolve bug", reviews=[
            {"state": "APPROVED", "body": "Great fix, clean and well done!", "user": "r"},
        ])]
        lessons = extract_lessons(prs)
        assert len(lessons["positive_patterns"]) > 0
        assert "approved with praise" in lessons["positive_patterns"][0]

    def test_negative_pattern_from_changes_requested(self):
        prs = [self._make_pr(1, "feat: add X", reviews=[
            {"state": "CHANGES_REQUESTED", "body": "Missing tests!", "user": "r"},
        ])]
        lessons = extract_lessons(prs)
        assert len(lessons["negative_patterns"]) > 0

    def test_dont_touch_from_inline_comment(self):
        prs = [self._make_pr(1, "refactor: cleanup", comments=[
            {"body": "Don't touch this file please", "path": "src/core.py", "user": "r"},
        ])]
        lessons = extract_lessons(prs)
        assert len(lessons["dont_touch_areas"]) > 0
        assert "src/core.py" in lessons["dont_touch_areas"][0]

    def test_review_quotes_captured(self):
        prs = [self._make_pr(1, "feat: add feature", reviews=[
            {
                "state": "CHANGES_REQUESTED",
                "body": "I think the approach here is over-engineered for our needs",
                "user": "reviewer",
            },
        ])]
        lessons = extract_lessons(prs)
        assert len(lessons["review_quotes"]) > 0
        assert lessons["review_quotes"][0]["pr"] == 1

    def test_short_comments_excluded_from_quotes(self):
        prs = [self._make_pr(1, "fix: typo", reviews=[
            {"state": "APPROVED", "body": "ok", "user": "r"},
        ])]
        lessons = extract_lessons(prs)
        # "ok" is <= 10 chars, should not appear in quotes
        assert len(lessons["review_quotes"]) == 0

    def test_pending_reviews_excluded_from_quotes(self):
        prs = [self._make_pr(1, "feat: X", reviews=[
            {
                "state": "PENDING",
                "body": "This is a long pending review that shouldn't appear",
                "user": "r",
            },
        ])]
        lessons = extract_lessons(prs)
        assert len(lessons["review_quotes"]) == 0

    def test_caps_on_patterns(self):
        """Verify that pattern lists are capped at 10."""
        reviews = [
            {"state": "CHANGES_REQUESTED",
             "body": f"Missing tests for item {i}", "user": "r"}
            for i in range(15)
        ]
        prs = [self._make_pr(i, f"feat: item {i}", reviews=[reviews[i]])
               for i in range(15)]
        lessons = extract_lessons(prs)
        assert len(lessons["negative_patterns"]) <= 10


# ─── format_lessons_for_prompt ────────────────────────────────────────────


class TestFormatLessonsForPrompt:
    def test_empty_lessons_returns_empty(self):
        lessons = {
            "feedback_counts": {},
            "rejected_prs": [],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [],
        }
        assert format_lessons_for_prompt(lessons) == ""

    def test_rejected_prs_section(self):
        lessons = {
            "feedback_counts": {},
            "rejected_prs": [{"number": 42, "title": "bad PR", "reason": "scope too large"}],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [],
        }
        result = format_lessons_for_prompt(lessons)
        assert "Rejected PRs" in result
        assert "#42" in result
        assert "scope too large" in result

    def test_dont_touch_section(self):
        lessons = {
            "feedback_counts": {},
            "rejected_prs": [],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": ["src/core.py: leave as-is"],
            "review_quotes": [],
        }
        result = format_lessons_for_prompt(lessons)
        assert "Areas to avoid" in result
        assert "src/core.py" in result

    def test_recurring_feedback_requires_threshold(self):
        """Only categories with 2+ occurrences should appear."""
        lessons = {
            "feedback_counts": {"testing": 1, "scope": 3},
            "rejected_prs": [],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [],
        }
        result = format_lessons_for_prompt(lessons)
        assert "scope" in result.lower() or "PR scope" in result
        # "testing" with count=1 should NOT appear
        assert "Missing or insufficient tests" not in result

    def test_praise_excluded_from_recurring(self):
        """Praise shouldn't appear as 'recurring feedback'."""
        lessons = {
            "feedback_counts": {"praise": 5},
            "rejected_prs": [],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [],
        }
        result = format_lessons_for_prompt(lessons)
        # Should be empty since praise is filtered out
        assert result == ""

    def test_positive_patterns_section(self):
        lessons = {
            "feedback_counts": {},
            "rejected_prs": [],
            "positive_patterns": ["PR #10 (fix: good): approved with praise"],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [],
        }
        result = format_lessons_for_prompt(lessons)
        assert "reviewer values" in result
        assert "#10" in result

    def test_notable_quotes_section(self):
        lessons = {
            "feedback_counts": {},
            "rejected_prs": [],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [
                {
                    "pr": 5,
                    "title": "feat: thing",
                    "text": "This approach is over-engineered for our use case",
                    "state": "CHANGES_REQUESTED",
                },
            ],
        }
        result = format_lessons_for_prompt(lessons)
        assert "Notable reviewer comments" in result
        assert "over-engineered" in result

    def test_quotes_filtered_by_state(self):
        """Only APPROVED and CHANGES_REQUESTED quotes should appear."""
        lessons = {
            "feedback_counts": {},
            "rejected_prs": [],
            "positive_patterns": [],
            "negative_patterns": [],
            "dont_touch_areas": [],
            "review_quotes": [
                {
                    "pr": 5,
                    "title": "feat: thing",
                    "text": "This is just a comment, not a review verdict",
                    "state": "COMMENTED",
                },
            ],
        }
        result = format_lessons_for_prompt(lessons)
        assert "Notable reviewer comments" not in result


# ─── _category_label ──────────────────────────────────────────────────────


class TestCategoryLabel:
    def test_known_categories(self):
        assert "scope" in _category_label("scope").lower()
        assert "test" in _category_label("testing").lower()
        assert "style" in _category_label("style").lower()

    def test_unknown_category(self):
        assert _category_label("mystery") == "mystery"


# ─── fetch_pr_reviews ─────────────────────────────────────────────────────


class TestFetchPrReviews:
    def _mock_gh(self, data_map):
        """Create a side_effect that returns different data per command."""
        def side_effect(*args, **kwargs):
            cmd_args = args
            # Check if it's a pr list or api call
            if "pr" in cmd_args and "list" in cmd_args:
                # Determine state from args
                state_idx = cmd_args.index("--state") + 1 if "--state" in cmd_args else None
                state = cmd_args[state_idx] if state_idx else "merged"
                return json.dumps(data_map.get(f"pr_list_{state}", []))
            elif "api" in cmd_args:
                # API call for reviews or comments
                api_path = cmd_args[1] if len(cmd_args) > 1 else ""
                if "reviews" in api_path:
                    return data_map.get("reviews", "")
                elif "comments" in api_path:
                    return data_map.get("comments", "")
            return "[]"
        return side_effect

    @patch("subprocess.run")
    def test_empty_when_no_prs(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="[]", stderr=""
        )
        result = fetch_pr_reviews("/fake/path")
        assert result == []

    @patch("subprocess.run")
    def test_filters_non_koan_branches(self, mock_run):
        now = datetime.now(timezone.utc)
        prs = [{
            "number": 1,
            "title": "fix: something",
            "createdAt": now.isoformat(),
            "mergedAt": now.isoformat(),
            "closedAt": None,
            "headRefName": "feature/not-koan",
            "state": "MERGED",
        }]
        # First call: merged prs, second: closed prs, then API calls
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(prs), stderr=""
        )
        result = fetch_pr_reviews("/fake/path")
        assert len(result) == 0

    def test_import_error_returns_empty(self):
        with patch.dict("sys.modules", {"app.github": None}):
            # Should gracefully return empty
            result = fetch_pr_reviews("/fake/path")
            assert result == []


# ─── get_review_lessons (integration) ─────────────────────────────────────


class TestGetReviewLessons:
    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_returns_empty_when_no_prs(self, mock_fetch):
        mock_fetch.return_value = []
        result = get_review_lessons("/fake/path")
        assert result == ""

    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_returns_formatted_lessons(self, mock_fetch):
        mock_fetch.return_value = [
            {
                "number": 1,
                "title": "feat: add feature",
                "was_merged": False,
                "reviews": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "body": "This is too big, please split into smaller PRs",
                        "user": "reviewer",
                    },
                ],
                "review_comments": [],
            },
        ]
        result = get_review_lessons("/fake/path")
        assert "Rejected PRs" in result
        assert "scope too large" in result

    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_handles_only_merged_prs(self, mock_fetch):
        mock_fetch.return_value = [
            {
                "number": 1,
                "title": "fix: bug",
                "was_merged": True,
                "reviews": [
                    {"state": "APPROVED", "body": "Great fix, well done!", "user": "r"},
                ],
                "review_comments": [],
            },
        ]
        result = get_review_lessons("/fake/path")
        assert "approved with praise" in result


# ─── prompt_builder integration ───────────────────────────────────────────


class TestPromptBuilderIntegration:
    @patch("app.pr_review_learning.get_review_lessons")
    def test_review_lessons_section(self, mock_lessons):
        from app.prompt_builder import _get_review_lessons_section
        mock_lessons.return_value = "### Some lessons\n- Don't do X"
        result = _get_review_lessons_section("/fake/path")
        assert "PR Review Lessons" in result
        assert "Don't do X" in result

    @patch("app.pr_review_learning.get_review_lessons")
    def test_empty_when_no_lessons(self, mock_lessons):
        from app.prompt_builder import _get_review_lessons_section
        mock_lessons.return_value = ""
        result = _get_review_lessons_section("/fake/path")
        assert result == ""

    @patch("app.pr_review_learning.get_review_lessons")
    def test_exception_handled(self, mock_lessons):
        from app.prompt_builder import _get_review_lessons_section
        mock_lessons.side_effect = RuntimeError("boom")
        result = _get_review_lessons_section("/fake/path")
        assert result == ""
