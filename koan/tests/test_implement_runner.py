"""Tests for implement_runner.py — the implement execution pipeline."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from app.github import fetch_issue_with_comments, detect_parent_repo
from app.issue_tracker.types import IssueContent, IssueRef
from app.issue_tracker import UnresolvedJiraProjectError
from app.plan_runner import ASSUMPTIONS_OK, ASSUMPTIONS_CRITICAL
from app.projects_config import get_project_submit_to_repository
from skills.core.implement.implement_runner import (
    run_implement,
    _GateImproved,
    _is_plan_content,
    _extract_latest_plan,
    _build_prompt,
    _execute_implementation,
    _generate_pr_summary,
    _is_plan_cache_fresh,
    _plan_hash,
    _plan_review_cache_path,
    _post_improved_plan,
    _run_plan_review_gate,
    _submit_implement_pr,
    _write_plan_cache,
    main,
)

# Shared helpers imported via app.pr_submit
from app.pr_submit import (
    get_current_branch,
    get_commit_subjects,
    get_fork_owner,
    guess_project_name,
    resolve_submit_target,
    submit_draft_pr,
)


_IMPL_MODULE = "skills.core.implement.implement_runner"
_PR_MODULE = "app.pr_submit"


def _github_issue(title="Title", body="Body", comments=None, key="42", repo="o/r"):
    """Build an IssueContent as the tracker's fetch_issue would return it."""
    ref = IssueRef(
        provider="github",
        url="https://github.com/o/r/issues/42",
        key=key,
        repo=repo,
    )
    return IssueContent(
        ref=ref, title=title, body=body, comments=comments or [], state="open",
    )


# ---------------------------------------------------------------------------
# _is_plan_content
# ---------------------------------------------------------------------------

class TestIsPlanContent:
    def test_empty_text(self):
        assert not _is_plan_content("")
        assert not _is_plan_content(None)

    def test_no_markers(self):
        assert not _is_plan_content("Just a random comment about the issue.")

    def test_phase_marker(self):
        assert _is_plan_content("Some text\n#### Phase 1: Setup\nDo things")

    def test_implementation_phases_marker(self):
        assert _is_plan_content("## Implementation Phases\n#### Phase 1")

    def test_summary_marker(self):
        assert _is_plan_content("### Summary\nThis plan does X")

    def test_changes_iteration_marker(self):
        assert _is_plan_content("### Changes in this iteration\n- Updated phase 2")

    def test_case_insensitive(self):
        assert _is_plan_content("## implementation phases\n#### Phase 1")
        assert _is_plan_content("### SUMMARY\nText here")


# ---------------------------------------------------------------------------
# _extract_latest_plan
# ---------------------------------------------------------------------------

class TestExtractLatestPlan:
    def test_plan_in_body_only(self):
        body = "## Summary\nDo the thing\n### Implementation Phases\n#### Phase 1: Start"
        result = _extract_latest_plan(body, [])
        assert result == body

    def test_plan_in_latest_comment(self):
        body = "Original issue description"
        comments = [
            {"body": "Nice idea!", "author": "user1", "date": "2026-01-01"},
            {"body": "### Summary\nOld plan v1\n#### Phase 1: Old", "author": "bot", "date": "2026-01-02"},
            {"body": "### Summary\nNew plan v2\n#### Phase 1: New", "author": "bot", "date": "2026-01-03"},
        ]
        result = _extract_latest_plan(body, comments)
        assert "New plan v2" in result
        assert "Old plan v1" not in result

    def test_ignores_non_plan_comments(self):
        body = "### Summary\nThe original plan"
        comments = [
            {"body": "Looks good!", "author": "reviewer", "date": "2026-01-01"},
            {"body": "Ship it", "author": "reviewer2", "date": "2026-01-02"},
        ]
        result = _extract_latest_plan(body, comments)
        assert "The original plan" in result

    def test_fallback_to_long_body_without_markers(self):
        body = "A" * 200  # Long body without plan markers
        result = _extract_latest_plan(body, [])
        assert result == body

    def test_empty_body_no_comments(self):
        result = _extract_latest_plan("", [])
        assert result == ""

    def test_short_body_without_markers(self):
        """Short bodies without markers are now returned as fallback."""
        result = _extract_latest_plan("Short text", [])
        assert result == "Short text"

    def test_plan_in_middle_comment_not_last(self):
        """The latest plan comment wins, even if non-plan comments follow."""
        body = "Issue body"
        comments = [
            {"body": "### Summary\nPlan v1\n#### Phase 1: Do it", "author": "bot", "date": "2026-01-01"},
            {"body": "Thanks for the plan!", "author": "human", "date": "2026-01-02"},
        ]
        result = _extract_latest_plan(body, comments)
        assert "Plan v1" in result

    def test_empty_comments_list(self):
        body = "### Implementation Phases\n#### Phase 1: Go"
        result = _extract_latest_plan(body, [])
        assert "Phase 1" in result

    def test_none_body_no_comments(self):
        """Issues with empty body (GitHub returns body=null) must not crash."""
        result = _extract_latest_plan(None, [])
        assert result == ""

    def test_none_body_with_plan_in_comment(self):
        """A plan in a comment is returned even when the issue body is None."""
        comments = [
            {"body": "### Summary\nPlan from comment", "author": "bot", "date": "2026-01-01"},
        ]
        result = _extract_latest_plan(None, comments)
        assert "Plan from comment" in result

    @staticmethod
    def _published_part(plan, part, count, updated, navigation=""):
        """A comment exactly as `jira_plan_publish` renders it.

        Built through the real renderer rather than a hand-written string, so a
        change to the published format fails these tests instead of silently
        leaving `/implement` unable to recognise its own plans.
        """
        from app.jira_plan_publish import _render_comment, _revision

        return {
            "body": _render_comment(plan, _revision(plan), part, count, navigation),
            "updated": updated,
        }

    def test_jira_multipart_plan_is_concatenated_in_part_order(self):
        plan = "## Summary\nfirst half\nsecond half"
        stamp = "2026-07-31T12:01:00.000+0000"
        comments = [
            self._published_part(plan, 2, 2, stamp, "Previous part: https://j/x?focusedCommentId=1"),
            self._published_part(plan, 1, 2, stamp, "Next part: https://j/x?focusedCommentId=2"),
        ]
        # Each part carries the whole body in this fixture; distinguish by order.
        comments[0]["body"] = comments[0]["body"].replace("## Summary\nfirst half\n", "")
        comments[1]["body"] = comments[1]["body"].replace("\nsecond half", "")

        result = _extract_latest_plan("Issue body", comments)

        assert result.index("first half") < result.index("second half")
        assert "Part 1 of 2" not in result
        assert "Koan current plan (rev" not in result
        assert "focusedCommentId" not in result

    def test_jira_multipart_plan_uses_newest_revision_group(self):
        old, new = "old plan text", "new plan text"
        comments = [
            self._published_part(old, 1, 3, "2026-07-30T12:00:00.000+0000"),
            self._published_part(new, 1, 2, "2026-07-31T12:00:00.000+0000"),
            self._published_part(new, 2, 2, "2026-07-31T12:01:00.000+0000"),
        ]

        result = _extract_latest_plan("Issue body", comments)

        assert "new plan text" in result
        assert "old plan text" not in result

    def test_jira_incomplete_multipart_plan_uses_available_parts(self):
        comments = [self._published_part("available", 2, 3, "2026-07-31T12:00:00.000+0000")]

        assert _extract_latest_plan("Issue body", comments) == "available"

    def test_single_part_jira_plan_is_not_treated_as_multipart(self):
        comments = [self._published_part("## Summary\nthe whole plan", 1, 1, "2026-07-31T12:00:00.000+0000")]

        assert "the whole plan" in _extract_latest_plan("Issue body", comments)

    def test_split_plan_survives_the_full_publish_and_fetch_round_trip(self):
        """Publish → Jira ADF → fetch → reassemble must return the plan intact.

        The publisher and this reader agree on a format; Jira's ADF conversion
        sits between them. Exercising all three together is what catches a
        format change that leaves `/implement` silently unable to find a plan.
        """
        from app.jira_notifications import _adf_to_markdown, markdown_to_adf
        from app.jira_plan_publish import _plan_parts, _render_comment, _revision

        plan = (
            "## Summary\nDo the thing.\n\n#### Phase 1: setup\n\n"
            "```python\nx = 1\n```\n" + "\n".join(f"- step {i}" for i in range(3000))
        )
        parts = _plan_parts(plan)
        revision = _revision(plan)
        assert len(parts) > 1, "fixture must be large enough to split"

        comments = []
        for number, part in enumerate(parts, 1):
            navigation = (
                f"Next part: https://j/x?focusedCommentId={number + 1}"
                if number < len(parts) else ""
            )
            rendered = _render_comment(part, revision, number, len(parts), navigation)
            comments.append({
                # What Jira stores and hands back through fetch_jira_issue.
                "body": _adf_to_markdown(markdown_to_adf(rendered)),
                "updated": f"2026-07-31T12:0{number}:00.000+0000",
            })

        result = _extract_latest_plan("Issue body", comments)

        assert "## Summary" in result
        assert "```python" in result and "x = 1" in result
        assert "step 0" in result and "step 2999" in result
        assert "Part 1 of" not in result
        assert "focusedCommentId" not in result
        assert "Koan current plan (rev" not in result


# ---------------------------------------------------------------------------
# fetch_issue_with_comments (now in github.py)
# ---------------------------------------------------------------------------

class TestFetchIssueWithComments:
    def test_successful_fetch(self):
        issue_data = json.dumps({"title": "My Plan", "body": "The plan body"})
        comments_data = json.dumps([
            {"author": "user1", "date": "2026-01-01", "body": "Nice!"}
        ])
        with patch("app.github.api", side_effect=[issue_data, comments_data]):
            title, body, comments = fetch_issue_with_comments("owner", "repo", "42")
            assert title == "My Plan"
            assert body == "The plan body"
            assert len(comments) == 1
            assert comments[0]["author"] == "user1"

    def test_malformed_issue_json(self):
        with patch("app.github.api", side_effect=["not json", "[]"]):
            title, body, comments = fetch_issue_with_comments("o", "r", "1")
            assert title == ""
            assert body == "not json"
            assert comments == []

    def test_malformed_comments_json(self):
        issue_data = json.dumps({"title": "T", "body": "B"})
        with patch("app.github.api", side_effect=[issue_data, "bad"]):
            title, body, comments = fetch_issue_with_comments("o", "r", "1")
            assert title == "T"
            assert comments == []

    def test_empty_comments(self):
        issue_data = json.dumps({"title": "T", "body": "B"})
        with patch("app.github.api", side_effect=[issue_data, "[]"]):
            _, _, comments = fetch_issue_with_comments("o", "r", "1")
            assert comments == []

    def test_null_body_normalized_to_empty_string(self):
        """GitHub returns body=null for issues with empty body — must coerce to ''."""
        issue_data = json.dumps({"title": "T", "body": None})
        with patch("app.github.api", side_effect=[issue_data, "[]"]):
            _, body, _ = fetch_issue_with_comments("o", "r", "1")
            assert body == ""


# ---------------------------------------------------------------------------
# _run_plan_review_gate
# ---------------------------------------------------------------------------

class TestPlanReviewGate:
    """Tests for the plan-review quality gate in implement_runner."""

    def test_approved_plan_proceeds(self):
        """When review_plan returns APPROVED, gate returns None (proceed)."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate("## Phase 1\nDo stuff\n" * 10, "/project")
            assert result is None

    def test_issues_found_triggers_improvement_then_proceeds(self):
        """When review finds issues, gate improves plan and proceeds (fail open)."""
        issues = "- Phase 1: missing file paths"
        improved = "## Phase 1: Update koan/app/foo.py\nDo stuff"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, issues), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value=improved), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            result = _run_plan_review_gate("## Phase 1\nDo stuff\n" * 10, "/project")
            assert isinstance(result, _GateImproved)
            assert result.plan == improved
            assert "missing file paths" in result.issues_fixed

    def test_issues_found_notifies_telegram_about_improvement(self):
        """When gate finds issues, notify_fn is called about auto-improvement."""
        issues = "- Phase 1: missing file paths"
        notify = MagicMock()
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, issues), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value="improved"), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            _run_plan_review_gate(
                "## Phase 1\nDo stuff\n" * 10, "/project", notify_fn=notify,
            )
            notify.assert_called_once()
            assert "auto-improving" in notify.call_args[0][0]

    def test_improvement_posts_improved_plan_to_tracker(self):
        """When gate improves plan, posts improved version via the tracker."""
        issues = "- Phase 1: missing file paths"
        improved = "## Phase 1: Update koan/app/foo.py\nFixed plan"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, issues), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value=improved), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}.add_comment") as mock_comment:
            _run_plan_review_gate(
                "## Phase 1\nDo stuff\n" * 10, "/project",
                issue_url="https://github.com/o/r/issues/42",
            )
            mock_comment.assert_called_once()
            args = mock_comment.call_args[0]
            assert args[0] == "https://github.com/o/r/issues/42"
            assert "Improved" in args[1]
            assert improved in args[1]

    def test_notify_failure_does_not_block_improvement(self):
        """notify_fn exception doesn't prevent gate from proceeding."""
        notify = MagicMock(side_effect=RuntimeError("send failed"))
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, "issues"), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value="improved"), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            result = _run_plan_review_gate(
                "## Phase 1\nDo stuff\n" * 10, "/project", notify_fn=notify,
            )
            assert isinstance(result, _GateImproved)
            assert result.plan == "improved"

    def test_comment_failure_does_not_block_gate(self):
        """A tracker comment exception doesn't prevent gate from proceeding."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, "issues"), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value="improved"), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}.add_comment", side_effect=RuntimeError("post failed")):
            result = _run_plan_review_gate(
                "## Phase 1\nDo stuff\n" * 10, "/project",
                issue_url="https://github.com/o/r/issues/42",
            )
            assert isinstance(result, _GateImproved)
            assert result.plan == "improved"

    def test_simple_plan_skips_review(self):
        """Simple plans bypass the review gate entirely — no config read needed."""
        with patch("app.plan_runner.is_simple_plan", return_value=True), \
             patch("app.config.get_plan_review_config") as mock_cfg, \
             patch("app.plan_runner.review_plan") as mock_review:
            result = _run_plan_review_gate("Rename X to Y", "/project")
            assert result is None
            mock_cfg.assert_not_called()
            mock_review.assert_not_called()

    def test_config_disabled_skips_review(self):
        """When implement_gate is False, gate is skipped."""
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": False}), \
             patch("app.plan_runner.review_plan") as mock_review:
            result = _run_plan_review_gate("## Phase 1\nBig plan", "/project")
            assert result is None
            mock_review.assert_not_called()

    def test_reviewer_error_fails_open(self):
        """When review_plan fails open (returns approved=True on error), proceed."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate("## Phase 1\nDo stuff\n" * 10, "/project")
            assert result is None

    def test_gate_improved_plan_used_for_implementation(self):
        """Integration: run_implement uses improved plan and context from gate."""
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        improved = "## Phase 1: koan/app/foo.py\nImproved plan"
        gate_result = _GateImproved(improved, "- missing file paths")
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate",
                    return_value=gate_result), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="done") as mock_exec, \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            call_kwargs = mock_exec.call_args[1]
            assert call_kwargs["plan"] == improved
            assert "Plan Improvement Notes" in call_kwargs["context"]
            assert "missing file paths" in call_kwargs["context"]

    def test_assumptions_advisory_injected_into_implementation_context(self):
        """Integration: an assumptions advisory reaches the implementation context."""
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        advisory = "1. [UNVERIFIED/CRITICAL] popen_cli is called without env="

        def gate_with_advisory(plan, *args, **kwargs):
            return _GateImproved(plan, "", advisory)

        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate",
                    side_effect=gate_with_advisory), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="done") as mock_exec, \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            call_kwargs = mock_exec.call_args[1]
            assert "Assumption Verification Required" in call_kwargs["context"]
            assert advisory in call_kwargs["context"]
            # Plan unchanged, no critic issues — no misleading improvement notes
            assert "Plan Improvement Notes" not in call_kwargs["context"]


# ---------------------------------------------------------------------------
# Plan assumptions check (advisory pressure-test)
# ---------------------------------------------------------------------------

class TestPlanAssumptionsGate:
    """Tests for the advisory assumptions pressure-test in _run_plan_review_gate."""

    _PLAN = "## Phase 1\nDo stuff\n" * 10

    def test_critical_assumption_is_advisory_not_blocking(self):
        """A critical unverified assumption is advisory: gate proceeds (fail open)."""
        reason = "3. [UNVERIFIED/CRITICAL] The timeout is caused by a socket leak"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "assumptions_check": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, reason)), \
             patch("app.plan_runner.review_plan", return_value=(True, "")) as mock_review, \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert not isinstance(result, tuple)
            assert isinstance(result, _GateImproved)
            assert result.plan == self._PLAN
            assert "socket leak" in result.assumptions_advisory
            # Advisory falls through to the structural critic, not around it
            mock_review.assert_called_once()

    def test_assumptions_ok_proceeds_to_structural_critic(self):
        """When assumptions pass, gate continues to structural critic."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "assumptions_check": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert result is None

    def test_assumptions_check_disabled_skips(self):
        """When assumptions_check is False, skip directly to structural critic."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "assumptions_check": False}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions") as mock_assumptions, \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert result is None
            mock_assumptions.assert_not_called()

    def test_assumptions_default_enabled(self):
        """When assumptions_check key is absent from config, check still runs."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_OK, "")) as mock_assumptions, \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            _run_plan_review_gate(self._PLAN, "/project")
            mock_assumptions.assert_called_once()

    def test_critical_assumption_notifies_user(self):
        """When a critical assumption is flagged, notify_fn announces the advisory."""
        notify = MagicMock()
        reason = "Plan assumes API endpoint /v2/users exists but this is unverified"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, reason)), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(
                self._PLAN, "/project", notify_fn=notify,
            )
            assert isinstance(result, _GateImproved)
            assert result.assumptions_advisory == reason
            notify.assert_called_once()
            message = notify.call_args[0][0].lower()
            assert "assumptions flagged" in message
            assert "proceeding" in message

    def test_reviewer_error_fails_open(self):
        """Reviewer infrastructure failure falls through to structural critic (fail-open)."""
        from app.plan_runner import ASSUMPTIONS_REVIEWER_ERROR
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_REVIEWER_ERROR, "assumptions subagent failed: model unavailable")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")) as mock_review, \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert result is None
            mock_review.assert_called_once()

    def test_reviewer_error_notifies_user(self):
        """Reviewer-error fail-open notifies the operator so degradation is visible."""
        from app.plan_runner import ASSUMPTIONS_REVIEWER_ERROR
        notify = MagicMock()
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_REVIEWER_ERROR, "model unavailable")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(
                self._PLAN, "/project", notify_fn=notify,
            )
            assert result is None
            notify.assert_called_once()
            assert "reviewer error" in notify.call_args[0][0].lower()

    def test_notify_failure_does_not_lose_advisory(self):
        """notify_fn exception doesn't prevent the advisory from propagating."""
        notify = MagicMock(side_effect=RuntimeError("send failed"))
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, "bad assumption")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"):
            result = _run_plan_review_gate(
                self._PLAN, "/project", notify_fn=notify,
            )
            assert isinstance(result, _GateImproved)
            assert result.assumptions_advisory == "bad assumption"

    def test_advisory_threaded_through_improved_plan(self):
        """Advisory survives when the structural critic also improves the plan."""
        improved = "## Phase 1: koan/app/foo.py\nConcrete plan"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, "unproven root cause")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, "- vague"), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value=improved), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert isinstance(result, _GateImproved)
            assert result.plan == improved
            assert result.assumptions_advisory == "unproven root cause"


# ---------------------------------------------------------------------------
# Plan review improvement loop
# ---------------------------------------------------------------------------

class TestPlanReviewImprovementLoop:
    """Tests for the autonomous plan improvement loop in the review gate."""

    _PLAN = "## Phase 1\nDo stuff\n" * 10

    def test_improvement_loop_succeeds_on_second_round(self):
        """Improve once, second review passes — returns improved plan."""
        improved = "## Phase 1: koan/app/foo.py\nConcrete plan"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, "missing paths"), (True, "")]), \
             patch("app.plan_runner.improve_plan", return_value=improved), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache") as mock_cache, \
             patch(f"{_IMPL_MODULE}._post_improved_plan") as mock_post:
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert isinstance(result, _GateImproved)
            assert result.plan == improved
            assert "missing paths" in result.issues_fixed
            mock_cache.assert_called_once()
            mock_post.assert_called_once()

    def test_improvement_loop_exhausts_all_rounds_fails_open(self):
        """All rounds fail — returns improved plan anyway (fail open)."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 2}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(False, "issues")), \
             patch("app.plan_runner.improve_plan", return_value="better but not perfect"), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache") as mock_cache, \
             patch(f"{_IMPL_MODULE}._post_improved_plan") as mock_post:
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert isinstance(result, _GateImproved)
            assert result.plan == "better but not perfect"
            mock_cache.assert_not_called()
            mock_post.assert_called_once()

    def test_exhausted_with_unchanged_plan_returns_none(self):
        """If improve_plan returns the same text, gate returns None (use original)."""
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 2}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(False, "issues")), \
             patch("app.plan_runner.improve_plan", return_value=self._PLAN), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache") as mock_cache, \
             patch(f"{_IMPL_MODULE}._post_improved_plan") as mock_post:
            result = _run_plan_review_gate(self._PLAN, "/project")
            assert result is None
            mock_cache.assert_not_called()
            mock_post.assert_not_called()

    def test_improve_plan_called_with_issues(self):
        """improve_plan receives the issues text from the reviewer."""
        issues = "- Phase 1: no file paths\n- Phase 3: too large"
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 3}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan",
                    side_effect=[(False, issues), (True, "")]), \
             patch("app.plan_runner.improve_plan",
                    return_value="fixed") as mock_improve, \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._write_plan_cache"), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            _run_plan_review_gate(self._PLAN, "/project")
            mock_improve.assert_called_once()
            args = mock_improve.call_args[0]
            assert args[0] == self._PLAN
            assert args[1] == issues
            assert args[2] == "/project"

    def test_improvement_not_called_on_last_round(self):
        """On the final round, don't waste tokens improving — just fail open."""
        call_count = 0

        def counting_improve(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return "improved"

        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 2}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(False, "issues")), \
             patch("app.plan_runner.improve_plan", side_effect=counting_improve), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            _run_plan_review_gate(self._PLAN, "/project")
            # max_rounds=2: round 1 review fails → improve, round 2 review fails → stop
            assert call_count == 1

    def test_exhaustion_notifies_user(self):
        """When all rounds exhausted, user is notified about fail-open."""
        notify = MagicMock()
        with patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 2}), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(False, "issues")), \
             patch("app.plan_runner.improve_plan", return_value="improved"), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            _run_plan_review_gate(self._PLAN, "/project", notify_fn=notify)
            # First call: improvement notification, second: exhaustion notification
            assert notify.call_count == 2
            exhaustion_msg = notify.call_args_list[1][0][0]
            assert "couldn't fully resolve" in exhaustion_msg


# ---------------------------------------------------------------------------
# Plan review cache
# ---------------------------------------------------------------------------

class TestPlanReviewCache:
    """Tests for content-hash caching in the plan-review gate."""

    def test_plan_hash_deterministic(self):
        """Same plan text always produces the same hash."""
        plan = "## Phase 1\nDo stuff\n## Phase 2\nMore stuff"
        assert _plan_hash(plan) == _plan_hash(plan)

    def test_plan_hash_strips_whitespace(self):
        """Leading/trailing whitespace doesn't affect hash."""
        assert _plan_hash("  plan  ") == _plan_hash("plan")

    def test_plan_hash_differs_for_different_plans(self):
        """Different plan text produces different hashes."""
        assert _plan_hash("plan A") != _plan_hash("plan B")

    def test_cache_path_is_project_specific(self):
        """Cache path includes the project name."""
        with patch(f"{_IMPL_MODULE}.guess_project_name", return_value="myproj"):
            path = _plan_review_cache_path("/some/project")
            assert "myproj" in path.name

    def test_is_plan_cache_fresh_no_file(self, tmp_path):
        """Returns False when no cache file exists."""
        with patch(f"{_IMPL_MODULE}._plan_review_cache_path",
                    return_value=tmp_path / "nonexistent"):
            assert not _is_plan_cache_fresh("/project", "abc123")

    def test_is_plan_cache_fresh_match(self, tmp_path):
        """Returns True when cached hash matches."""
        cache_file = tmp_path / ".plan-review-hash-myproj"
        cache_file.write_text("abc123\n")
        with patch(f"{_IMPL_MODULE}._plan_review_cache_path",
                    return_value=cache_file):
            assert _is_plan_cache_fresh("/project", "abc123")

    def test_is_plan_cache_fresh_mismatch(self, tmp_path):
        """Returns False when cached hash differs."""
        cache_file = tmp_path / ".plan-review-hash-myproj"
        cache_file.write_text("old_hash\n")
        with patch(f"{_IMPL_MODULE}._plan_review_cache_path",
                    return_value=cache_file):
            assert not _is_plan_cache_fresh("/project", "new_hash")

    def test_write_plan_cache(self, tmp_path):
        """write_plan_cache persists the hash to disk."""
        cache_file = tmp_path / ".plan-review-hash-myproj"
        with patch(f"{_IMPL_MODULE}._plan_review_cache_path",
                    return_value=cache_file):
            _write_plan_cache("/project", "deadbeef")
            assert cache_file.read_text().strip() == "deadbeef"

    def test_cache_hit_skips_review(self):
        """When cache is fresh, review_plan is never called."""
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=True), \
             patch("app.plan_runner.review_plan") as mock_review:
            result = _run_plan_review_gate("## Phase 1\nBig plan", "/project")
            assert result is None
            mock_review.assert_not_called()

    def test_approved_writes_cache(self):
        """When review approves, cache is written."""
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True}), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch(f"{_IMPL_MODULE}._write_plan_cache") as mock_write:
            _run_plan_review_gate("## Phase 1\nDo stuff", "/project")
            mock_write.assert_called_once()

    def test_exhausted_rounds_does_not_write_cache(self):
        """When improvement exhausts all rounds (fail open), cache is NOT written."""
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"implement_gate": True, "max_rounds": 2}), \
             patch(f"{_IMPL_MODULE}._is_plan_cache_fresh", return_value=False), \
             patch("app.plan_runner.review_plan_assumptions", return_value=(ASSUMPTIONS_OK, "")), \
             patch("app.plan_runner.review_plan", return_value=(False, "issues")), \
             patch("app.plan_runner.improve_plan", return_value="still bad"), \
             patch(f"{_IMPL_MODULE}._write_plan_cache") as mock_write, \
             patch(f"{_IMPL_MODULE}._post_improved_plan"):
            _run_plan_review_gate("## Phase 1\nDo stuff\n" * 10, "/project")
            mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# _build_prompt + _execute_implementation
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_uses_skill_prompt_when_skill_dir_given(self):
        skill_dir = Path("/fake/skill/dir")
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill", return_value="prompt") as mock_load:
            result = _build_prompt(
                "http://url", "Title", "Plan", "Context",
                skill_dir=skill_dir,
            )
            mock_load.assert_called_once_with(
                skill_dir, "implement",
                project_path=None,
                ISSUE_URL="http://url",
                ISSUE_TITLE="Title",
                PLAN="Plan",
                CONTEXT="Context",
                BRANCH_PREFIX="koan/",
                ISSUE_NUMBER="",
                PROJECT_MEMORY="",
                BASE_BRANCH="main",
            )
            assert result == "prompt"

    def test_uses_global_prompt_when_no_skill_dir(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill", return_value="prompt") as mock_load:
            result = _build_prompt(
                "http://url", "Title", "Plan", "Context",
            )
            mock_load.assert_called_once_with(
                None, "implement",
                project_path=None,
                ISSUE_URL="http://url",
                ISSUE_TITLE="Title",
                PLAN="Plan",
                CONTEXT="Context",
                BRANCH_PREFIX="koan/",
                ISSUE_NUMBER="",
                PROJECT_MEMORY="",
                BASE_BRANCH="main",
            )
            assert result == "prompt"

    def test_passes_project_path_for_koan_skill_injection(self):
        """project_path must reach the loader so .koan/skills/implement/*.md applies."""
        skill_dir = Path("/fake/skill/dir")
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill", return_value="prompt") as mock_load:
            _build_prompt(
                "http://url", "Title", "Plan", "Context",
                skill_dir=skill_dir,
                project_path="/repo/koan",
            )
            assert mock_load.call_args.kwargs["project_path"] == "/repo/koan"

    def test_base_branch_propagates_into_prompt_template_vars(self):
        """BASE_BRANCH must reach load_prompt_or_skill so the rendered prompt
        names the project's actual base (e.g. `staging`) — otherwise Claude
        falls back to the hardcoded main/master branch-creation rule and
        commits onto the base branch (regression seen on aback)."""
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill", return_value="p") as mock_load:
            _build_prompt(
                "http://url", "Title", "Plan", "Context",
                base_branch="staging",
            )
            assert mock_load.call_args.kwargs["BASE_BRANCH"] == "staging"

    def test_prompt_includes_pr_creation_step(self):
        """implement.md must instruct Claude to push and create a draft PR."""
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "implement"
        from app.prompts import load_skill_prompt

        prompt = load_skill_prompt(
            skill_dir, "implement",
            ISSUE_URL="https://github.com/o/r/issues/42",
            ISSUE_TITLE="Test",
            PLAN="Plan text",
            CONTEXT="ctx",
            BRANCH_PREFIX="koan/",
            ISSUE_NUMBER="42",
            BASE_BRANCH="main",
        )
        assert "gh pr create --draft" in prompt
        assert "git push" in prompt
        assert "Closes https://github.com/o/r/issues/42" in prompt
        assert "{KOAN_PYTHON}" not in prompt
        assert " -m app.issue_cli" in prompt

    def test_prompt_mentions_resolved_base_branch_so_claude_branches_off_it(self):
        """When the project's base branch is not main/master (e.g. staging),
        the rendered prompt MUST tell Claude that committing on that branch
        is forbidden. Otherwise the branch-creation rule never triggers and
        Claude commits straight onto staging."""
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "implement"
        from app.prompts import load_skill_prompt

        prompt = load_skill_prompt(
            skill_dir, "implement",
            ISSUE_URL="https://github.com/o/r/issues/42",
            ISSUE_TITLE="Test",
            PLAN="Plan text",
            CONTEXT="ctx",
            BRANCH_PREFIX="koan/",
            ISSUE_NUMBER="42",
            BASE_BRANCH="staging",
        )
        assert "staging" in prompt
        assert "Never commit on `staging`" in prompt


class TestExecuteImplementation:
    def test_passes_correct_run_command_params(self):
        with patch(f"{_IMPL_MODULE}._build_prompt", return_value="prompt"), \
             patch("app.cli_provider.run_command_streaming", return_value="ok") as mock_run:
            result = _execute_implementation(
                "/project", "url", "t", "p", "c",
                skill_dir=Path("/skill"),
            )
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[0][0] == "prompt"
            assert call_kwargs[0][1] == "/project"
            assert call_kwargs[1]["model_key"] == "mission"
            assert call_kwargs[1]["max_turns"] == 200
            assert call_kwargs[1]["timeout"] == 7200
            assert result == "ok"

    def test_passes_allowed_tools(self):
        """run_command must receive allowed_tools covering full CLAUDE_TOOLS set."""
        with patch(f"{_IMPL_MODULE}._build_prompt", return_value="p"), \
             patch("app.cli_provider.run_command_streaming", return_value="ok") as mock_run:
            _execute_implementation("/project", "url", "t", "p", "c")
            call_args = mock_run.call_args
            tools = call_args[1].get("allowed_tools") or call_args[0][2]
            for tool in ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]:
                assert tool in tools, f"{tool} missing from allowed_tools"


# ---------------------------------------------------------------------------
# run_implement — top-level
# ---------------------------------------------------------------------------

class TestRunImplement:
    def test_invalid_url(self):
        ok, msg = run_implement("/project", "not-a-url", notify_fn=MagicMock())
        assert not ok
        assert "Invalid" in msg

    def test_unmapped_jira_project_notifies_and_fails(self):
        notify = MagicMock()
        with patch(
            f"{_IMPL_MODULE}.fetch_issue",
            side_effect=UnresolvedJiraProjectError(
                "Unmapped Jira issue 'PROJ-42': no Koan project was resolved. "
                "Add this mapping in projects.yaml under projects.<name>.issue_tracker "
                "with provider: jira and jira_project: PROJ.",
            ),
        ):
            ok, msg = run_implement(
                "/project",
                "https://org.atlassian.net/browse/PROJ-42",
                notify_fn=notify,
            )
            assert not ok
            assert "projects.yaml" in msg
            assert "PROJ-42" in msg
            notify.assert_called_once()

    def test_no_plan_found(self):
        notify = MagicMock()
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body="")):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "No plan found" in msg

    def test_successful_implementation(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "#42" in msg

    def test_with_context(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done") as mock_run, \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                context="Phase 1 to 3",
                notify_fn=notify,
            )
            assert ok
            assert "Phase 1 to 3" in msg
            _, kwargs = mock_run.call_args
            assert kwargs.get("context") == "Phase 1 to 3" or \
                   mock_run.call_args[0][3] == "Phase 1 to 3"

    def test_fetch_failure(self):
        notify = MagicMock()
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    side_effect=RuntimeError("API error")):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "Failed to fetch" in msg

    def test_claude_failure(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    side_effect=RuntimeError("Timeout")):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "Implementation failed" in msg

    def test_empty_claude_output_triggers_retry_then_soft_failure(self):
        """Empty output from both passes → soft failure notification, no blocker keywords."""
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value=""), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects", return_value=[]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="main"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            # Soft failure — no harsh blocker-keyword phrases
            all_notified = " ".join(c.args[0] for c in notify.call_args_list)
            assert "BLOCKED" not in all_notified
            assert "could not execute" not in all_notified.lower()
            assert "no PR" not in all_notified

    def test_default_context_when_none(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done") as mock_run, \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"):
            run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            args = mock_run.call_args
            assert "Implement the full plan." in str(args)

    def test_notify_messages(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None):
            run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                context="Phase 1",
                notify_fn=notify,
            )
            messages = [c.args[0] for c in notify.call_args_list]
            # First message: the "Implementing #42..." kickoff.
            assert "#42" in messages[0]
            assert "Phase 1" in messages[0]
            # Last message: the final implementation summary, also tagged #42.
            assert "#42" in messages[-1]
            assert "Phase 1" in messages[-1]

    def test_explicit_project_name_reaches_tracker_and_memory(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)) as fetch, \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                   return_value="Done") as execute, \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                   return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                   return_value="koan/implement-42"):
            run_implement(
                "/workspace/webpros-shield",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
                project_name="webpros-shield",
                instance_dir="/koan/instance",
            )

        assert fetch.call_args.kwargs["project_name"] == "webpros-shield"
        assert execute.call_args.kwargs["project_name"] == "webpros-shield"
        assert execute.call_args.kwargs["instance_dir"] == "/koan/instance"


# ---------------------------------------------------------------------------
# guess_project_name (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGuessProjectName:
    def test_extracts_dir_name(self):
        assert guess_project_name("/Users/me/workspace/koan") == "koan"

    def test_simple_path(self):
        assert guess_project_name("/tmp/myproject") == "myproject"


# ---------------------------------------------------------------------------
# get_current_branch (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGetCurrentBranch:
    def test_returns_branch_name(self):
        with patch(f"{_PR_MODULE}._git_get_current_branch",
                    return_value="koan/implement-42"):
            assert get_current_branch("/project") == "koan/implement-42"

    def test_returns_main_on_error(self):
        with patch(f"{_PR_MODULE}._git_get_current_branch",
                    return_value="main"):
            assert get_current_branch("/project") == "main"


# ---------------------------------------------------------------------------
# get_commit_subjects (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGetCommitSubjects:
    def test_returns_subjects(self):
        with patch(f"{_PR_MODULE}._git_get_commit_subjects",
                    return_value=["feat: add X", "fix: broken Y"]):
            result = get_commit_subjects("/project")
            assert result == ["feat: add X", "fix: broken Y"]

    def test_returns_empty_on_error(self):
        with patch(f"{_PR_MODULE}._git_get_commit_subjects",
                    return_value=[]):
            assert get_commit_subjects("/project") == []

    def test_returns_empty_for_no_output(self):
        with patch(f"{_PR_MODULE}._git_get_commit_subjects",
                    return_value=[]):
            assert get_commit_subjects("/project") == []


# ---------------------------------------------------------------------------
# get_fork_owner (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGetForkOwner:
    def test_returns_owner(self):
        with patch(f"{_PR_MODULE}.run_gh",
                    return_value="atoomic"):
            assert get_fork_owner("/project") == "atoomic"

    def test_returns_empty_on_error(self):
        with patch(f"{_PR_MODULE}.run_gh",
                    side_effect=RuntimeError("gh failed")):
            assert get_fork_owner("/project") == ""


# ---------------------------------------------------------------------------
# resolve_submit_target (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestResolveSubmitTarget:
    def test_config_based_target(self):
        config = {
            "defaults": {},
            "projects": {
                "myapp": {
                    "path": "/project",
                    "submit_to_repository": {"repo": "upstream/myapp", "remote": "upstream"},
                }
            },
        }
        with patch("app.projects_config.load_projects_config",
                    return_value=config), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            target = resolve_submit_target("/project", "myapp", "fork-owner", "myapp")
            assert target == {"repo": "upstream/myapp", "is_fork": True}

    def test_auto_detect_fork(self):
        with patch("app.projects_config.load_projects_config",
                    return_value=None), \
             patch(f"{_PR_MODULE}.resolve_target_repo",
                    return_value="parent-owner/repo"), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            target = resolve_submit_target("/project", "myapp", "o", "r")
            assert target == {"repo": "parent-owner/repo", "is_fork": True}

    def test_fallback_to_issue_repo(self):
        with patch("app.projects_config.load_projects_config",
                    return_value=None), \
             patch(f"{_PR_MODULE}.resolve_target_repo",
                    return_value=None), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            target = resolve_submit_target("/project", "myapp", "owner", "repo")
            assert target == {"repo": "owner/repo", "is_fork": False}

    def test_no_koan_root(self):
        with patch(f"{_PR_MODULE}.resolve_target_repo",
                    return_value=None), \
             patch.dict("os.environ", {}, clear=True):
            target = resolve_submit_target("/project", "myapp", "o", "r")
            assert target == {"repo": "o/r", "is_fork": False}


# ---------------------------------------------------------------------------
# _generate_pr_summary
# ---------------------------------------------------------------------------

class TestGeneratePRSummary:
    def test_happy_path(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command",
                    return_value="A great summary"):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add X", "fix: broken Y"],
                skill_dir=Path("/skill"),
            )
            assert result == "A great summary"

    def test_fallback_on_model_failure(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command",
                    side_effect=RuntimeError("model unavailable")):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add X"],
                skill_dir=Path("/skill"),
            )
            assert "http://issue/1" in result
            assert "feat: add X" in result

    def test_fallback_on_empty_output(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value=""):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add X"],
                skill_dir=Path("/skill"),
            )
            assert "http://issue/1" in result

    def test_no_skill_dir_uses_load_prompt(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt") as mock_load, \
             patch("app.cli_provider.run_command", return_value="summary"):
            _generate_pr_summary(
                "/project", "Title", "http://issue/1", ["c1"],
            )
            mock_load.assert_called_once()
            assert mock_load.call_args[0][0] is None
            assert mock_load.call_args[0][1] == "pr_summary"

    def test_empty_commits(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value="summary"):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1", [],
                skill_dir=Path("/skill"),
            )
            assert result == "summary"

    def test_fallback_when_max_turns_noise_stripped(self):
        """When run_command returns empty after strip_cli_noise, fallback is used."""
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value=""):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add dashboard"],
                skill_dir=Path("/skill"),
            )
            assert "http://issue/1" in result
            assert "feat: add dashboard" in result


# ---------------------------------------------------------------------------
# submit_draft_pr (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestSubmitDraftPR:
    def test_skips_on_main_branch(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="main"):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None

    def test_returns_existing_pr_url(self):
        existing = json.dumps({"url": "https://github.com/o/r/pull/99", "body": "## Summary\nBody", "number": 99})
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", return_value=existing):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result == "https://github.com/o/r/pull/99"

    def test_skips_when_no_commits(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", return_value=""), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=[]):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None

    def test_returns_none_on_push_failure(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", return_value=""), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_PR_MODULE}.run_git_strict", side_effect=RuntimeError("push failed")):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None

    def test_happy_path_creates_pr(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/impl-42"), \
             patch(f"{_PR_MODULE}.run_gh", side_effect=["", ""]), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["feat: add X"]), \
             patch(f"{_PR_MODULE}.run_git_strict"), \
             patch(f"{_PR_MODULE}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_PR_MODULE}.pr_create",
                    return_value="https://github.com/o/r/pull/100") as mock_pr:
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="Implement: The Title",
                pr_body="## Summary\n\nBody",
                issue_url="http://issue/42",
            )
            assert result == "https://github.com/o/r/pull/100"
            mock_pr.assert_called_once()
            call_kwargs = mock_pr.call_args[1]
            assert call_kwargs["draft"] is True
            assert "The Title" in call_kwargs["title"]

    def test_fork_workflow_uses_repo_and_head(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/impl-42"), \
             patch(f"{_PR_MODULE}.run_gh", side_effect=["", ""]), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_PR_MODULE}.run_git_strict"), \
             patch(f"{_PR_MODULE}.resolve_submit_target",
                    return_value={"repo": "upstream/repo", "is_fork": True}), \
             patch(f"{_PR_MODULE}.get_fork_owner", return_value="myfork"), \
             patch(f"{_PR_MODULE}.pr_create",
                    return_value="https://github.com/upstream/repo/pull/5") as mock_pr:
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result == "https://github.com/upstream/repo/pull/5"
            call_kwargs = mock_pr.call_args[1]
            assert call_kwargs["repo"] == "upstream/repo"
            assert call_kwargs["head"] == "myfork:koan/impl-42"

    def test_returns_none_on_pr_create_failure(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", side_effect=["", RuntimeError("fail")]), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_PR_MODULE}.run_git_strict"), \
             patch(f"{_PR_MODULE}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_PR_MODULE}.pr_create", side_effect=RuntimeError("auth fail")):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None


# ---------------------------------------------------------------------------
# detect_parent_repo (in github.py)
# ---------------------------------------------------------------------------

class TestDetectParentRepo:
    def test_fork_detected(self):
        with patch("app.github.run_gh", return_value="upstream-owner/repo-name"):
            result = detect_parent_repo("/project")
            assert result == "upstream-owner/repo-name"

    def test_not_a_fork(self):
        with patch("app.github.run_gh", return_value=""):
            assert detect_parent_repo("/project") is None

    def test_null_parent(self):
        with patch("app.github.run_gh", return_value="null/null"):
            assert detect_parent_repo("/project") is None

    def test_gh_error(self):
        with patch("app.github.run_gh", side_effect=RuntimeError("gh failed")):
            assert detect_parent_repo("/project") is None

    def test_slash_only(self):
        with patch("app.github.run_gh", return_value="/"):
            assert detect_parent_repo("/project") is None


# ---------------------------------------------------------------------------
# get_project_submit_to_repository (in projects_config.py)
# ---------------------------------------------------------------------------

class TestGetProjectSubmitToRepository:
    def test_empty_config(self):
        config = {"defaults": {}, "projects": {"app": {"path": "/app"}}}
        assert get_project_submit_to_repository(config, "app") == {}

    def test_defaults_only(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "up/stream", "remote": "upstream"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "up/stream", "remote": "upstream"}

    def test_project_override(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "default/repo"}},
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {"repo": "custom/repo", "remote": "origin"},
                }
            },
        }
        result = get_project_submit_to_repository(config, "app")
        assert result["repo"] == "custom/repo"
        assert result["remote"] == "origin"

    def test_non_dict_value(self):
        config = {
            "defaults": {"submit_to_repository": "invalid"},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_submit_to_repository(config, "app") == {}

    def test_partial_config(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "up/stream"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "up/stream"}
        assert "remote" not in result


# ---------------------------------------------------------------------------
# Progress output
# ---------------------------------------------------------------------------

class TestProgressOutput:
    """Verify that run_implement prints timestamped progress lines to stdout."""

    def test_progress_lines_emitted(self, capsys):
        """Pipeline stages should print HH:MM — <message> lines."""
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    return_value="https://github.com/o/r/pull/10"), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"):
            run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
        captured = capsys.readouterr().out
        lines = captured.strip().splitlines()
        progress_lines = [l for l in lines if " — " in l]
        assert len(progress_lines) >= 3, f"Expected ≥3 progress lines, got: {progress_lines}"
        import re
        for line in progress_lines:
            assert re.match(r"\d{2}:\d{2} — ", line), f"Bad format: {line}"


# ---------------------------------------------------------------------------
# run_implement — updated integration tests
# ---------------------------------------------------------------------------

class TestRunImplementWithPR:
    """Tests verifying PR submission is called after successful implementation."""

    def test_pr_url_in_summary_on_success(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    return_value="https://github.com/o/r/pull/99"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "https://github.com/o/r/pull/99" in msg

    def test_private_gate_runs_after_pr_creation(self, tmp_path):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        gate_result = SimpleNamespace(
            ran=True,
            summary="Private review gate passed after 1 fix round(s)",
        )
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    return_value="https://github.com/o/r/pull/99"), \
             patch("app.private_review_gate.run_private_review_gate",
                   return_value=gate_result) as mock_gate:
            ok, msg = run_implement(
                str(tmp_path),
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
                project_name="app",
            )

            assert ok
            assert "Private gate: Private review gate passed" in msg
            mock_gate.assert_called_once()
            assert mock_gate.call_args.kwargs["pr_url"] == "https://github.com/o/r/pull/99"
            assert mock_gate.call_args.kwargs["plan_url"] == "https://github.com/o/r/issues/42"
            assert mock_gate.call_args.kwargs["skill_origin"] == "implement"

    def test_private_gate_failure_does_not_fail_implementation(self, tmp_path):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        gate_result = SimpleNamespace(
            ran=True,
            summary="Private review gate could not complete: review failed",
            clean=False,
        )
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    return_value="https://github.com/o/r/pull/99"), \
             patch("app.private_review_gate.run_private_review_gate",
                   return_value=gate_result):
            ok, msg = run_implement(
                str(tmp_path),
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
                project_name="app",
            )

            assert ok
            assert "Draft PR: https://github.com/o/r/pull/99" in msg
            assert "Private review gate could not complete" in msg

    def test_branch_in_summary_when_pr_fails(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/impl-42"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "koan/impl-42" in msg

    def test_warning_when_on_main_with_commits(self):
        """When commits land on the base branch, trigger retry then soft failure."""
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects", return_value=[]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="main"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            # No work on a feature branch → retry → still none → soft failure
            assert not ok
            all_notified = " ".join(c.args[0] for c in notify.call_args_list)
            assert "BLOCKED" not in all_notified

    def test_pr_submission_exception_does_not_fail_mission(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=body)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: implement plan"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    side_effect=RuntimeError("unexpected")):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok


# ---------------------------------------------------------------------------
# main — CLI entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_success_exit_code(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")):
            code = main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
            ])
            assert code == 0

    def test_failure_exit_code(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(False, "failed")):
            code = main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
            ])
            assert code == 1

    def test_context_arg_passed(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--context", "Phase 1 to 3",
            ])
            _, kwargs = mock.call_args
            assert kwargs["context"] == "Phase 1 to 3"

    def test_context_defaults_to_none(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
            ])
            _, kwargs = mock.call_args
            assert kwargs["context"] is None

    def test_project_identity_args_passed(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--project-name", "webpros-shield",
                "--instance-dir", "/koan/instance",
            ])
            _, kwargs = mock.call_args
            assert kwargs["project_name"] == "webpros-shield"
            assert kwargs["instance_dir"] == "/koan/instance"

    def test_base_branch_arg_passed(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--base-branch", "main",
            ])
            _, kwargs = mock.call_args
            assert kwargs["base_branch"] == "main"


# ---------------------------------------------------------------------------
# Escalated retry — work_landed detection
# ---------------------------------------------------------------------------

class TestEscalatedRetry:
    """Tests for the mechanical fallback retry pass when no work lands."""

    _BODY = "### Summary\nPlan\n#### Phase 1: Do it"

    def _base_patches(self, notify):
        """Common patches for run_implement that resolve issue + gate cleanly."""
        return [
            patch(f"{_IMPL_MODULE}.fetch_issue",
                  return_value=_github_issue(title="Title", body=self._BODY)),
            patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None),
            patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None),
        ]

    def test_escalated_retry_invoked_when_first_pass_leaves_no_commits(self):
        """First pass → no commits; second pass → commits. Retry runs exactly once."""
        notify = MagicMock()
        exec_mock = MagicMock(return_value="Done")
        # _work_landed() is called twice (once per pass).
        # Each call reads get_current_branch then get_commit_subjects.
        # Pass 1: branch=main, commits=[] → False (feature branch fallback also
        #   runs but git_utils.get_commit_subjects returns [] → no match)
        # Pass 2 (after retry): branch=koan/implement-42, commits=["feat"] → True
        # Post-retry checkout check (line ~258) = 3rd call.
        # Final get_current_branch call for the summary notification = 4th call.
        branch_side_effect = ["main", "koan/implement-42", "koan/implement-42", "koan/implement-42"]
        commit_subjects_side_effect = [[], ["feat: add X"]]

        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=self._BODY)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", exec_mock), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    side_effect=commit_subjects_side_effect), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    side_effect=branch_side_effect), \
             patch("app.config.get_branch_prefix", return_value="koan/"), \
             patch("app.git_utils.run_git", return_value=(1, "", "")), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )

        assert ok
        assert exec_mock.call_count == 2
        # Second call must have escalate=True
        second_call_kwargs = exec_mock.call_args_list[1][1]
        assert second_call_kwargs.get("escalate") is True

    def test_no_retry_when_first_pass_lands_commits(self):
        """First pass → commits on feature branch → no retry pass."""
        notify = MagicMock()
        exec_mock = MagicMock(return_value="Done")

        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=self._BODY)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", exec_mock), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects",
                    return_value=["feat: add X"]), \
             patch(f"{_IMPL_MODULE}.get_current_branch",
                    return_value="koan/implement-42"), \
             patch("app.config.get_branch_prefix", return_value="koan/"), \
             patch("app.git_utils.run_git", return_value=(1, "", "")), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None):
            ok, _ = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )

        assert ok
        assert exec_mock.call_count == 1

    def test_soft_notification_when_both_passes_fail(self):
        """Both passes produce no committed changes → soft notification, no blocker words."""
        notify = MagicMock()

        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=self._BODY)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects", return_value=[]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="main"), \
             patch("app.config.get_branch_prefix", return_value="koan/"), \
             patch("app.git_utils.run_git", return_value=(1, "", "")), \
             patch("app.git_utils.get_commit_subjects", return_value=[]):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )

        assert not ok
        # Must not raise — function returns cleanly
        all_notified = " ".join(c.args[0] for c in notify.call_args_list)
        # Soft wording — none of the _RESULT_ALERT_REGEX trigger phrases
        assert "BLOCKED" not in all_notified
        assert "could not execute" not in all_notified.lower()
        assert "no code changes" not in all_notified.lower()
        # Should mention the issue label
        assert "#42" in all_notified

    def test_no_retry_when_feature_branch_has_commits_but_head_is_main(self):
        """Claude checked out main after pushing — feature branch detected,
        no retry, and runner checks out the feature branch for PR submission."""
        notify = MagicMock()
        exec_mock = MagicMock(return_value="Done")
        submit_mock = MagicMock(return_value="https://github.com/o/r/pull/99")

        # run_git is called for:
        #   1. pre-run rev-parse (branch absent → rc=1)
        #   2. post-run rev-parse inside _work_landed (branch now exists → new tip)
        #   3. checkout of the detected branch
        def run_git_side_effect(*args, cwd=None, **kwargs):
            if args[:2] == ("rev-parse", "--verify"):
                if not hasattr(run_git_side_effect, "_revparse_call"):
                    run_git_side_effect._revparse_call = 0
                run_git_side_effect._revparse_call += 1
                if run_git_side_effect._revparse_call == 1:
                    return (1, "", "unknown revision")
                return (0, "abc123", "")
            if args[0] == "checkout":
                return (0, "", "")
            return (0, "", "")

        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=self._BODY)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", exec_mock), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects", return_value=[]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="main"), \
             patch("app.config.get_branch_prefix", return_value="koan/"), \
             patch("app.git_utils.get_commit_subjects",
                    return_value=["feat: add X"]), \
             patch("app.git_utils.run_git", side_effect=run_git_side_effect), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", submit_mock):
            ok, _ = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )

        assert ok
        assert exec_mock.call_count == 1

    def test_checkout_failure_skips_pr_submission(self):
        """When checkout of detected feature branch fails, return success
        but skip PR submission to avoid submitting from main."""
        notify = MagicMock()
        exec_mock = MagicMock(return_value="Done")
        submit_mock = MagicMock(return_value="https://github.com/o/r/pull/99")

        # run_git calls:
        #   1. pre-run rev-parse → branch absent (rc=1)
        #   2. post-run rev-parse → branch exists with new tip
        #   3. checkout → fails
        def run_git_side_effect(*args, cwd=None, **kwargs):
            if args[:2] == ("rev-parse", "--verify"):
                if not hasattr(run_git_side_effect, "_revparse_call"):
                    run_git_side_effect._revparse_call = 0
                run_git_side_effect._revparse_call += 1
                if run_git_side_effect._revparse_call == 1:
                    return (1, "", "unknown revision")
                return (0, "abc123", "")
            if args[0] == "checkout":
                return (1, "", "error: pathspec not found")
            return (0, "", "")

        with patch(f"{_IMPL_MODULE}.fetch_issue",
                    return_value=_github_issue(title="Title", body=self._BODY)), \
             patch(f"{_IMPL_MODULE}._run_plan_review_gate", return_value=None), \
             patch(f"{_IMPL_MODULE}._execute_implementation", exec_mock), \
             patch(f"{_IMPL_MODULE}.get_commit_subjects", return_value=[]), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="main"), \
             patch("app.config.get_branch_prefix", return_value="koan/"), \
             patch("app.git_utils.get_commit_subjects",
                    return_value=["feat: add X"]), \
             patch("app.git_utils.run_git", side_effect=run_git_side_effect), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", submit_mock):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )

        assert ok
        assert "could not switch" in msg.lower()
        submit_mock.assert_not_called()
        all_notified = " ".join(c.args[0] for c in notify.call_args_list)
        assert "skipping PR submission" in all_notified

    def test_escalated_retry_injects_escalation_preamble_into_context(self):
        """_execute_implementation with escalate=True prepends the retry-context fragment."""
        escalation_text = "## Escalated Retry — Committed Changes Required"
        base_context = "Implement the full plan."

        with patch(f"{_IMPL_MODULE}._build_prompt", return_value="prompt") as mock_build, \
             patch("app.cli_provider.run_command_streaming", return_value="output"), \
             patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value=escalation_text) as mock_load:
            _execute_implementation(
                "/project", "http://url", "Title", "Plan", base_context,
                escalate=True,
            )

        # load_prompt_or_skill must be called for the escalation fragment
        escalation_calls = [
            c for c in mock_load.call_args_list
            if len(c.args) >= 2 and c.args[1] == "implement_retry_context"
        ]
        assert len(escalation_calls) == 1

        # The context passed to _build_prompt must include the escalation preamble
        build_context_arg = mock_build.call_args.kwargs.get("context") or mock_build.call_args[0][3]
        assert escalation_text in build_context_arg
        assert base_context in build_context_arg
