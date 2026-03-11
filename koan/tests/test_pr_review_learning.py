"""Tests for pr_review_learning.py — PR review learning for autonomous alignment."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pr_review_learning import (
    _append_lessons_to_learnings,
    _compute_review_hash,
    _is_cache_fresh,
    _parse_iso,
    _write_cache,
    analyze_reviews_with_cli,
    fetch_pr_reviews,
    format_reviews_for_analysis,
    learn_from_reviews,
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


# ─── format_reviews_for_analysis ─────────────────────────────────────────


class TestFormatReviewsForAnalysis:
    def _make_pr(self, number, title, was_merged=True, reviews=None, comments=None):
        return {
            "number": number,
            "title": title,
            "was_merged": was_merged,
            "reviews": reviews or [],
            "review_comments": comments or [],
        }

    def test_empty_prs(self):
        assert format_reviews_for_analysis([]) == ""

    def test_pr_with_no_reviews(self):
        prs = [self._make_pr(1, "feat: add X")]
        # No reviews or comments → no section
        assert format_reviews_for_analysis(prs) == ""

    def test_merged_pr_with_review(self):
        prs = [self._make_pr(1, "feat: add X", reviews=[
            {"state": "APPROVED", "body": "LGTM", "user": "reviewer"},
        ])]
        result = format_reviews_for_analysis(prs)
        assert "PR #1" in result
        assert "MERGED" in result
        assert "LGTM" in result

    def test_closed_pr_shows_status(self):
        prs = [self._make_pr(1, "feat: bad idea", was_merged=False, reviews=[
            {"state": "CHANGES_REQUESTED", "body": "Too big", "user": "reviewer"},
        ])]
        result = format_reviews_for_analysis(prs)
        assert "CLOSED (not merged)" in result

    def test_inline_comments_included(self):
        prs = [self._make_pr(1, "fix: thing", comments=[
            {"body": "Don't touch this", "path": "src/core.py", "user": "reviewer"},
        ])]
        result = format_reviews_for_analysis(prs)
        assert "src/core.py" in result
        assert "Don't touch this" in result

    def test_review_without_body_but_with_state(self):
        prs = [self._make_pr(1, "fix: thing", reviews=[
            {"state": "APPROVED", "body": "", "user": "reviewer"},
        ])]
        result = format_reviews_for_analysis(prs)
        assert "APPROVED" in result
        assert "[no comment]" in result

    def test_multiple_prs(self):
        prs = [
            self._make_pr(1, "feat: A", reviews=[
                {"state": "APPROVED", "body": "Nice!", "user": "r"},
            ]),
            self._make_pr(2, "feat: B", reviews=[
                {"state": "CHANGES_REQUESTED", "body": "Too big", "user": "r"},
            ]),
        ]
        result = format_reviews_for_analysis(prs)
        assert "PR #1" in result
        assert "PR #2" in result


# ─── _compute_review_hash ────────────────────────────────────────────────


class TestComputeReviewHash:
    def test_stable_for_same_input(self):
        prs = [{"number": 1, "reviews": [{"body": "ok"}], "review_comments": []}]
        h1 = _compute_review_hash(prs)
        h2 = _compute_review_hash(prs)
        assert h1 == h2

    def test_changes_with_new_review(self):
        prs1 = [{"number": 1, "reviews": [{"body": "ok"}], "review_comments": []}]
        prs2 = [{"number": 1, "reviews": [{"body": "ok"}, {"body": "more"}], "review_comments": []}]
        assert _compute_review_hash(prs1) != _compute_review_hash(prs2)

    def test_order_independent(self):
        prs_a = [
            {"number": 1, "reviews": [], "review_comments": []},
            {"number": 2, "reviews": [], "review_comments": []},
        ]
        prs_b = [
            {"number": 2, "reviews": [], "review_comments": []},
            {"number": 1, "reviews": [], "review_comments": []},
        ]
        assert _compute_review_hash(prs_a) == _compute_review_hash(prs_b)


# ─── cache ───────────────────────────────────────────────────────────────


class TestCache:
    def test_fresh_when_hash_matches(self, tmp_path):
        _write_cache(str(tmp_path), "abc123")
        assert _is_cache_fresh(str(tmp_path), "abc123") is True

    def test_stale_when_hash_differs(self, tmp_path):
        _write_cache(str(tmp_path), "abc123")
        assert _is_cache_fresh(str(tmp_path), "def456") is False

    def test_stale_when_no_cache(self, tmp_path):
        assert _is_cache_fresh(str(tmp_path), "abc123") is False


# ─── _append_lessons_to_learnings ────────────────────────────────────────


class TestAppendLessonsToLearnings:
    def test_creates_file_if_missing(self, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        lessons = "- Reviewer prefers small PRs\n- Always add tests"
        added = _append_lessons_to_learnings(str(instance_dir), "myproject", lessons)

        assert added == 2
        learnings = (instance_dir / "memory" / "projects" / "myproject" / "learnings.md")
        assert learnings.exists()
        content = learnings.read_text()
        assert "small PRs" in content
        assert "add tests" in content
        assert "# Learnings" in content

    def test_appends_to_existing(self, tmp_path):
        instance_dir = tmp_path / "instance"
        learnings_dir = instance_dir / "memory" / "projects" / "myproject"
        learnings_dir.mkdir(parents=True)
        learnings_file = learnings_dir / "learnings.md"
        learnings_file.write_text("# Learnings — myproject\n\n- Old lesson\n")

        lessons = "- New lesson from reviews"
        added = _append_lessons_to_learnings(str(instance_dir), "myproject", lessons)

        assert added == 1
        content = learnings_file.read_text()
        assert "Old lesson" in content
        assert "New lesson from reviews" in content

    def test_skips_duplicates(self, tmp_path):
        instance_dir = tmp_path / "instance"
        learnings_dir = instance_dir / "memory" / "projects" / "myproject"
        learnings_dir.mkdir(parents=True)
        learnings_file = learnings_dir / "learnings.md"
        learnings_file.write_text("# Learnings\n\n- Existing lesson\n")

        lessons = "- Existing lesson\n- Brand new lesson"
        added = _append_lessons_to_learnings(str(instance_dir), "myproject", lessons)

        assert added == 1
        content = learnings_file.read_text()
        assert content.count("Existing lesson") == 1
        assert "Brand new lesson" in content

    def test_returns_zero_when_all_duplicates(self, tmp_path):
        instance_dir = tmp_path / "instance"
        learnings_dir = instance_dir / "memory" / "projects" / "myproject"
        learnings_dir.mkdir(parents=True)
        learnings_file = learnings_dir / "learnings.md"
        learnings_file.write_text("# Learnings\n\n- Already known\n")

        added = _append_lessons_to_learnings(str(instance_dir), "myproject", "- Already known")
        assert added == 0

    def test_empty_lessons_returns_zero(self, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        added = _append_lessons_to_learnings(str(instance_dir), "myproject", "")
        assert added == 0


# ─── analyze_reviews_with_cli ────────────────────────────────────────────


class TestAnalyzeReviewsWithCli:
    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command")
    @patch("app.config.get_model_config")
    @patch("app.prompts.load_prompt")
    def test_returns_stdout_on_success(self, mock_prompt, mock_models,
                                       mock_build, mock_run):
        mock_prompt.return_value = "analysis prompt"
        mock_models.return_value = {"lightweight": "haiku", "fallback": "sonnet"}
        mock_build.return_value = ["claude", "-p", "..."]
        mock_run.return_value = MagicMock(
            returncode=0, stdout="- Lesson 1\n- Lesson 2\n", stderr=""
        )

        result = analyze_reviews_with_cli("some review text", "/fake/path")
        assert "Lesson 1" in result
        assert "Lesson 2" in result

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command")
    @patch("app.config.get_model_config")
    @patch("app.prompts.load_prompt")
    def test_returns_empty_on_failure(self, mock_prompt, mock_models,
                                      mock_build, mock_run):
        mock_prompt.return_value = "prompt"
        mock_models.return_value = {"lightweight": "haiku", "fallback": "sonnet"}
        mock_build.return_value = ["claude", "-p", "..."]
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="quota exceeded"
        )

        result = analyze_reviews_with_cli("review text", "/fake/path")
        assert result == ""

    @patch("app.cli_exec.run_cli_with_retry")
    @patch("app.cli_provider.build_full_command")
    @patch("app.config.get_model_config")
    @patch("app.prompts.load_prompt")
    def test_returns_empty_on_exception(self, mock_prompt, mock_models,
                                        mock_build, mock_run):
        mock_prompt.return_value = "prompt"
        mock_models.return_value = {"lightweight": "haiku", "fallback": "sonnet"}
        mock_build.return_value = ["claude", "-p", "..."]
        mock_run.side_effect = OSError("timeout")

        result = analyze_reviews_with_cli("review text", "/fake/path")
        assert result == ""


# ─── fetch_pr_reviews ────────────────────────────────────────────────────


class TestFetchPrReviews:
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
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(prs), stderr=""
        )
        result = fetch_pr_reviews("/fake/path")
        assert len(result) == 0

    def test_import_error_returns_empty(self):
        with patch.dict("sys.modules", {"app.github": None}):
            result = fetch_pr_reviews("/fake/path")
            assert result == []


# ─── learn_from_reviews (integration) ────────────────────────────────────


class TestLearnFromReviews:
    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_returns_skipped_when_no_prs(self, mock_fetch):
        mock_fetch.return_value = []
        result = learn_from_reviews("/instance", "proj", "/path")
        assert result["skipped_reason"] == "no_reviews"
        assert result["lessons_added"] == 0

    @patch("app.pr_review_learning._write_cache")
    @patch("app.pr_review_learning._is_cache_fresh")
    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_skips_when_cache_fresh(self, mock_fetch, mock_cache, mock_write):
        mock_fetch.return_value = [
            {"number": 1, "reviews": [{"body": "ok"}], "review_comments": [],
             "was_merged": True, "title": "fix"},
        ]
        mock_cache.return_value = True
        result = learn_from_reviews("/instance", "proj", "/path")
        assert result["skipped_reason"] == "cache_fresh"
        mock_write.assert_not_called()

    @patch("app.pr_review_learning._append_lessons_to_learnings")
    @patch("app.pr_review_learning._write_cache")
    @patch("app.pr_review_learning._is_cache_fresh")
    @patch("app.pr_review_learning.analyze_reviews_with_cli")
    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_full_flow(self, mock_fetch, mock_analyze, mock_cache_check,
                       mock_cache_write, mock_append):
        mock_fetch.return_value = [
            {
                "number": 1, "title": "feat: X", "was_merged": False,
                "reviews": [
                    {"state": "CHANGES_REQUESTED", "body": "Too big!", "user": "r"},
                ],
                "review_comments": [],
            },
        ]
        mock_cache_check.return_value = False
        mock_analyze.return_value = "- Keep PRs small and focused"
        mock_append.return_value = 1

        result = learn_from_reviews("/instance", "proj", "/path")

        assert result["fetched"] == 1
        assert result["analyzed"] is True
        assert result["lessons_added"] == 1
        assert result["skipped_reason"] is None
        mock_cache_write.assert_called_once()

    @patch("app.pr_review_learning._write_cache")
    @patch("app.pr_review_learning._is_cache_fresh")
    @patch("app.pr_review_learning.analyze_reviews_with_cli")
    @patch("app.pr_review_learning.fetch_pr_reviews")
    def test_empty_analysis_still_caches(self, mock_fetch, mock_analyze,
                                         mock_cache_check, mock_cache_write):
        mock_fetch.return_value = [
            {
                "number": 1, "title": "feat: X", "was_merged": True,
                "reviews": [
                    {"state": "APPROVED", "body": "ok", "user": "r"},
                ],
                "review_comments": [],
            },
        ]
        mock_cache_check.return_value = False
        mock_analyze.return_value = ""

        result = learn_from_reviews("/instance", "proj", "/path")
        assert result["skipped_reason"] == "empty_analysis"
        # Cache should still be written to avoid re-running
        mock_cache_write.assert_called_once()
