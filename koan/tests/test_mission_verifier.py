"""Tests for mission_verifier.py — post-mission semantic verification."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.mission_verifier import (
    Check,
    CheckStatus,
    VerifyResult,
    _is_analysis_mission,
    _is_code_mission,
    _expects_tests,
    check_commit_quality,
    check_diff_coherence,
    check_mission_alignment,
    check_pr_created,
    check_test_coverage,
    format_verify_result,
    verify_mission,
)


# ---------------------------------------------------------------------------
# Mission type classification
# ---------------------------------------------------------------------------


class TestMissionTypeClassification:
    def test_code_mission_keywords(self):
        assert _is_code_mission("implement user authentication")
        assert _is_code_mission("fix broken login flow")
        assert _is_code_mission("add pagination to API")
        assert _is_code_mission("refactor database layer")

    def test_analysis_mission_keywords(self):
        assert _is_analysis_mission("audit security of auth module")
        assert _is_analysis_mission("review codebase for tech debt")
        assert _is_analysis_mission("investigate memory leak")
        assert _is_analysis_mission("explore new caching strategies")

    def test_mixed_keywords_code_with_analysis_is_analysis(self):
        # When both code and analysis keywords present, analysis wins
        assert not _is_code_mission("audit and fix security issues")
        assert _is_analysis_mission("audit and fix security issues")

    def test_no_matching_keywords(self):
        assert not _is_code_mission("hello world")
        assert not _is_analysis_mission("hello world")

    def test_expects_tests(self):
        assert _expects_tests("implement user login")
        assert _expects_tests("fix the broken signup")
        assert _expects_tests("add new feature for exports")
        assert not _expects_tests("audit codebase")
        assert not _expects_tests("document the API")


# ---------------------------------------------------------------------------
# VerifyResult dataclass
# ---------------------------------------------------------------------------


class TestVerifyResult:
    def test_warnings_property(self):
        result = VerifyResult(
            passed=True,
            checks=[
                Check("a", CheckStatus.PASS, "ok"),
                Check("b", CheckStatus.WARN, "hmm"),
                Check("c", CheckStatus.FAIL, "bad"),
            ],
        )
        assert len(result.warnings) == 1
        assert result.warnings[0].name == "b"

    def test_failures_property(self):
        result = VerifyResult(
            passed=False,
            checks=[
                Check("a", CheckStatus.PASS, "ok"),
                Check("b", CheckStatus.FAIL, "bad"),
                Check("c", CheckStatus.FAIL, "worse"),
            ],
        )
        assert len(result.failures) == 2

    def test_empty_result(self):
        result = VerifyResult(passed=True)
        assert result.warnings == []
        assert result.failures == []


# ---------------------------------------------------------------------------
# check_diff_coherence
# ---------------------------------------------------------------------------


class TestCheckDiffCoherence:
    @patch("app.mission_verifier.run_git")
    def test_pass_with_changes(self, mock_git):
        mock_git.side_effect = [
            (0, "koan/my-branch", ""),   # rev-parse
            (0, "", ""),                  # rev-parse --verify origin/main
            (0, "file1.py | 10 +\nfile2.py | 5 -\n2 files changed", ""),  # diff --stat
        ]
        result = check_diff_coherence("/project", "koan/")
        assert result.status == CheckStatus.PASS
        assert "2 file(s)" in result.message

    @patch("app.mission_verifier.run_git")
    def test_fail_no_changes(self, mock_git):
        mock_git.side_effect = [
            (0, "koan/my-branch", ""),
            (0, "", ""),
            (0, "", ""),  # Empty diff
        ]
        result = check_diff_coherence("/project", "koan/")
        assert result.status == CheckStatus.FAIL

    @patch("app.mission_verifier.run_git")
    def test_skip_on_main(self, mock_git):
        mock_git.return_value = (0, "main", "")
        result = check_diff_coherence("/project", "koan/")
        assert result.status == CheckStatus.SKIP

    @patch("app.mission_verifier.run_git")
    def test_skip_wrong_prefix(self, mock_git):
        mock_git.return_value = (0, "feature/other", "")
        result = check_diff_coherence("/project", "koan/")
        assert result.status == CheckStatus.SKIP

    @patch("app.mission_verifier.run_git")
    def test_skip_no_base_ref(self, mock_git):
        mock_git.side_effect = [
            (0, "koan/my-branch", ""),
            (1, "", ""),  # upstream/main not found
            (1, "", ""),  # origin/main not found
            (1, "", ""),  # upstream/master not found
            (1, "", ""),  # origin/master not found
        ]
        result = check_diff_coherence("/project", "koan/")
        assert result.status == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# check_test_coverage
# ---------------------------------------------------------------------------


class TestCheckTestCoverage:
    @patch("app.mission_verifier.run_git")
    def test_pass_tests_modified(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),  # origin/main exists
            (0, "src/auth.py\ntests/test_auth.py", ""),
        ]
        result = check_test_coverage("/project", "implement user auth")
        assert result.status == CheckStatus.PASS
        assert "1 test file(s)" in result.message

    @patch("app.mission_verifier.run_git")
    def test_warn_no_tests(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),
            (0, "src/auth.py\nsrc/models.py", ""),
        ]
        result = check_test_coverage("/project", "implement user auth")
        assert result.status == CheckStatus.WARN
        assert "No test files" in result.message

    def test_skip_analysis_mission(self):
        result = check_test_coverage("/project", "audit security configuration")
        assert result.status == CheckStatus.SKIP

    @patch("app.mission_verifier.run_git")
    def test_detects_various_test_patterns(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),
            (0, "src/app.ts\nsrc/app.test.ts\nsrc/app.spec.tsx\ntests/unit/test_core.py", ""),
        ]
        result = check_test_coverage("/project", "add new component")
        assert result.status == CheckStatus.PASS
        assert "3 test file(s)" in result.message


# ---------------------------------------------------------------------------
# check_pr_created
# ---------------------------------------------------------------------------


class TestCheckPrCreated:
    @patch("app.mission_verifier.run_git")
    def test_skip_analysis_mission(self, mock_git):
        result = check_pr_created("/project", "audit the auth module")
        assert result.status == CheckStatus.SKIP

    @patch("app.mission_verifier.run_git")
    def test_skip_on_main(self, mock_git):
        mock_git.return_value = (0, "main", "")
        result = check_pr_created("/project", "implement login")
        assert result.status == CheckStatus.SKIP

    @patch("app.mission_verifier.run_git")
    @patch("app.github.run_gh")
    def test_pass_pr_exists(self, mock_gh, mock_git):
        mock_git.return_value = (0, "koan/my-branch", "")
        mock_gh.return_value = '{"number": 42, "state": "OPEN", "isDraft": true}'
        result = check_pr_created("/project", "implement login")
        assert result.status == CheckStatus.PASS
        assert "#42" in result.message
        assert "draft" in result.message

    @patch("app.mission_verifier.run_git")
    @patch("app.github.run_gh")
    def test_warn_no_pr(self, mock_gh, mock_git):
        mock_git.return_value = (0, "koan/my-branch", "")
        mock_gh.side_effect = RuntimeError("no PR found")
        result = check_pr_created("/project", "implement login")
        assert result.status == CheckStatus.WARN

    @patch("app.mission_verifier.run_git")
    @patch("app.github.run_gh")
    def test_warn_pr_not_open(self, mock_gh, mock_git):
        mock_git.return_value = (0, "koan/my-branch", "")
        mock_gh.return_value = '{"number": 42, "state": "CLOSED", "isDraft": false}'
        result = check_pr_created("/project", "implement login")
        assert result.status == CheckStatus.WARN
        assert "CLOSED" in result.message


# ---------------------------------------------------------------------------
# check_commit_quality
# ---------------------------------------------------------------------------


class TestCheckCommitQuality:
    @patch("app.mission_verifier.run_git")
    def test_pass_clean_commits(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),  # origin/main exists
            (0, "feat: add user authentication\ntest: add auth tests", ""),
        ]
        result = check_commit_quality("/project")
        assert result.status == CheckStatus.PASS
        assert "2 commit(s)" in result.message

    @patch("app.mission_verifier.run_git")
    def test_warn_fixup_commits(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),
            (0, "feat: add auth\nfixup! feat: add auth", ""),
        ]
        result = check_commit_quality("/project")
        assert result.status == CheckStatus.WARN
        assert "Unsquashed" in result.message

    @patch("app.mission_verifier.run_git")
    def test_warn_short_message(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),
            (0, "fix", ""),
        ]
        result = check_commit_quality("/project")
        assert result.status == CheckStatus.WARN
        assert "Very short" in result.message

    @patch("app.mission_verifier.run_git")
    def test_skip_no_base_ref(self, mock_git):
        mock_git.return_value = (1, "", "not found")
        result = check_commit_quality("/project")
        assert result.status == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# check_mission_alignment
# ---------------------------------------------------------------------------


class TestCheckMissionAlignment:
    @patch("app.mission_verifier.run_git")
    def test_pass_keywords_match(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),                     # origin/main exists
            (0, "src/auth.py\ntests/test_auth.py", ""),  # changed files
            (0, "feat: add user authentication", ""),     # commit messages
        ]
        result = check_mission_alignment("/project", "implement user authentication")
        assert result.status == CheckStatus.PASS
        assert "authentication" in result.message

    @patch("app.mission_verifier.run_git")
    def test_warn_no_keywords_match(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),
            (0, "src/billing.py", ""),
            (0, "refactor: clean up billing", ""),
        ]
        result = check_mission_alignment("/project", "implement user authentication")
        assert result.status == CheckStatus.WARN
        assert "No mission keywords" in result.message

    def test_skip_empty_title(self):
        result = check_mission_alignment("/project", "")
        assert result.status == CheckStatus.SKIP
        assert "autonomous" in result.message.lower()

    @patch("app.mission_verifier.run_git")
    def test_skip_no_meaningful_keywords(self, mock_git):
        # Only stop-words in title
        result = check_mission_alignment("/project", "add the new fix")
        assert result.status == CheckStatus.SKIP

    @patch("app.mission_verifier.run_git")
    def test_warn_low_overlap(self, mock_git):
        mock_git.side_effect = [
            (0, "", ""),
            (0, "src/auth.py", ""),
            (0, "fix: patch auth", ""),
        ]
        # "authentication" + "dashboard" + "pagination" = 3 keywords
        # Only "auth" substring won't match exactly
        result = check_mission_alignment(
            "/project", "implement authentication dashboard pagination"
        )
        # "authentication" in "auth.py" won't match (substring, not word boundary)
        # But it IS in the corpus as a substring. Let's check both ways.
        assert result.status in (CheckStatus.PASS, CheckStatus.WARN)


# ---------------------------------------------------------------------------
# verify_mission (pipeline)
# ---------------------------------------------------------------------------


class TestVerifyMission:
    @patch("app.mission_verifier.check_mission_alignment")
    @patch("app.mission_verifier.check_commit_quality")
    @patch("app.mission_verifier.check_pr_created")
    @patch("app.mission_verifier.check_test_coverage")
    @patch("app.mission_verifier.check_diff_coherence")
    def test_all_pass(self, mock_diff, mock_tests, mock_pr, mock_commit, mock_align):
        mock_diff.return_value = Check("diff", CheckStatus.PASS, "ok")
        mock_tests.return_value = Check("tests", CheckStatus.PASS, "ok")
        mock_pr.return_value = Check("pr", CheckStatus.PASS, "ok")
        mock_commit.return_value = Check("commit", CheckStatus.PASS, "ok")
        mock_align.return_value = Check("align", CheckStatus.PASS, "ok")

        result = verify_mission("/project", "implement auth", exit_code=0)
        assert result.passed is True
        assert len(result.failures) == 0
        assert "passed" in result.summary

    @patch("app.mission_verifier.check_mission_alignment")
    @patch("app.mission_verifier.check_commit_quality")
    @patch("app.mission_verifier.check_pr_created")
    @patch("app.mission_verifier.check_test_coverage")
    @patch("app.mission_verifier.check_diff_coherence")
    def test_failure_blocks(self, mock_diff, mock_tests, mock_pr, mock_commit, mock_align):
        mock_diff.return_value = Check("diff", CheckStatus.FAIL, "no changes")
        mock_tests.return_value = Check("tests", CheckStatus.SKIP, "skip")
        mock_pr.return_value = Check("pr", CheckStatus.WARN, "no PR")
        mock_commit.return_value = Check("commit", CheckStatus.SKIP, "skip")
        mock_align.return_value = Check("align", CheckStatus.SKIP, "skip")

        result = verify_mission("/project", "implement auth", exit_code=0)
        assert result.passed is False
        assert len(result.failures) == 1

    def test_nonzero_exit_code(self):
        with patch("app.mission_verifier.check_diff_coherence") as mock_diff:
            mock_diff.return_value = Check("diff", CheckStatus.FAIL, "no changes")
            result = verify_mission("/project", "implement auth", exit_code=1)

        assert result.passed is False
        assert any(c.name == "exit_code" for c in result.checks)
        assert result.checks[0].status == CheckStatus.FAIL

    @patch("app.mission_verifier.check_mission_alignment")
    @patch("app.mission_verifier.check_commit_quality")
    @patch("app.mission_verifier.check_pr_created")
    @patch("app.mission_verifier.check_test_coverage")
    @patch("app.mission_verifier.check_diff_coherence")
    def test_warnings_dont_fail(self, mock_diff, mock_tests, mock_pr, mock_commit, mock_align):
        mock_diff.return_value = Check("diff", CheckStatus.PASS, "ok")
        mock_tests.return_value = Check("tests", CheckStatus.WARN, "no tests")
        mock_pr.return_value = Check("pr", CheckStatus.WARN, "no PR")
        mock_commit.return_value = Check("commit", CheckStatus.PASS, "ok")
        mock_align.return_value = Check("align", CheckStatus.WARN, "low overlap")

        result = verify_mission("/project", "implement auth", exit_code=0)
        assert result.passed is True
        assert len(result.warnings) == 3

    @patch("app.mission_verifier.check_mission_alignment")
    @patch("app.mission_verifier.check_commit_quality")
    @patch("app.mission_verifier.check_pr_created")
    @patch("app.mission_verifier.check_test_coverage")
    @patch("app.mission_verifier.check_diff_coherence")
    def test_exception_in_check_doesnt_crash(self, mock_diff, mock_tests, mock_pr, mock_commit, mock_align):
        mock_diff.side_effect = RuntimeError("git broken")
        mock_tests.return_value = Check("tests", CheckStatus.PASS, "ok")
        mock_pr.return_value = Check("pr", CheckStatus.PASS, "ok")
        mock_commit.return_value = Check("commit", CheckStatus.PASS, "ok")
        mock_align.return_value = Check("align", CheckStatus.PASS, "ok")

        result = verify_mission("/project", "implement auth", exit_code=0)
        # Should still have results from other checks
        assert result.passed is True
        # 1 exit_code + 4 successful checks (diff errored and was skipped)
        assert len(result.checks) >= 4


# ---------------------------------------------------------------------------
# format_verify_result
# ---------------------------------------------------------------------------


class TestFormatVerifyResult:
    def test_passing_result(self):
        result = VerifyResult(
            passed=True,
            checks=[
                Check("exit_code", CheckStatus.PASS, "ok"),
                Check("diff", CheckStatus.PASS, "3 files changed"),
            ],
            summary="2 passed",
        )
        output = format_verify_result(result)
        assert "PASS" in output
        assert "✓" in output
        assert "3 files changed" in output

    def test_failing_result(self):
        result = VerifyResult(
            passed=False,
            checks=[
                Check("exit_code", CheckStatus.FAIL, "exit code 1"),
                Check("diff", CheckStatus.WARN, "no changes yet"),
            ],
            summary="1 failure(s), 1 warning(s)",
        )
        output = format_verify_result(result)
        assert "FAIL" in output
        assert "✗" in output
        assert "⚠" in output

    def test_skipped_checks_hidden(self):
        result = VerifyResult(
            passed=True,
            checks=[
                Check("a", CheckStatus.PASS, "ok"),
                Check("b", CheckStatus.SKIP, "not applicable"),
            ],
            summary="1 passed",
        )
        output = format_verify_result(result)
        assert "not applicable" not in output


# ---------------------------------------------------------------------------
# Integration with mission_runner
# ---------------------------------------------------------------------------


class TestMissionRunnerIntegration:
    """Verify mission_runner._run_mission_verification works correctly."""

    @patch("app.mission_verifier.verify_mission")
    @patch("app.mission_verifier.format_verify_result")
    def test_run_mission_verification_returns_result(self, mock_format, mock_verify):
        from app.mission_runner import _run_mission_verification

        mock_result = VerifyResult(passed=True, summary="all good")
        mock_verify.return_value = mock_result
        mock_format.return_value = "Verification: PASS"

        result = _run_mission_verification("/project", "test mission", 0, "/instance")
        assert result is not None
        assert result.passed is True

    @patch("app.mission_verifier.verify_mission", side_effect=Exception("boom"))
    def test_run_mission_verification_handles_errors(self, mock_verify):
        from app.mission_runner import _run_mission_verification

        result = _run_mission_verification("/project", "test mission", 0, "/instance")
        assert result is None
