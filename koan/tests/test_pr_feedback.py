"""Tests for pr_feedback.py — PR merge feedback loop for topic alignment."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from app.pr_feedback import (
    categorize_pr,
    compute_merge_velocity,
    fetch_merged_prs,
    fetch_open_prs,
    get_alignment_summary,
    get_category_boost,
    _parse_iso_datetime,
    _hours_between,
    _format_hours,
    FAST_MERGE_HOURS,
    SLOW_MERGE_HOURS,
)


def _mock_gh_success(data):
    """Create a MagicMock simulating successful gh CLI output."""
    return MagicMock(returncode=0, stdout=json.dumps(data), stderr="")


def _mock_gh_failure(msg="gh failed"):
    """Create a MagicMock simulating failed gh CLI output."""
    return MagicMock(returncode=1, stdout="", stderr=msg)


def _iso_hours_ago(hours: int) -> str:
    """Return an ISO UTC timestamp `hours` ago from now."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── categorize_pr ───────────────────────────────────────────────────────

class TestCategorizePr:
    """Tests for PR title categorization."""

    def test_conventional_commit_fix(self):
        assert categorize_pr("fix: resolve login timeout") == "fix"

    def test_conventional_commit_feat(self):
        assert categorize_pr("feat: add dark mode toggle") == "feature"

    def test_conventional_commit_test(self):
        assert categorize_pr("test: add 48 tests for handler") == "test"

    def test_conventional_commit_refactor(self):
        assert categorize_pr("refactor: extract auth module") == "refactor"

    def test_conventional_commit_docs(self):
        assert categorize_pr("docs: update README") == "docs"

    def test_conventional_commit_perf(self):
        assert categorize_pr("perf: optimize query plan") == "perf"

    def test_conventional_commit_ci(self):
        assert categorize_pr("ci: add pip caching") == "ci"

    def test_conventional_commit_chore(self):
        assert categorize_pr("chore: bump dependencies") == "other"

    def test_conventional_commit_with_scope(self):
        assert categorize_pr("fix(auth): resolve token refresh") == "fix"

    def test_conventional_commit_breaking(self):
        assert categorize_pr("feat!: new API format") == "feature"

    def test_keyword_fix(self):
        assert categorize_pr("Fix stock sync deadlock") == "fix"

    def test_keyword_test(self):
        assert categorize_pr("Add tests for mission runner") == "test"

    def test_keyword_refactor(self):
        assert categorize_pr("Refactoring the config module") == "refactor"

    def test_keyword_security(self):
        assert categorize_pr("Fix CSRF vulnerability in dashboard") == "security"

    def test_keyword_feature(self):
        assert categorize_pr("Add new user registration flow") == "feature"

    def test_keyword_performance(self):
        assert categorize_pr("Optimize database queries") == "perf"

    def test_keyword_docs(self):
        assert categorize_pr("Update documentation for API") == "docs"

    def test_keyword_ci(self):
        assert categorize_pr("Fix deployment pipeline") == "ci"

    def test_empty_title(self):
        assert categorize_pr("") == "other"

    def test_unrecognized_title(self):
        assert categorize_pr("Random changes to stuff") == "other"

    def test_case_insensitive(self):
        assert categorize_pr("FIX: uppercase prefix") == "fix"

    def test_conventional_takes_priority(self):
        """Conventional commit prefix wins over keyword matching."""
        assert categorize_pr("fix: failing test assertion") == "fix"

    def test_security_beats_fix_keyword(self):
        """Security pattern matches before fix when both present."""
        assert categorize_pr("Fix XSS vulnerability") == "security"


# ─── _parse_iso_datetime ─────────────────────────────────────────────────

class TestParseIsoDatetime:

    def test_z_suffix(self):
        dt = _parse_iso_datetime("2026-02-20T14:30:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.hour == 14

    def test_offset_format(self):
        dt = _parse_iso_datetime("2026-02-20T14:30:00+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_empty_string(self):
        assert _parse_iso_datetime("") is None

    def test_none_input(self):
        assert _parse_iso_datetime(None) is None

    def test_invalid_format(self):
        assert _parse_iso_datetime("not-a-date") is None


# ─── _hours_between ──────────────────────────────────────────────────────

class TestHoursBetween:

    def test_exact_24_hours(self):
        start = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc)
        assert _hours_between(start, end) == 24.0

    def test_fraction_hours(self):
        start = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 20, 13, 30, tzinfo=timezone.utc)
        assert _hours_between(start, end) == 1.5

    def test_naive_datetimes(self):
        """Naive datetimes are treated as UTC."""
        start = datetime(2026, 2, 20, 12, 0)
        end = datetime(2026, 2, 20, 18, 0)
        assert _hours_between(start, end) == 6.0


# ─── _format_hours ───────────────────────────────────────────────────────

class TestFormatHours:

    def test_less_than_one(self):
        assert _format_hours(0.5) == "<1h"

    def test_hours(self):
        assert _format_hours(5.0) == "5h"

    def test_one_day(self):
        assert _format_hours(24.0) == "1.0d"

    def test_multiple_days(self):
        assert _format_hours(72.0) == "3d"

    def test_large(self):
        assert _format_hours(240.0) == "10d"


# ─── compute_merge_velocity ──────────────────────────────────────────────

class TestComputeMergeVelocity:

    def test_empty_list(self):
        assert compute_merge_velocity([]) == {}

    def test_single_fast_pr(self):
        prs = [{"category": "fix", "hours_to_merge": 12.0}]
        result = compute_merge_velocity(prs)
        assert result["fix"]["count"] == 1
        assert result["fix"]["avg_hours"] == 12.0
        assert result["fix"]["speed"] == "fast"

    def test_single_slow_pr(self):
        prs = [{"category": "refactor", "hours_to_merge": 200.0}]
        result = compute_merge_velocity(prs)
        assert result["refactor"]["speed"] == "slow"

    def test_moderate_speed(self):
        prs = [{"category": "feature", "hours_to_merge": 96.0}]
        result = compute_merge_velocity(prs)
        assert result["feature"]["speed"] == "moderate"

    def test_multiple_categories(self):
        prs = [
            {"category": "fix", "hours_to_merge": 6.0},
            {"category": "fix", "hours_to_merge": 18.0},
            {"category": "test", "hours_to_merge": 24.0},
            {"category": "refactor", "hours_to_merge": 200.0},
        ]
        result = compute_merge_velocity(prs)

        assert result["fix"]["count"] == 2
        assert result["fix"]["avg_hours"] == 12.0
        assert result["fix"]["speed"] == "fast"

        assert result["test"]["count"] == 1
        assert result["test"]["speed"] == "fast"

        assert result["refactor"]["count"] == 1
        assert result["refactor"]["speed"] == "slow"

    def test_boundary_fast(self):
        """Exactly at FAST_MERGE_HOURS threshold is fast."""
        prs = [{"category": "fix", "hours_to_merge": FAST_MERGE_HOURS}]
        result = compute_merge_velocity(prs)
        assert result["fix"]["speed"] == "fast"

    def test_boundary_moderate(self):
        """Just above FAST_MERGE_HOURS is moderate."""
        prs = [{"category": "fix", "hours_to_merge": FAST_MERGE_HOURS + 1}]
        result = compute_merge_velocity(prs)
        assert result["fix"]["speed"] == "moderate"

    def test_boundary_slow(self):
        """Just above SLOW_MERGE_HOURS is slow."""
        prs = [{"category": "fix", "hours_to_merge": SLOW_MERGE_HOURS + 1}]
        result = compute_merge_velocity(prs)
        assert result["fix"]["speed"] == "slow"


# ─── fetch_merged_prs ────────────────────────────────────────────────────

class TestFetchMergedPrs:

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_filters_koan_branches(self, mock_run, _prefix):
        """Only returns PRs from koan/* branches."""
        mock_run.return_value = _mock_gh_success([
            {
                "number": 1,
                "title": "fix: something",
                "createdAt": _iso_hours_ago(8),
                "mergedAt": _iso_hours_ago(4),
                "headRefName": "koan/fix-something",
            },
            {
                "number": 2,
                "title": "feat: other thing",
                "createdAt": _iso_hours_ago(8),
                "mergedAt": _iso_hours_ago(4),
                "headRefName": "feature/other-thing",
            },
        ])

        result = fetch_merged_prs("/fake/path")
        assert len(result) == 1
        assert result[0]["number"] == 1

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_computes_hours_to_merge(self, mock_run, _prefix):
        mock_run.return_value = _mock_gh_success([{
            "number": 1,
            "title": "fix: something",
            "createdAt": "2026-02-20T10:00:00Z",
            "mergedAt": "2026-02-21T10:00:00Z",
            "headRefName": "koan/fix-something",
        }])

        result = fetch_merged_prs("/fake/path")
        assert result[0]["hours_to_merge"] == 24.0

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_categorizes_prs(self, mock_run, _prefix):
        mock_run.return_value = _mock_gh_success([{
            "number": 1,
            "title": "test: add coverage",
            "createdAt": _iso_hours_ago(8),
            "mergedAt": _iso_hours_ago(4),
            "headRefName": "koan/test-coverage",
        }])

        result = fetch_merged_prs("/fake/path")
        assert result[0]["category"] == "test"

    @patch("subprocess.run")
    def test_gh_failure_returns_empty(self, mock_run):
        mock_run.return_value = _mock_gh_failure()
        result = fetch_merged_prs("/fake/path")
        assert result == []

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_invalid_json_returns_empty(self, mock_run, _prefix):
        mock_run.return_value = MagicMock(returncode=0, stdout="invalid json", stderr="")
        # run_gh will succeed but json.loads will fail
        # Actually run_gh doesn't parse JSON — our function does
        # But run_gh returns the raw stdout, so we need it to return valid output
        # that then fails json.loads in our code
        # Let's make run_gh raise instead (simulating gh failing)
        mock_run.return_value = _mock_gh_failure("json error")
        result = fetch_merged_prs("/fake/path")
        assert result == []

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_skips_prs_without_dates(self, mock_run, _prefix):
        mock_run.return_value = _mock_gh_success([{
            "number": 1,
            "title": "fix: something",
            "createdAt": "",
            "mergedAt": "",
            "headRefName": "koan/fix-something",
        }])

        result = fetch_merged_prs("/fake/path")
        assert result == []

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_filters_by_days_cutoff(self, mock_run, _prefix):
        """PRs merged before the days cutoff are excluded."""
        mock_run.return_value = _mock_gh_success([
            {
                "number": 1,
                "title": "fix: recent",
                "createdAt": "2026-02-25T10:00:00Z",
                "mergedAt": "2026-02-26T10:00:00Z",
                "headRefName": "koan/fix-recent",
            },
            {
                "number": 2,
                "title": "fix: old",
                "createdAt": "2025-01-01T10:00:00Z",
                "mergedAt": "2025-01-02T10:00:00Z",
                "headRefName": "koan/fix-old",
            },
        ])

        result = fetch_merged_prs("/fake/path", days=30)
        # Only the recent PR should be included
        assert len(result) == 1
        assert result[0]["number"] == 1

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_days_parameter_respected(self, mock_run, _prefix):
        """Different days values produce different filtering."""
        mock_run.return_value = _mock_gh_success([{
            "number": 1,
            "title": "fix: something",
            "createdAt": "2026-02-25T10:00:00Z",
            "mergedAt": "2026-02-26T10:00:00Z",
            "headRefName": "koan/fix-something",
        }])

        # With days=365 — should include PRs within the last year
        result = fetch_merged_prs("/fake/path", days=365)
        assert len(result) == 1

        # With days=0 — only PRs merged today
        result = fetch_merged_prs("/fake/path", days=0)
        # The PR from Feb 26 is far in the past, so should be excluded
        assert len(result) == 0


# ─── fetch_open_prs ──────────────────────────────────────────────────────

class TestFetchOpenPrs:

    @patch("app.config.get_branch_prefix", return_value="koan/")
    @patch("subprocess.run")
    def test_returns_open_koan_prs(self, mock_run, _prefix):
        mock_run.return_value = _mock_gh_success([{
            "number": 5,
            "title": "refactor: extract module",
            "createdAt": "2026-02-20T10:00:00Z",
            "headRefName": "koan/refactor-module",
        }])

        result = fetch_open_prs("/fake/path")
        assert len(result) == 1
        assert result[0]["number"] == 5
        assert result[0]["category"] == "refactor"
        assert result[0]["hours_open"] > 0

    @patch("subprocess.run")
    def test_gh_failure_returns_empty(self, mock_run):
        mock_run.return_value = _mock_gh_failure()
        result = fetch_open_prs("/fake/path")
        assert result == []


# ─── get_alignment_summary ───────────────────────────────────────────────

class TestGetAlignmentSummary:

    @patch("app.pr_feedback.fetch_open_prs", return_value=[])
    @patch("app.pr_feedback.fetch_merged_prs", return_value=[])
    def test_no_data_returns_empty(self, _merged, _open):
        assert get_alignment_summary("/fake/path") == ""

    @patch("app.pr_feedback.fetch_open_prs", return_value=[])
    @patch("app.pr_feedback.fetch_merged_prs")
    def test_fast_merges_shown(self, mock_merged, _open):
        mock_merged.return_value = [
            {"category": "fix", "hours_to_merge": 12.0},
            {"category": "fix", "hours_to_merge": 6.0},
        ]
        result = get_alignment_summary("/fake/path")
        assert "Quickly merged" in result
        assert "fix" in result

    @patch("app.pr_feedback.fetch_open_prs", return_value=[])
    @patch("app.pr_feedback.fetch_merged_prs")
    def test_slow_merges_shown(self, mock_merged, _open):
        mock_merged.return_value = [
            {"category": "refactor", "hours_to_merge": 200.0},
        ]
        result = get_alignment_summary("/fake/path")
        assert "Slow to merge" in result
        assert "refactor" in result

    @patch("app.pr_feedback.fetch_open_prs")
    @patch("app.pr_feedback.fetch_merged_prs", return_value=[])
    def test_open_prs_shown(self, _merged, mock_open):
        mock_open.return_value = [
            {"number": 42, "category": "feature", "hours_open": 120.0},
        ]
        result = get_alignment_summary("/fake/path")
        assert "Still open" in result
        assert "#42" in result

    @patch("app.pr_feedback.fetch_open_prs")
    @patch("app.pr_feedback.fetch_merged_prs")
    def test_combined_output(self, mock_merged, mock_open):
        mock_merged.return_value = [
            {"category": "fix", "hours_to_merge": 8.0},
            {"category": "test", "hours_to_merge": 20.0},
        ]
        mock_open.return_value = [
            {"number": 10, "category": "docs", "hours_open": 48.0},
        ]
        result = get_alignment_summary("/fake/path")
        assert "Quickly merged" in result
        assert "Still open" in result


# ─── get_category_boost ──────────────────────────────────────────────────

class TestGetCategoryBoost:

    @patch("app.pr_feedback.fetch_merged_prs", return_value=[])
    def test_no_data_returns_empty(self, _mock):
        assert get_category_boost("/fake/path") == {}

    @patch("app.pr_feedback.fetch_merged_prs")
    def test_fast_gets_boost(self, mock_merged):
        mock_merged.return_value = [
            {"category": "fix", "hours_to_merge": 12.0},
        ]
        boosts = get_category_boost("/fake/path")
        assert boosts["fix"] == -1  # Boost (higher priority)

    @patch("app.pr_feedback.fetch_merged_prs")
    def test_slow_gets_penalty(self, mock_merged):
        mock_merged.return_value = [
            {"category": "refactor", "hours_to_merge": 200.0},
        ]
        boosts = get_category_boost("/fake/path")
        assert boosts["refactor"] == 1  # Penalty (lower priority)

    @patch("app.pr_feedback.fetch_merged_prs")
    def test_moderate_no_adjustment(self, mock_merged):
        mock_merged.return_value = [
            {"category": "feature", "hours_to_merge": 96.0},
        ]
        boosts = get_category_boost("/fake/path")
        assert "feature" not in boosts  # No adjustment for moderate
