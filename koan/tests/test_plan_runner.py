"""Tests for plan_runner.py — the plan execution pipeline."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.plan_runner import (
    run_plan,
    _generate_plan,
    _generate_iteration_plan,
    _run_claude_plan,
    _is_error_output,
    _strip_preamble,
    _format_comments,
    _extract_title,
    _extract_idea_from_issue,
    _strip_title_line,
    _run_new_plan,
    _run_issue_plan,
    _PLAN_LABEL,
    main,
    review_plan,
    review_plan_assumptions,
    _apply_assumptions_audit,
    _merge_assumptions_into_open_questions,
    ASSUMPTIONS_OK,
    ASSUMPTIONS_CRITICAL,
    ASSUMPTIONS_REVIEWER_ERROR,
    _review_loop,
    _critic_loop,
    is_simple_plan,
)
from app.url_skill_args import merge_context_with_base_branch
from app.issue_tracker.types import IssueContent, IssueRef
from app.issue_tracker import UnresolvedJiraProjectError

pytestmark = pytest.mark.slow


def _issue_ref(provider="github", url="https://github.com/o/r/issues/64",
               key="64", repo="o/r"):
    return IssueRef(provider=provider, url=url, key=key, repo=repo)


def _issue_content(title="Issue Title", body="body", comments=None,
                   provider="github", key="64"):
    ref = _issue_ref(provider=provider, key=key)
    return IssueContent(
        ref=ref, title=title, body=body, comments=comments or [], state="open",
    )


# ---------------------------------------------------------------------------
# run_plan — top-level routing
# ---------------------------------------------------------------------------

class TestRunPlan:
    def test_no_idea_no_url_returns_error(self):
        ok, msg = run_plan("/project")
        assert not ok
        assert "No idea" in msg

    def test_routes_to_new_plan(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "done")) as mock:
            ok, msg = run_plan("/project", idea="Add feature", notify_fn=MagicMock())
            assert ok
            mock.assert_called_once()

    def test_routes_to_issue_plan(self):
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._run_issue_plan", return_value=(True, "done")) as mock:
            ok, msg = run_plan("/project", issue_url=url, notify_fn=MagicMock())
            assert ok
            mock.assert_called_once()

    def test_passes_context_to_new_plan(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "ok")) as mock:
            run_plan("/project", idea="Add X", notify_fn=MagicMock(), context="Phase 2")
            _, kwargs = mock.call_args
            assert kwargs.get("context") == "Phase 2"

    def test_passes_context_to_issue_plan(self):
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner._run_issue_plan", return_value=(True, "ok")) as mock:
            run_plan("/project", issue_url=url, notify_fn=MagicMock(), context="Focus on API")
            _, kwargs = mock.call_args
            assert kwargs.get("context") == "Focus on API"

    def test_context_defaults_to_none(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "ok")) as mock:
            run_plan("/project", idea="Add X", notify_fn=MagicMock())
            _, kwargs = mock.call_args
            assert kwargs.get("context") is None

    def test_defaults_notify_fn(self):
        with patch("app.plan_runner._run_new_plan", return_value=(True, "ok")) as mock, \
             patch("app.notify.send_telegram"):
            run_plan("/project", idea="test")
            # Should not crash — notify_fn defaults to send_telegram


# ---------------------------------------------------------------------------
# _run_new_plan
# ---------------------------------------------------------------------------

class TestRunNewPlan:
    def test_successful_plan_with_issue(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan\nStep 1"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_supports_labels", return_value=True), \
             patch("app.plan_runner.create_issue",
                   return_value="https://github.com/sukria/koan/issues/99"):
            ok, msg = _run_new_plan("/project", "Add feature", notify, None)
            assert ok
            assert "issues/99" in msg
            notify.assert_called()

    def test_no_github_repo_sends_inline(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan\nStep 1"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=False):
            ok, msg = _run_new_plan("/project", "Add feature", notify, None)
            assert ok
            assert "inline" in msg
            # Plan was sent via notify_fn
            calls = [str(c) for c in notify.call_args_list]
            assert any("Plan" in c for c in calls)

    def test_normal_mode_gates_banner_but_sends_outcome(self):
        """Under normal mode with the default sink, the 🧠 Planning banner is
        suppressed but the ✅ Plan created outcome is always sent."""
        sent = []
        with patch("app.messaging_level.is_debug", return_value=False), \
             patch("app.notify.send_telegram", lambda m, **k: sent.append(m)), \
             patch("app.plan_runner._generate_plan", return_value="## Plan\nStep 1"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_supports_labels", return_value=True), \
             patch("app.plan_runner.create_issue",
                   return_value="https://github.com/o/r/issues/42"):
            ok, _msg = run_plan("/project", idea="Add feature", notify_fn=None)
        assert ok
        assert not any("Planning" in m for m in sent)  # banner gated
        assert any("https://github.com/o/r/issues/42" in m for m in sent)  # outcome sent

    def test_generate_plan_failure(self):
        notify = MagicMock()
        with patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner._generate_plan", side_effect=RuntimeError("timeout")):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert not ok
            assert "failed" in msg.lower()

    def test_empty_plan(self):
        notify = MagicMock()
        with patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner._generate_plan", return_value=""):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert not ok
            assert "empty" in msg.lower()

    def test_context_passed_to_generate_plan(self):
        """User context should be forwarded to _generate_plan."""
        notify = MagicMock()
        with patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=False), \
             patch("app.plan_runner._generate_plan", return_value="## Plan") as mock_gen:
            _run_new_plan("/project", "Add X", notify, None, context="Phase 2 only")
            _, kwargs = mock_gen.call_args
            assert kwargs.get("context") == "Phase 2 only"

    def test_no_context_passes_empty_string(self):
        """Without context, _generate_plan should receive empty string."""
        notify = MagicMock()
        with patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=False), \
             patch("app.plan_runner._generate_plan", return_value="## Plan") as mock_gen:
            _run_new_plan("/project", "Add X", notify, None)
            _, kwargs = mock_gen.call_args
            assert kwargs.get("context") == ""

    def test_issue_creation_failure_with_label_retries_without(self):
        notify = MagicMock()
        # First (labelled) create fails; retry without labels succeeds.
        create = MagicMock(side_effect=[
            RuntimeError("label not found"),
            "https://github.com/o/r/issues/5",
        ])
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_supports_labels", return_value=True), \
             patch("app.plan_runner.create_issue", create):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert ok
            assert "issues/5" in msg

    def test_issue_creation_total_failure(self):
        notify = MagicMock()
        create = MagicMock(side_effect=RuntimeError("no perms"))
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_supports_labels", return_value=True), \
             patch("app.plan_runner.create_issue", create):
            ok, msg = _run_new_plan("/project", "idea", notify, None)
            assert ok
            assert "failed" in msg.lower()

    def test_sends_planning_notification(self):
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=False):
            _run_new_plan("/project", "Add dark mode to dashboard", notify, None)
            first_msg = notify.call_args_list[0][0][0]
            assert "Planning" in first_msg
            assert "dark mode" in first_msg

    def test_long_idea_truncated_in_notification(self):
        notify = MagicMock()
        long_idea = "A" * 200
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=False):
            _run_new_plan("/project", long_idea, notify, None)
            first_msg = notify.call_args_list[0][0][0]
            assert "..." in first_msg

    def test_reuses_existing_issue_when_found(self):
        """When an existing issue matches, delegate to _run_issue_plan."""
        notify = MagicMock()
        existing = _issue_ref(key="42", url="https://github.com/o/r/issues/42")
        with patch("app.plan_runner.find_existing_plan_issue", return_value=existing), \
             patch("app.plan_runner._run_issue_plan",
                    return_value=(True, "Plan posted on #42")) as mock_issue:
            ok, msg = _run_new_plan("/project", "dark mode feature", notify, None)
            assert ok
            assert "#42" in msg
            mock_issue.assert_called_once()
            # Verify the URL passed to _run_issue_plan
            url_arg = mock_issue.call_args[0][1]
            assert "issues/42" in url_arg

    def test_existing_issue_notification(self):
        """When reusing an issue, notify the user about the redirect."""
        notify = MagicMock()
        existing = _issue_ref(key="7", url="https://github.com/o/r/issues/7")
        with patch("app.plan_runner.find_existing_plan_issue", return_value=existing), \
             patch("app.plan_runner._run_issue_plan", return_value=(True, "ok")):
            _run_new_plan("/project", "similar idea", notify, None)
            # Should have notified about finding an existing issue
            msgs = [str(c) for c in notify.call_args_list]
            assert any("existing" in m.lower() or "Found" in m for m in msgs)

    def test_search_failure_creates_new_issue(self):
        """If no existing issue matches, proceed with new issue creation."""
        notify = MagicMock()
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_supports_labels", return_value=True), \
             patch("app.plan_runner.create_issue",
                   return_value="https://github.com/o/r/issues/10"):
            ok, msg = _run_new_plan("/project", "brand new idea", notify, None)
            assert ok
            assert "issues/10" in msg

    def test_creates_issue_with_plan_label(self):
        """New GitHub issues should be created with the 'plan' label."""
        notify = MagicMock()
        create = MagicMock(return_value="https://github.com/o/r/issues/1")
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_supports_labels", return_value=True), \
             patch("app.plan_runner.create_issue", create):
            _run_new_plan("/project", "test idea", notify, None)
            _, kwargs = create.call_args
            assert kwargs.get("labels") == [_PLAN_LABEL]

    def test_jira_tracker_omits_labels(self):
        """A label-less tracker (Jira) should not pass labels to create_issue."""
        notify = MagicMock()
        create = MagicMock(return_value="https://org.atlassian.net/browse/PROJ-1")
        with patch("app.plan_runner._generate_plan", return_value="## Plan"), \
             patch("app.plan_runner.find_existing_plan_issue", return_value=None), \
             patch("app.plan_runner.tracker_is_configured", return_value=True), \
             patch("app.plan_runner.tracker_provider", return_value="jira"), \
             patch("app.plan_runner.tracker_supports_labels", return_value=False), \
             patch("app.plan_runner.create_issue", create):
            ok, msg = _run_new_plan("/project", "test idea", notify, None)
            assert ok
            _, kwargs = create.call_args
            assert kwargs.get("labels") is None
            assert "_Generated by [Kōan]" in create.call_args.args[3]
            assert "## Plan" in create.call_args.args[3]


# ---------------------------------------------------------------------------
# _run_issue_plan
# ---------------------------------------------------------------------------

class TestRunIssuePlan:
    def _patch_tracker(self, content, ref=None):
        """Patch service helpers for issue-plan tests."""
        ref = ref or _issue_ref()
        add = MagicMock(return_value=True)
        return (
            patch("app.plan_runner.resolve_issue_ref", return_value=ref),
            patch("app.plan_runner.fetch_issue", return_value=content),
            patch("app.plan_runner.add_comment", add),
            add,
        )

    def test_successful_iteration(self):
        notify = MagicMock()
        url = "https://github.com/sukria/koan/issues/64"
        content = _issue_content(title="Issue Title", comments=[
            {"author": "alice", "date": "2026-01-01", "body": "comment"},
        ])
        p_ref, p_fetch, p_add, add = self._patch_tracker(content)
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Updated Plan"):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert ok
            assert "#64" in msg
            add.assert_called_once()

    def test_invalid_url(self):
        notify = MagicMock()
        with patch("app.plan_runner.resolve_issue_ref",
                    side_effect=ValueError("Invalid GitHub URL")):
            ok, msg = _run_issue_plan("/project", "not-a-url", notify, None)
        assert not ok
        assert "Invalid" in msg

    def test_fetch_failure(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner.resolve_issue_ref", return_value=_issue_ref()), \
             patch("app.plan_runner.fetch_issue", side_effect=RuntimeError("not found")):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "Failed to fetch" in msg

    def test_unmapped_jira_project_resolve_notifies_and_fails(self):
        notify = MagicMock()
        with patch(
            "app.plan_runner.resolve_issue_ref",
            side_effect=UnresolvedJiraProjectError(
                "Unmapped Jira issue 'PROJ-42': no Koan project was resolved. "
                "Add this mapping in projects.yaml under projects.<name>.issue_tracker "
                "with provider: jira and jira_project: PROJ.",
            ),
        ):
            ok, msg = _run_issue_plan(
                "/project",
                "https://org.atlassian.net/browse/PROJ-42",
                notify,
                None,
            )
        assert not ok
        assert "projects.yaml" in msg
        notify.assert_called_once()

    def test_plan_generation_failure(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        p_ref, p_fetch, p_add, _ = self._patch_tracker(_issue_content())
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan",
                    side_effect=RuntimeError("error")):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "failed" in msg.lower()

    def test_empty_plan(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        p_ref, p_fetch, p_add, _ = self._patch_tracker(_issue_content())
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan", return_value=""):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "empty" in msg.lower()

    def test_comment_failure_sends_inline(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner.resolve_issue_ref", return_value=_issue_ref()), \
             patch("app.plan_runner.fetch_issue", return_value=_issue_content()), \
             patch("app.plan_runner.add_comment", side_effect=RuntimeError("no perms")), \
             patch("app.plan_runner._generate_iteration_plan", return_value="## Plan"):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "failed" in msg.lower()

    def test_false_comment_result_is_a_failure_not_a_false_success(self):
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner.resolve_issue_ref", return_value=_issue_ref()), \
             patch("app.plan_runner.fetch_issue", return_value=_issue_content()), \
             patch("app.plan_runner.add_comment", return_value=False), \
             patch("app.plan_runner._generate_iteration_plan", return_value="## Plan"):
            ok, msg = _run_issue_plan("/project", url, notify, None)

        assert not ok
        assert "failed" in msg.lower()
        assert not any("Plan posted" in str(call) for call in notify.call_args_list)

    def test_sends_reading_notification(self):
        notify = MagicMock()
        url = "https://github.com/sukria/koan/issues/64"
        p_ref, p_fetch, p_add, _ = self._patch_tracker(_issue_content())
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan", return_value="## Plan"):
            _run_issue_plan("/project", url, notify, None)
            first_msg = notify.call_args_list[0][0][0]
            assert "#64" in first_msg

    def test_success_includes_title(self):
        notify = MagicMock()
        url = "https://github.com/sukria/koan/issues/64"
        p_ref, p_fetch, p_add, _ = self._patch_tracker(
            _issue_content(title="Add dark mode"),
        )
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan", return_value="## Plan"):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert ok
            assert "Add dark mode" in msg

    def test_uses_iteration_prompt(self):
        """Issue plan should use _generate_iteration_plan, not _generate_plan."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        content = _issue_content(title="Title", body="body text", comments=[
            {"author": "alice", "date": "2026-01-01", "body": "great idea"},
        ])
        p_ref, p_fetch, p_add, _ = self._patch_tracker(content)
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Updated Plan") as mock_iter:
            _run_issue_plan("/project", url, notify, None)
            mock_iter.assert_called_once()
            # Verify the issue context is passed
            context_arg = mock_iter.call_args[1].get("issue_context") or \
                          mock_iter.call_args[0][1]
            assert "Title" in context_arg
            assert "alice" in context_arg

    def test_no_comments_still_includes_context(self):
        """Even with no comments, the context should note that."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        p_ref, p_fetch, p_add, _ = self._patch_tracker(_issue_content(comments=[]))
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan") as mock_iter:
            _run_issue_plan("/project", url, notify, None)
            context_arg = mock_iter.call_args[0][1]
            assert "No comments" in context_arg

    def test_user_context_appended_to_issue_context(self):
        """User context should appear in the issue context passed to Claude."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        content = _issue_content(comments=[
            {"author": "bob", "date": "2026-01-01", "body": "comment"},
        ])
        p_ref, p_fetch, p_add, _ = self._patch_tracker(content)
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan") as mock_iter:
            _run_issue_plan("/project", url, notify, None, context="Focus on phase 2")
            context_arg = mock_iter.call_args[0][1]
            assert "User Instructions" in context_arg
            assert "Focus on phase 2" in context_arg

    def test_no_user_context_omits_instructions_section(self):
        """Without user context, no 'User Instructions' section should appear."""
        notify = MagicMock()
        url = "https://github.com/o/r/issues/1"
        p_ref, p_fetch, p_add, _ = self._patch_tracker(_issue_content())
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan",
                    return_value="## Plan") as mock_iter:
            _run_issue_plan("/project", url, notify, None)
            context_arg = mock_iter.call_args[0][1]
            assert "User Instructions" not in context_arg

    def test_jira_iteration_comment_is_human_readable(self):
        notify = MagicMock()
        url = "https://org.atlassian.net/browse/PROJ-9"
        ref = _issue_ref(provider="jira", url=url, key="PROJ-9", repo="o/r")
        content = _issue_content(provider="jira", key="PROJ-9")
        p_ref, p_fetch, p_add, _add = self._patch_tracker(content, ref=ref)
        staged = []
        with p_ref, p_fetch, p_add, \
             patch("app.plan_runner._generate_iteration_plan", return_value="## Updated Plan\n\n### Phase 1\n- Do X"), \
             patch("app.jira_plan_publish.load_staged_plan", return_value=None), \
             patch("app.jira_plan_publish.stage_plan", side_effect=lambda _url, body, _instance: staged.append(body)), \
             patch("app.jira_plan_publish.publish_staged_plan", return_value=(True, "123")):
            ok, _msg = _run_issue_plan("/project", url, notify, None)
            assert ok
            comment_text = staged[0]
            assert "## Updated Plan" in comment_text
            assert "### Phase 1" in comment_text

    def test_jira_iteration_failure_posts_status_comment(self):
        notify = MagicMock()
        url = "https://org.atlassian.net/browse/PROJ-9"
        ref = _issue_ref(provider="jira", url=url, key="PROJ-9", repo="o/r")
        add = MagicMock(return_value=True)
        with patch("app.plan_runner.resolve_issue_ref", return_value=ref), \
             patch("app.plan_runner.fetch_issue", return_value=_issue_content(provider="jira", key="PROJ-9")), \
             patch("app.plan_runner.add_comment", add), \
             patch("app.plan_runner._generate_iteration_plan", side_effect=RuntimeError("timeout")):
            ok, msg = _run_issue_plan("/project", url, notify, None)
            assert not ok
            assert "failed" in msg.lower()
            assert add.called
            assert "plan update failed" in add.call_args.args[1].lower()

    def test_jira_publish_failure_fails_without_false_success(self):
        notify = MagicMock()
        url = "https://org.atlassian.net/browse/PROJ-9"
        ref = _issue_ref(provider="jira", url=url, key="PROJ-9", repo="o/r")
        with patch("app.plan_runner.resolve_issue_ref", return_value=ref), \
             patch("app.plan_runner.fetch_issue", return_value=_issue_content(provider="jira", key="PROJ-9")), \
             patch("app.plan_runner._generate_iteration_plan", return_value="## Plan"), \
             patch("app.jira_plan_publish.load_staged_plan", return_value=None), \
             patch("app.jira_plan_publish.stage_plan") as stage, \
             patch("app.jira_plan_publish.publish_staged_plan", return_value=(False, "verification_failed")):
            ok, msg = _run_issue_plan("/project", url, notify, None)

        assert not ok
        assert "could not verify" in msg.lower()
        stage.assert_called_once()
        assert not any("✅ Plan posted" in str(call) for call in notify.call_args_list)

    def test_jira_resume_publishes_staged_plan_without_generation(self):
        notify = MagicMock()
        url = "https://org.atlassian.net/browse/PROJ-9"
        ref = _issue_ref(provider="jira", url=url, key="PROJ-9", repo="o/r")
        with patch("app.plan_runner.resolve_issue_ref", return_value=ref), \
             patch("app.plan_runner.fetch_issue") as fetch, \
             patch("app.plan_runner._generate_iteration_plan") as generate, \
             patch("app.jira_plan_publish.load_staged_plan", return_value="saved plan"), \
             patch("app.jira_plan_publish.publish_staged_plan", return_value=(True, "321")):
            ok, msg = _run_issue_plan("/project", url, notify, None)

        assert ok
        assert "Plan posted" in msg
        fetch.assert_not_called()
        generate.assert_not_called()


# ---------------------------------------------------------------------------
# _generate_plan
# ---------------------------------------------------------------------------

class TestGeneratePlan:
    @patch("app.cli_provider.run_command_streaming", return_value="## Plan\n\nStep 1")
    def test_returns_claude_output(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            skill_dir = Path("/fake/skills/core/plan")
            result = _generate_plan("/project", "Add feature", skill_dir=skill_dir)
            assert "Step 1" in result

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_includes_context(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill") as mock_load:
            skill_dir = Path("/fake")
            _generate_plan("/project", "idea", context="prev", skill_dir=skill_dir)
            _, kwargs = mock_load.call_args
            assert kwargs["CONTEXT"] == "prev"

    @patch("app.cli_provider.run_command_streaming",
           side_effect=RuntimeError("CLI invocation failed: rate limited"))
    def test_raises_on_failure(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            with pytest.raises(RuntimeError, match="invocation failed"):
                _generate_plan("/project", "idea", skill_dir=Path("/fake"))

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_uses_read_only_tools(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            _generate_plan("/project", "idea", skill_dir=Path("/fake"))
            call_kwargs = mock_run.call_args[1]
            assert "Read" in call_kwargs.get("allowed_tools", [])

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_no_skill_dir_uses_load_prompt(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt") as mock_load:
            _generate_plan("/project", "idea")
            mock_load.assert_called_once()


# ---------------------------------------------------------------------------
# _generate_iteration_plan
# ---------------------------------------------------------------------------

class TestGenerateIterationPlan:
    @patch("app.cli_provider.run_command_streaming", return_value="## Updated Plan")
    def test_uses_plan_iterate_prompt(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill") as mock_load:
            skill_dir = Path("/fake/skills/core/plan")
            result = _generate_iteration_plan(
                "/project", "issue context here", skill_dir=skill_dir
            )
            assert "Updated Plan" in result
            # Verify it loads plan-iterate, not plan
            mock_load.assert_called_once_with(
                skill_dir, "plan-iterate",
                project_path="/project",
                ISSUE_CONTEXT="issue context here",
                PROJECT_MEMORY="",
            )

    @patch("app.cli_provider.run_command_streaming", return_value="plan")
    def test_no_skill_dir_uses_load_prompt(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill") as mock_load:
            _generate_iteration_plan("/project", "context")
            mock_load.assert_called_once_with(
                None, "plan-iterate",
                project_path="/project",
                ISSUE_CONTEXT="context",
                PROJECT_MEMORY="",
            )

    @patch("app.cli_provider.run_command_streaming",
           side_effect=RuntimeError("CLI invocation failed: error"))
    def test_raises_on_failure(self, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"):
            with pytest.raises(RuntimeError):
                _generate_iteration_plan(
                    "/project", "context", skill_dir=Path("/fake")
                )


# ---------------------------------------------------------------------------
# _run_claude_plan — shared Claude invocation
# ---------------------------------------------------------------------------

class TestRunClaudePlan:
    @patch("app.config.get_skill_max_turns", return_value=50)
    @patch("app.config.get_skill_timeout", return_value=3600)
    @patch("app.config.mcp_configs_for_role", return_value=None)
    @patch("app.cli_provider.run_command_streaming", return_value="result with spaces")
    def test_returns_stripped_output(self, mock_cmd, mock_mcp, mock_timeout, mock_turns):
        result = _run_claude_plan("test prompt", "/project")
        assert result == "result with spaces"
        mock_cmd.assert_called_once_with(
            "test prompt", "/project",
            allowed_tools=["Read", "Glob", "Grep", "WebFetch"],
            model_key="mission",
            max_turns=50, timeout=3600,
            project_name="",
            mcp_configs=None,
        )
        mock_mcp.assert_called_once_with("plan", "")

    @patch("app.config.get_skill_max_turns", return_value=10)
    @patch("app.config.get_skill_timeout", return_value=300)
    @patch("app.cli_provider.run_command_streaming", return_value="plan output")
    def test_uses_mission_model_key(self, mock_cmd, mock_timeout, mock_turns):
        """Regression test for issue #1614: plan should use mission model, not haiku."""
        _run_claude_plan("plan prompt", "/project")
        # Verify that model_key="mission" is passed, not default "chat" (haiku)
        call_kwargs = mock_cmd.call_args[1]
        assert call_kwargs["model_key"] == "mission"

    @patch("app.cli_provider.run_command_streaming",
           side_effect=RuntimeError("CLI invocation failed: error msg"))
    def test_raises_on_non_zero_exit(self, mock_cmd):
        with pytest.raises(RuntimeError, match="CLI invocation failed"):
            _run_claude_plan("prompt", "/project")

    @patch("app.cli_provider.run_command_streaming",
           return_value="Error: Reached max turns (3)")
    def test_raises_on_max_turns_error(self, mock_cmd):
        with pytest.raises(RuntimeError, match="Reached max turns"):
            _run_claude_plan("prompt", "/project")

    @patch("app.cli_provider.run_command_streaming",
           return_value="Error: Something went wrong")
    def test_raises_on_short_error_output(self, mock_cmd):
        with pytest.raises(RuntimeError, match="Something went wrong"):
            _run_claude_plan("prompt", "/project")

    @patch("app.cli_provider.run_command_streaming",
           return_value=(
               "● Read files\nExcellent! Now I have all the context I need.\n"
               "\nClean title\n\n### Summary"
           ))
    def test_strips_preamble_from_output(self, mock_cmd):
        result = _run_claude_plan("prompt", "/project")
        assert result.startswith("Clean title")
        assert "● Read" not in result


# ---------------------------------------------------------------------------
# _is_error_output
# ---------------------------------------------------------------------------

class TestIsErrorOutput:
    def test_empty_string(self):
        assert _is_error_output("") is False

    def test_none(self):
        assert _is_error_output(None) is False

    def test_valid_plan_output(self):
        assert _is_error_output("### Summary\n\nThis plan does X.") is False

    def test_max_turns_error(self):
        assert _is_error_output("Error: Reached max turns (3)") is True

    def test_max_turns_error_with_prefix(self):
        assert _is_error_output("Some text\nReached max turns (25)\nmore") is True

    def test_short_error_message(self):
        assert _is_error_output("Error: Connection refused") is True

    def test_whitespace_prefixed_error(self):
        assert _is_error_output("  Error: Reached max turns (3)") is True

    def test_long_error_not_flagged(self):
        # A long "Error:" string is likely plan content mentioning errors
        long_text = "Error: " + "x" * 300
        assert _is_error_output(long_text) is False

    def test_error_in_plan_content_not_flagged(self):
        # An error word in normal plan content should not trigger
        assert _is_error_output(
            "### Error Handling\n\nWe should handle errors gracefully."
        ) is False


# ---------------------------------------------------------------------------
# _strip_preamble
# ---------------------------------------------------------------------------

class TestStripPreamble:
    def test_strips_now_i_have_context(self):
        output = (
            "I searched the codebase for relevant files.\n"
            "Excellent! Now I have all the context I need. "
            "Let me create the comprehensive plan:\n"
            "\n"
            "Add dark mode support\n"
            "\n"
            "### Summary\n"
            "\nThis plan adds dark mode."
        )
        result = _strip_preamble(output)
        assert result.startswith("Add dark mode support")
        assert "I searched" not in result
        assert "Excellent" not in result

    def test_strips_let_me_create_plan(self):
        output = (
            "Reading files...\n"
            "Let me create the structured plan:\n"
            "\n"
            "Fix auth module\n"
            "\n"
            "### Summary"
        )
        result = _strip_preamble(output)
        assert result.startswith("Fix auth module")

    def test_strips_heres_the_plan(self):
        output = (
            "Some exploration output\n"
            "Here's the comprehensive plan:\n"
            "\n"
            "Improve logging\n"
            "\n"
            "### Summary"
        )
        result = _strip_preamble(output)
        assert result.startswith("Improve logging")

    def test_strips_here_is_the_plan(self):
        output = "Here is the implementation plan:\n\nTitle\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Title")

    def test_no_preamble_returns_unchanged(self):
        output = "Add dark mode\n\n### Summary\n\nDetails"
        assert _strip_preamble(output) == output

    def test_empty_string(self):
        assert _strip_preamble("") == ""

    def test_none_returns_none(self):
        assert _strip_preamble(None) is None

    def test_multiple_preamble_lines_uses_last(self):
        output = (
            "Let me create the plan:\n"
            "Actually, let me generate the plan with more detail:\n"
            "\n"
            "Real title\n"
            "### Summary"
        )
        result = _strip_preamble(output)
        assert result.startswith("Real title")

    def test_preamble_only_returns_original(self):
        """If stripping leaves nothing, return original."""
        output = "Now I have all the context I need."
        result = _strip_preamble(output)
        assert result == output

    def test_long_copilot_preamble(self):
        """Simulate Copilot tool-use output followed by plan."""
        lines = [
            "● Read README.md",
            "  Contents of README...",
            "● Glob **/*.py",
            "  Found 42 files",
            "● Read src/main.py",
            "  def main():",
            "    pass",
            "",
            "Excellent! Now I have the context I need. "
            "Let me create the comprehensive plan:",
            "",
            "Add comprehensive test suite",
            "",
            "### Summary",
            "",
            "This plan adds tests.",
        ]
        output = "\n".join(lines)
        result = _strip_preamble(output)
        assert result.startswith("Add comprehensive test suite")
        assert "● Read" not in result

    def test_case_insensitive(self):
        output = "HERE IS THE PLAN:\n\nTitle\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Title")

    def test_ill_create_plan(self):
        output = "I'll create the plan now.\n\nTitle here\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Title here")

    def test_let_me_draft_the_plan(self):
        output = "Let me draft the plan:\n\nDraft title\n\n### Summary"
        result = _strip_preamble(output)
        assert result.startswith("Draft title")


# ---------------------------------------------------------------------------
# _format_comments
# ---------------------------------------------------------------------------

class TestFormatComments:
    def test_formats_with_author_and_date(self):
        data = [
            {"author": "alice", "date": "2026-02-01T10:00:00Z", "body": "Good"},
        ]
        result = _format_comments(data)
        assert "alice" in result
        assert "2026-02-01" in result

    def test_empty_list(self):
        assert _format_comments([]) == ""

    def test_none_input(self):
        assert _format_comments(None) == ""

    def test_non_list_input(self):
        assert _format_comments("not a list") == ""

    def test_skips_empty_body(self):
        data = [
            {"author": "a", "date": "2026-01-01T00:00:00Z", "body": ""},
            {"author": "b", "date": "2026-01-02T00:00:00Z", "body": "useful"},
        ]
        result = _format_comments(data)
        assert "useful" in result
        assert result.count("**") == 2


# ---------------------------------------------------------------------------
# _extract_title
# ---------------------------------------------------------------------------

class TestExtractTitle:
    def test_from_heading(self):
        assert _extract_title("## Dark mode\n\nDetails") == "Dark mode"

    def test_first_non_empty_line(self):
        assert _extract_title("\n\nThis is the plan") == "This is the plan"

    def test_truncates(self):
        assert len(_extract_title("# " + "A" * 200)) <= 120

    def test_fallback(self):
        assert _extract_title("") == "Implementation Plan"

    def test_skips_generic_headings(self):
        """Generic section headings like 'Summary' are skipped."""
        assert _extract_title("### Summary\nReal plan title") == "Real plan title"
        assert _extract_title("### Summary") == "Implementation Plan"

    def test_first_line_title(self):
        """Title as plain first line (new prompt format)."""
        plan = "Add dark mode with theme persistence\n\n### Summary\n\nDetails"
        assert _extract_title(plan) == "Add dark mode with theme persistence"

    def test_strips_bullet_prefix(self):
        """Copilot-style ● prefix is stripped from title."""
        assert _extract_title("● GitHub notifications\n\n### Summary") == "GitHub notifications"

    def test_strips_arrow_prefix(self):
        assert _extract_title("→ Fix auth module\n\nDetails") == "Fix auth module"
        assert _extract_title("► Improve performance\n\nDetails") == "Improve performance"

    def test_strips_multiple_noise_chars(self):
        assert _extract_title(">> Some title\n\nBody") == "Some title"
        assert _extract_title("●● Double bullet\n\nBody") == "Double bullet"

    def test_noise_char_with_heading(self):
        assert _extract_title("# ● Noisy heading\n\nBody") == "Noisy heading"


# ---------------------------------------------------------------------------
# _strip_title_line
# ---------------------------------------------------------------------------

class TestStripTitleLine:
    def test_removes_first_line(self):
        text = "My title\n\n### Summary\n\nDetails here"
        result = _strip_title_line(text)
        assert "My title" not in result
        assert "### Summary" in result
        assert "Details here" in result

    def test_preserves_body(self):
        text = "Title\n\n### Summary\n\nA paragraph.\n\n### Phases\n\nPhase 1"
        result = _strip_title_line(text)
        assert result.startswith("### Summary")

    def test_empty_string(self):
        assert _strip_title_line("") == ""

    def test_title_only(self):
        assert _strip_title_line("Just a title") == "Just a title"

    def test_skips_leading_blank_lines(self):
        text = "\n\nActual title\n\nBody content"
        result = _strip_title_line(text)
        assert "Actual title" not in result
        assert "Body content" in result


# ---------------------------------------------------------------------------
# _extract_idea_from_issue
# ---------------------------------------------------------------------------

class TestExtractIdeaFromIssue:
    def test_first_paragraph(self):
        assert "Add dark mode" in _extract_idea_from_issue(
            "## Plan: Add dark mode\n\nDetails"
        )

    def test_skips_metadata(self):
        assert "real idea" in _extract_idea_from_issue(
            "---\n*Generated by Kōan*\n\nThe real idea"
        )

    def test_empty_body(self):
        assert "Review" in _extract_idea_from_issue("")
        assert "Review" in _extract_idea_from_issue(None)

    def test_strips_plan_prefix(self):
        idea = _extract_idea_from_issue("Plan: Implement X\n\nDetails")
        assert idea.startswith("Implement X")

    def test_truncates(self):
        assert len(_extract_idea_from_issue("A" * 600)) <= 500


# ---------------------------------------------------------------------------
# CLI entry point — main()
# ---------------------------------------------------------------------------

class TestCLI:
    def test_idea_mode(self):
        with patch("app.plan_runner.run_plan",
                    return_value=(True, "Plan created")) as mock:
            code = main(["--project-path", "/proj", "--idea", "Add auth"])
            assert code == 0
            mock.assert_called_once()
            assert mock.call_args.kwargs["idea"] == "Add auth"
            assert mock.call_args.kwargs["project_path"] == "/proj"

    def test_issue_url_mode(self):
        url = "https://github.com/o/r/issues/1"
        with patch("app.plan_runner.run_plan",
                    return_value=(True, "Posted")) as mock:
            code = main(["--project-path", "/proj", "--issue-url", url])
            assert code == 0
            assert mock.call_args.kwargs["issue_url"] == url

    def test_failure_returns_1(self):
        with patch("app.plan_runner.run_plan",
                    return_value=(False, "error")):
            code = main(["--project-path", "/proj", "--idea", "bad"])
            assert code == 1

    def test_missing_args_exits(self):
        with pytest.raises(SystemExit):
            main([])

    def test_both_idea_and_url_exits(self):
        with pytest.raises(SystemExit):
            main(["--project-path", "/p", "--idea", "x",
                   "--issue-url", "https://github.com/o/r/issues/1"])

    def test_skill_dir_resolved(self):
        with patch("app.plan_runner.run_plan",
                    return_value=(True, "ok")) as mock:
            main(["--project-path", "/proj", "--idea", "test"])
            skill_dir = mock.call_args.kwargs["skill_dir"]
            assert skill_dir.name == "plan"
            assert "skills/core/plan" in str(skill_dir)


# ---------------------------------------------------------------------------
# Prompt files — structure validation
# ---------------------------------------------------------------------------

PROMPTS_DIR = (
    Path(__file__).parent.parent / "skills" / "core" / "plan" / "prompts"
)


class TestPromptFiles:
    def test_plan_prompt_exists(self):
        assert (PROMPTS_DIR / "plan.md").exists()

    def test_plan_prompt_has_placeholders(self):
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "{IDEA}" in content
        assert "{CONTEXT}" in content

    def test_plan_prompt_has_phases(self):
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "phase" in content.lower()

    def test_plan_iterate_prompt_exists(self):
        assert (PROMPTS_DIR / "plan-iterate.md").exists()

    def test_plan_iterate_prompt_has_placeholders(self):
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "{ISSUE_CONTEXT}" in content

    def test_plan_iterate_prompt_has_required_sections(self):
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "Changes in this iteration" in content
        assert "comments" in content.lower()
        # Implementation Phases comes via {@include plan-phases-format}
        assert "{@include plan-phases-format}" in content or "Implementation Phases" in content
        assert "phase" in content.lower()

    def test_plan_iterate_prompt_instructs_feedback_processing(self):
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "suggestion" in content.lower()
        assert "question" in content.lower()

    def test_plan_prompt_requires_title_line(self):
        """Plan prompt includes title instruction (via partial or inline)."""
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "{@include plan-title-instruction}" in content or "FIRST LINE" in content
        assert "title" in content.lower()

    def test_plan_iterate_prompt_requires_title_line(self):
        """Iterate prompt includes title instruction (via partial or inline)."""
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "{@include plan-title-instruction}" in content or "FIRST LINE" in content

    def test_plan_prompt_has_phase_format(self):
        """Plan prompt includes phase format (via partial or inline)."""
        content = (PROMPTS_DIR / "plan.md").read_text()
        assert "{@include plan-phases-format}" in content or "#### Phase" in content

    def test_plan_iterate_prompt_has_phase_format(self):
        """Iterate prompt includes phase format (via partial or inline)."""
        content = (PROMPTS_DIR / "plan-iterate.md").read_text()
        assert "{@include plan-phases-format}" in content or "#### Phase" in content


# ---------------------------------------------------------------------------
# main() CLI — --context flag
# ---------------------------------------------------------------------------

class TestMainCLI:
    def test_context_flag_passed_to_run_plan(self):
        """--context flag should be forwarded to run_plan."""
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--context", "Focus on phase 2",
            ])
            _, kwargs = mock.call_args
            assert kwargs["context"] == "Focus on phase 2"

    def test_context_flag_optional(self):
        """Omitting --context should pass None."""
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main(["--project-path", "/project", "--idea", "Add feature"])
            _, kwargs = mock.call_args
            assert kwargs["context"] is None

    def test_context_with_idea(self):
        """--context can be used with --idea too."""
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--idea", "Add feature",
                "--context", "Must support dark mode",
            ])
            _, kwargs = mock.call_args
            assert kwargs["idea"] == "Add feature"
            assert kwargs["context"] == "Must support dark mode"

    def test_project_identity_flags_passed_to_run_plan(self):
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--project-name", "webpros-shield",
                "--instance-dir", "/koan/instance",
            ])
            _, kwargs = mock.call_args
            assert kwargs["project_name"] == "webpros-shield"
            assert kwargs["instance_dir"] == "/koan/instance"

    def test_base_branch_flag_passed_to_run_plan(self):
        with patch("app.plan_runner.run_plan", return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--base-branch", "main",
            ])
            _, kwargs = mock.call_args
            assert kwargs["base_branch"] == "main"


class TestMergeContextWithBaseBranch:
    def test_returns_context_when_no_branch(self):
        result = merge_context_with_base_branch("Focus on API", None)
        assert result == "Focus on API"

    def test_returns_branch_hint_when_context_empty(self):
        result = merge_context_with_base_branch("", "main")
        assert result == "Target base branch: `main`."

    def test_combines_context_and_branch_hint(self):
        result = merge_context_with_base_branch("Phase 1 only", "11.126")
        assert "Phase 1 only" in result
        assert "Target base branch: `11.126`." in result


# ---------------------------------------------------------------------------
# _is_simple_plan
# ---------------------------------------------------------------------------

class TestIsSimplePlan:
    def test_single_phase_short_plan_is_simple(self):
        plan = "Rename function foo to bar in utils.py\n\nEdit the file."
        assert is_simple_plan(plan)

    def test_multi_phase_plan_is_not_simple(self):
        plan = (
            "Implement feature\n\n"
            "#### Phase 1\nDo this.\n\n"
            "#### Phase 2\nDo that.\n"
        )
        assert not is_simple_plan(plan)

    def test_single_phase_long_plan_is_not_simple(self):
        # Single phase but many lines — not simple enough to skip review
        plan = "#### Phase 1\n" + "\n".join(f"Step {i}" for i in range(25))
        assert not is_simple_plan(plan)

    def test_empty_plan_is_simple(self):
        assert is_simple_plan("")

    def test_exactly_two_phases_not_simple(self):
        plan = (
            "Title\n\n"
            "#### Phase 1\nDo A.\n\n"
            "#### Phase 2\nDo B.\n"
        )
        assert not is_simple_plan(plan)


# ---------------------------------------------------------------------------
# _review_plan
# ---------------------------------------------------------------------------

class TestReviewPlan:
    def _skill_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_approved_on_approved_output(self):
        with patch("app.cli_provider.run_command", return_value="APPROVED\n"):
            approved, issues = review_plan("## Plan\nStep 1", "/project", self._skill_dir())
        assert approved
        assert issues == ""

    def test_issues_found_returns_false_and_issues(self):
        reviewer_output = "ISSUES_FOUND\n- Phase 1: no file path\n- Phase 2: missing tests"
        with patch("app.cli_provider.run_command", return_value=reviewer_output):
            approved, issues = review_plan("## Plan\nStep 1", "/project", self._skill_dir())
        assert not approved
        assert "no file path" in issues

    def test_malformed_output_treated_as_approved(self):
        with patch("app.cli_provider.run_command", return_value="Maybe looks ok"):
            approved, issues = review_plan("## Plan\nStep 1", "/project", self._skill_dir())
        assert approved

    def test_run_command_exception_fails_open(self):
        with patch("app.cli_provider.run_command", side_effect=RuntimeError("timeout")):
            approved, issues = review_plan("## Plan", "/project", self._skill_dir())
        assert approved

    def test_empty_output_treated_as_approved(self):
        with patch("app.cli_provider.run_command", return_value=""):
            approved, issues = review_plan("## Plan", "/project", self._skill_dir())
        assert approved

    def test_review_pass_honors_dot_koan_skill(self, tmp_path):
        """Sibling sub-pass (plan-review) must also inject .koan/skills/plan/*.md."""
        d = tmp_path / ".koan" / "skills" / "plan"
        d.mkdir(parents=True)
        (d / "house-style.md").write_text("REPO PLAN RULE")
        captured = {}

        def _capture(prompt, project_path, **kwargs):
            captured["prompt"] = prompt
            return "APPROVED\n"

        with patch("app.cli_provider.run_command", side_effect=_capture):
            review_plan("## Plan\nStep 1", str(tmp_path), self._skill_dir())
        assert "REPO PLAN RULE" in captured["prompt"]


# ---------------------------------------------------------------------------
# _review_loop
# ---------------------------------------------------------------------------

class TestReviewLoop:
    def _skill_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_approved_first_round_returns_plan(self):
        with patch("app.plan_runner.review_plan", return_value=(True, "")) as mock_review:
            result = _review_loop(
                "my plan", "/project", idea="idea", context="", skill_dir=self._skill_dir(),
                max_rounds=3,
            )
        assert result == "my plan"
        assert mock_review.call_count == 1

    def test_approved_second_round_after_regen(self):
        review_results = [(False, "- Missing file path"), (True, "")]
        with patch("app.plan_runner.review_plan", side_effect=review_results), \
             patch("app.plan_runner._run_claude_plan", return_value="improved plan"):
            result = _review_loop(
                "initial plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
            )
        assert result == "improved plan"

    def test_max_rounds_exhausted_returns_plan_with_warning(self):
        review_results = [
            (False, "- Phase 1: no file path"),
            (False, "- Phase 1: no file path"),
            (False, "- Phase 1: no file path"),
        ]
        with patch("app.plan_runner.review_plan", side_effect=review_results), \
             patch("app.plan_runner._run_claude_plan", return_value="regen plan"):
            result = _review_loop(
                "initial plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
            )
        assert "⚠️" in result
        assert "human review recommended" in result

    def test_regen_failure_keeps_previous_plan(self):
        with patch("app.plan_runner.review_plan", return_value=(False, "- issue")), \
             patch("app.plan_runner._run_claude_plan", side_effect=RuntimeError("boom")):
            result = _review_loop(
                "original plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=2,
            )
        # Should not crash; should contain warning after max rounds
        assert "original plan" in result or "⚠️" in result

    def test_regen_empty_keeps_previous_plan(self):
        review_results = [(False, "- issue"), (True, "")]
        with patch("app.plan_runner.review_plan", side_effect=review_results), \
             patch("app.plan_runner._run_claude_plan", return_value=""):
            result = _review_loop(
                "original plan", "/project", idea="idea", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
            )
        # Empty regen keeps original; then approved on round 2 with original
        assert result == "original plan"

    def test_iteration_mode_uses_plan_iterate_prompt(self):
        with patch("app.plan_runner.review_plan", side_effect=[(False, "- issue"), (True, "")]), \
             patch("app.plan_runner._run_claude_plan", return_value="iter plan") as mock_run, \
             patch("app.plan_runner.load_prompt_or_skill", return_value="prompt text") as mock_load:
            result = _review_loop(
                "initial", "/project", idea="", context="",
                skill_dir=self._skill_dir(), max_rounds=3,
                is_iteration=True, issue_context="issue ctx",
            )
        # Should have called load_prompt_or_skill with "plan-iterate"
        calls = [c[0][1] for c in mock_load.call_args_list]
        assert "plan-iterate" in calls


# ---------------------------------------------------------------------------
# _generate_plan — review loop integration
# ---------------------------------------------------------------------------

class TestGeneratePlanWithReview:
    def _skill_dir(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_review_skipped_for_simple_plan(self):
        short_plan = "Do one thing quickly."
        with patch("app.plan_runner._run_claude_plan", return_value=short_plan), \
             patch("app.plan_runner._review_loop") as mock_loop, \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": True, "max_rounds": 3}):
            result = _generate_plan("/project", "rename X", skill_dir=self._skill_dir())
        mock_loop.assert_not_called()
        assert result == short_plan

    def test_review_runs_for_multi_phase_plan(self):
        big_plan = (
            "Multi-phase feature\n\n"
            "#### Phase 1\nDo A.\n\n"
            "#### Phase 2\nDo B.\n"
        )
        reviewed_plan = big_plan + "\n(reviewed)"
        with patch("app.plan_runner._run_claude_plan", return_value=big_plan), \
             patch("app.plan_runner._review_loop", return_value=reviewed_plan) as mock_loop, \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": True, "max_rounds": 3}):
            result = _generate_plan("/project", "big feature", skill_dir=self._skill_dir())
        mock_loop.assert_called_once()
        assert result == reviewed_plan

    def test_review_disabled_skips_loop(self):
        big_plan = (
            "Multi-phase feature\n\n"
            "#### Phase 1\nDo A.\n\n"
            "#### Phase 2\nDo B.\n"
        )
        with patch("app.plan_runner._run_claude_plan", return_value=big_plan), \
             patch("app.plan_runner._review_loop") as mock_loop, \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": False, "max_rounds": 3}):
            _generate_plan("/project", "big feature", skill_dir=self._skill_dir())
        mock_loop.assert_not_called()


# ---------------------------------------------------------------------------
# _critic_loop — iterative plan refinement
# ---------------------------------------------------------------------------

class TestCriticLoop:
    def _skill_dir(self):
        return Path("/fake/skills/core/plan")

    @patch("app.plan_runner.load_prompt_or_skill", return_value="critic prompt")
    @patch("app.cli_provider.run_command", return_value="1. Phase 2 missing X")
    @patch("app.plan_runner._run_claude_plan")
    def test_iterations_3_runs_two_critic_rounds(self, mock_regen, mock_critic, mock_load):
        mock_regen.side_effect = ["plan v2", "plan v3"]
        result = _critic_loop(
            "initial plan", "/project", idea="Add feature", context="",
            skill_dir=self._skill_dir(), iterations=3,
        )
        assert mock_critic.call_count == 2
        assert mock_regen.call_count == 2
        assert result == "plan v3"

    @patch("app.plan_runner.load_prompt_or_skill", return_value="critic prompt")
    @patch("app.cli_provider.run_command", return_value="NO_GAPS_FOUND")
    @patch("app.plan_runner._run_claude_plan")
    def test_early_exit_on_no_gaps(self, mock_regen, mock_critic, mock_load):
        result = _critic_loop(
            "initial plan", "/project", idea="Add feature", context="",
            skill_dir=self._skill_dir(), iterations=5,
        )
        assert mock_critic.call_count == 1
        mock_regen.assert_not_called()
        assert result == "initial plan"

    @patch("app.plan_runner.load_prompt_or_skill", return_value="critic prompt")
    @patch("app.cli_provider.run_command", return_value="1. gap found")
    @patch("app.plan_runner._run_claude_plan")
    def test_progress_notifications(self, mock_regen, mock_critic, mock_load):
        mock_regen.return_value = "plan v2"
        mock_notify = MagicMock()
        _critic_loop(
            "initial plan", "/project", idea="idea", context="",
            skill_dir=self._skill_dir(), iterations=3, notify_fn=mock_notify,
        )
        calls = [c[0][0] for c in mock_notify.call_args_list]
        assert any("turn 1/3" in c for c in calls)
        assert any("turn 2/3" in c for c in calls)

    @patch("app.plan_runner.load_prompt_or_skill", return_value="critic prompt")
    @patch("app.cli_provider.run_command", side_effect=RuntimeError("timeout"))
    def test_critic_failure_returns_current_plan(self, mock_critic, mock_load):
        mock_notify = MagicMock()
        result = _critic_loop(
            "initial plan", "/project", idea="idea", context="",
            skill_dir=self._skill_dir(), iterations=3, notify_fn=mock_notify,
        )
        assert result == "initial plan"
        warn_calls = [c[0][0] for c in mock_notify.call_args_list if "failed" in c[0][0]]
        assert len(warn_calls) == 1

    @patch("app.plan_runner.load_prompt_or_skill", return_value="critic prompt")
    @patch("app.cli_provider.run_command", return_value="")
    def test_empty_critic_response_warns_and_stops(self, mock_critic, mock_load):
        mock_notify = MagicMock()
        result = _critic_loop(
            "initial plan", "/project", idea="idea", context="",
            skill_dir=self._skill_dir(), iterations=3, notify_fn=mock_notify,
        )
        assert result == "initial plan"
        warn_calls = [c[0][0] for c in mock_notify.call_args_list if "empty" in c[0][0]]
        assert len(warn_calls) == 1

    @patch("app.plan_runner.load_prompt_or_skill", return_value="critic prompt")
    @patch("app.cli_provider.run_command", return_value="1. gap found")
    @patch("app.plan_runner._run_claude_plan", return_value="")
    def test_empty_regeneration_keeps_current(self, mock_regen, mock_critic, mock_load):
        result = _critic_loop(
            "initial plan", "/project", idea="idea", context="",
            skill_dir=self._skill_dir(), iterations=2,
        )
        assert result == "initial plan"


class TestGeneratePlanWithCriticLoop:
    def _skill_dir(self):
        return Path("/fake/skills/core/plan")

    @patch("app.cli_provider.run_command_streaming", return_value="## Plan\n\nStep 1")
    @patch("app.plan_runner._critic_loop")
    def test_iterations_1_skips_critic(self, mock_critic, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"), \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": False, "max_rounds": 3}):
            _generate_plan(
                "/project", "idea", skill_dir=self._skill_dir(), iterations=1,
            )
        mock_critic.assert_not_called()

    @patch("app.cli_provider.run_command_streaming", return_value="## Plan\n\nStep 1")
    @patch("app.plan_runner._critic_loop", return_value="refined plan")
    def test_iterations_3_calls_critic(self, mock_critic, mock_run):
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"), \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": False, "max_rounds": 3}):
            result = _generate_plan(
                "/project", "idea", skill_dir=self._skill_dir(), iterations=3,
            )
        mock_critic.assert_called_once()
        assert result == "refined plan"

    @patch("app.cli_provider.run_command_streaming", return_value="## Plan\n\nStep 1")
    @patch("app.plan_runner._critic_loop", return_value="refined plan")
    def test_github_post_fires_once(self, mock_critic, mock_run):
        """Only the final plan is returned — caller posts once."""
        with patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"), \
             patch("app.config.get_plan_review_config",
                   return_value={"enabled": False, "max_rounds": 3}):
            result = _generate_plan(
                "/project", "idea", skill_dir=self._skill_dir(), iterations=3,
            )
        assert result == "refined plan"
        assert mock_run.call_count == 1


class TestMainIterationsArgparseValidation:
    def test_iterations_out_of_range_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            main(["--project-path", "/tmp", "--idea", "test", "--iterations", "100"])

    def test_iterations_zero_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            main(["--project-path", "/tmp", "--idea", "test", "--iterations", "0"])


# ---------------------------------------------------------------------------
# review_plan_assumptions
# ---------------------------------------------------------------------------

class TestReviewPlanAssumptions:
    """Tests for the assumptions pressure-test subagent."""

    _PLAN_DIR = Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    def test_assumptions_ok(self):
        output = "ASSUMPTIONS_OK\n1. [VERIFIED] Function exists in module"
        with patch("app.cli_provider.run_command", return_value=output):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_OK
            assert reason == ""

    def test_critical_assumption_unverified(self):
        output = (
            "CRITICAL_ASSUMPTION_UNVERIFIED\n"
            "1. [UNVERIFIED/CRITICAL] The API endpoint /v2/users does not exist\n"
            "The plan must verify /v2/users before proceeding."
        )
        with patch("app.cli_provider.run_command", return_value=output):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_CRITICAL
            assert "/v2/users" in reason

    def test_empty_output_fails_closed(self):
        with patch("app.cli_provider.run_command", return_value=""):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_REVIEWER_ERROR
            assert "empty output" in reason

    def test_none_output_fails_closed(self):
        with patch("app.cli_provider.run_command", return_value=None):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_REVIEWER_ERROR
            assert "empty output" in reason

    def test_unexpected_output_returns_reviewer_error(self):
        with patch("app.cli_provider.run_command", return_value="Some random text"):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_REVIEWER_ERROR
            assert "unparseable" in reason

    def test_subagent_exception_fails_closed(self):
        with patch("app.cli_provider.run_command",
                    side_effect=RuntimeError("model unavailable")):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_REVIEWER_ERROR
            assert "model unavailable" in reason

    def test_prompt_load_failure_fails_closed(self):
        with patch("app.plan_runner.load_prompt_or_skill",
                    side_effect=FileNotFoundError("missing")):
            status, reason = review_plan_assumptions("plan text", "/project", self._PLAN_DIR)
            assert status == ASSUMPTIONS_REVIEWER_ERROR
            assert "prompt load failed" in reason


class TestMergeAssumptionsIntoOpenQuestions:
    """Tests for folding audit findings into the Open Questions section."""

    _FINDINGS = "1. [UNVERIFIED/CRITICAL] The API endpoint /v2/users exists"

    def test_appends_under_existing_open_questions_section(self):
        plan = (
            "### Summary\n\nDo the thing.\n\n"
            "### Open Questions\n\n- Is the cache shared?\n\n"
            "### Risks\n\n- None.\n"
        )
        merged = _merge_assumptions_into_open_questions(plan, self._FINDINGS)
        assert "Assumptions audit (auto):" in merged
        assert self._FINDINGS in merged
        # Findings land inside Open Questions: after the existing bullet,
        # before the next section heading.
        oq = merged.index("### Open Questions")
        risks = merged.index("### Risks")
        assert oq < merged.index("Assumptions audit (auto):") < risks
        assert merged.index("Is the cache shared?") < merged.index("Assumptions audit (auto):")

    def test_creates_section_when_missing(self):
        plan = "### Summary\n\nDo the thing.\n"
        merged = _merge_assumptions_into_open_questions(plan, self._FINDINGS)
        assert "### Open Questions" in merged
        assert merged.index("### Open Questions") < merged.index(self._FINDINGS)

    def test_open_questions_as_last_section(self):
        plan = "### Summary\n\nDo it.\n\n### Open Questions\n\n- Any?\n"
        merged = _merge_assumptions_into_open_questions(plan, self._FINDINGS)
        assert merged.rstrip().endswith(self._FINDINGS)

    def test_matches_heading_level_variants(self):
        plan = "## Summary\n\nDo it.\n\n## Open questions\n\n- Any?\n\n## Risks\n\n- None.\n"
        merged = _merge_assumptions_into_open_questions(plan, self._FINDINGS)
        assert merged.index("## Open questions") < merged.index(self._FINDINGS) < merged.index("## Risks")


class TestApplyAssumptionsAudit:
    """Tests for the advisory pre-post assumptions audit in plan generation."""

    _PLAN_DIR = Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"
    _PLAN = "### Summary\n\nBig plan.\n\n" + "#### Phase 1\nDo stuff\n" * 10

    def test_critical_findings_merged_into_plan(self):
        findings = "1. [UNVERIFIED/CRITICAL] Endpoint /v2/users exists"
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"assumptions_check": True}), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, findings)):
            result = _apply_assumptions_audit(self._PLAN, "/project", self._PLAN_DIR)
            assert "Assumptions audit (auto):" in result
            assert findings in result

    def test_critical_findings_notify_user(self):
        notify = MagicMock()
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"assumptions_check": True}), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, "1. [UNVERIFIED/CRITICAL] X")):
            _apply_assumptions_audit(
                self._PLAN, "/project", self._PLAN_DIR, notify_fn=notify,
            )
            notify.assert_called_once()
            assert "Open Questions" in notify.call_args[0][0]

    def test_ok_leaves_plan_unchanged(self):
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"assumptions_check": True}), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_OK, "")):
            assert _apply_assumptions_audit(self._PLAN, "/project", self._PLAN_DIR) == self._PLAN

    def test_reviewer_error_fails_open_unchanged(self):
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"assumptions_check": True}), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_REVIEWER_ERROR, "model unavailable")):
            assert _apply_assumptions_audit(self._PLAN, "/project", self._PLAN_DIR) == self._PLAN

    def test_simple_plan_skips_auditor(self):
        with patch("app.plan_runner.is_simple_plan", return_value=True), \
             patch("app.plan_runner.review_plan_assumptions") as mock_audit:
            assert _apply_assumptions_audit("Rename X to Y", "/project", self._PLAN_DIR) == "Rename X to Y"
            mock_audit.assert_not_called()

    def test_disabled_skips_auditor(self):
        with patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"assumptions_check": False}), \
             patch("app.plan_runner.review_plan_assumptions") as mock_audit:
            assert _apply_assumptions_audit(self._PLAN, "/project", self._PLAN_DIR) == self._PLAN
            mock_audit.assert_not_called()

    def test_generate_plan_pipeline_includes_audit_block(self):
        """Pipeline: _generate_plan output carries the audit block pre-post."""
        raw_plan = self._PLAN + "\n### Open Questions\n\n- None yet.\n"
        findings = "1. [UNVERIFIED/CRITICAL] Config key is read by module W"
        with patch("app.plan_runner._run_claude_plan", return_value=raw_plan), \
             patch("app.skill_memory.build_memory_block_for_skill", return_value=""), \
             patch("app.plan_runner.load_prompt_or_skill", return_value="prompt"), \
             patch("app.plan_runner.is_simple_plan", return_value=False), \
             patch("app.config.get_plan_review_config",
                    return_value={"enabled": True, "max_rounds": 3,
                                  "implement_gate": True, "assumptions_check": True}), \
             patch("app.plan_runner.review_plan", return_value=(True, "")), \
             patch("app.plan_runner._record_plan_metric"), \
             patch("app.plan_runner.review_plan_assumptions",
                    return_value=(ASSUMPTIONS_CRITICAL, findings)):
            plan = _generate_plan("/project", "an idea", skill_dir=self._PLAN_DIR)
            assert "Assumptions audit (auto):" in plan
            assert findings in plan
            # Folded into the existing Open Questions section
            assert plan.index("### Open Questions") < plan.index(findings)


def test_run_claude_plan_passes_gated_mcp_configs():
    """Primary plan generation forwards role-gated MCP configs."""
    import app.plan_runner as pr

    with patch("app.cli_provider.run_command_streaming", return_value="PLAN") as rcs, \
         patch("app.config.mcp_configs_for_role", return_value=["/mcp.json"]) as gate, \
         patch("app.config.get_skill_max_turns", return_value=50), \
         patch("app.config.get_skill_timeout", return_value=600):
        pr._run_claude_plan("prompt", "/proj", project_name="proj")
    gate.assert_called_once_with("plan", "proj")
    assert rcs.call_args.kwargs["mcp_configs"] == ["/mcp.json"]
