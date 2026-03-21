"""Tests for check_runner.py — PR/issue check pipeline."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "instance"
    d.mkdir()
    missions_md = d / "missions.md"
    missions_md.write_text("## Pending\n\n## In Progress\n\n## Done\n")
    return d


@pytest.fixture
def koan_root(tmp_path):
    return str(tmp_path)


def _pr_json(**overrides):
    """Build a realistic PR metadata dict."""
    base = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "updatedAt": "2026-02-07T10:00:00Z",
        "headRefName": "koan/fix-xyz",
        "baseRefName": "main",
        "title": "Fix XYZ",
        "isDraft": False,
        "author": {"login": "koan-bot"},
        "url": "https://github.com/sukria/koan/pull/99",
    }
    base.update(overrides)
    return base


def _issue_json(**overrides):
    """Build a realistic issue metadata dict."""
    base = {
        "state": "open",
        "updatedAt": "2026-02-07T10:00:00Z",
        "title": "Improve performance",
        "url": "https://github.com/sukria/koan/issues/42",
        "comments": 3,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# run_check() — URL routing
# ---------------------------------------------------------------------------

class TestRunCheckRouting:
    def test_pr_url_routes_to_pr_handler(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "merged" in msg.lower()

    def test_issue_url_routes_to_issue_handler(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json(state="closed")
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.mark_checked"):
            success, msg = run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "closed" in msg.lower()

    def test_invalid_url_returns_failure(self, instance_dir, koan_root):
        from app.check_runner import run_check

        notify = MagicMock()
        success, msg = run_check(
            "https://example.com/nothing",
            str(instance_dir), koan_root, notify_fn=notify,
        )
        assert not success
        assert "No valid" in msg


# ---------------------------------------------------------------------------
# PR handling — closed/merged
# ---------------------------------------------------------------------------

class TestPrClosed:
    def test_closed_pr_no_action(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(state="CLOSED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked") as mock_mark:
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "closed" in msg.lower()
            mock_mark.assert_called_once()

    def test_merged_pr_no_action(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert "merged" in msg.lower()


# ---------------------------------------------------------------------------
# PR handling — no changes since last check
# ---------------------------------------------------------------------------

class TestPrNoChanges:
    def test_skip_when_no_updates(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=False):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "no updates" in msg.lower()


# ---------------------------------------------------------------------------
# PR handling — rebase needed
# ---------------------------------------------------------------------------

class TestPrRebase:
    def test_conflicting_pr_queues_rebase(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(mergeable="CONFLICTING")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "Rebase queued" in msg
            mock_insert.assert_called_once()
            entry = mock_insert.call_args[0][1]
            assert "Rebase PR #42" in entry
            assert "app.rebase_pr" in entry

    def test_rebase_mission_has_project_path(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(mergeable="CONFLICTING")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            entry = mock_insert.call_args[0][1]
            assert "--project-path /home/koan" in entry

    def test_rebase_without_project_path(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(mergeable="CONFLICTING")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            run_check(
                "https://github.com/unknown/repo/pull/10",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            entry = mock_insert.call_args[0][1]
            assert "--project-path" not in entry


# ---------------------------------------------------------------------------
# PR handling — review needed
# ---------------------------------------------------------------------------

class TestPrReview:
    def test_no_review_queues_pr_review(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="", isDraft=False, mergeable="MERGEABLE")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "review queued" in msg.lower()
            mock_insert.assert_called_once()
            entry = mock_insert.call_args[0][1]
            assert "/pr" in entry
            assert "Review PR #42" in entry

    def test_draft_pr_no_review(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="", isDraft=True, mergeable="MERGEABLE")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            mock_insert.assert_not_called()

    def test_approved_pr_no_review_queued(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="APPROVED", isDraft=False, mergeable="MERGEABLE")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert "No action needed" in msg

    def test_conflicting_pr_no_review_queued(self, instance_dir, koan_root):
        """When rebase is needed, skip review (rebase first)."""
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="", isDraft=False, mergeable="CONFLICTING")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/koan"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            # Only rebase should be queued, not review
            assert mock_insert.call_count == 1
            entry = mock_insert.call_args[0][1]
            assert "Rebase" in entry


# ---------------------------------------------------------------------------
# PR handling — clean status
# ---------------------------------------------------------------------------

class TestPrCleanStatus:
    def test_clean_pr_reports_status(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(
            reviewDecision="APPROVED",
            mergeable="MERGEABLE",
            headRefName="koan/fix-xyz",
            baseRefName="main",
        )
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "\u2705" in msg
            assert "koan/fix-xyz" in msg
            assert "No action needed" in msg


# ---------------------------------------------------------------------------
# PR handling — fetch error
# ---------------------------------------------------------------------------

class TestPrFetchError:
    def test_fetch_error_returns_failure(self, instance_dir, koan_root):
        from app.check_runner import run_check

        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata",
                    side_effect=RuntimeError("timeout")):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert not success
            assert "timeout" in msg


# ---------------------------------------------------------------------------
# PR handling — notifications
# ---------------------------------------------------------------------------

class TestPrNotifications:
    def test_sends_checking_notification(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            # First call should be the "Checking..." notification
            first_msg = notify.call_args_list[0][0][0]
            assert "Checking PR #42" in first_msg

    def test_sends_result_notification(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            run_check(
                "https://github.com/sukria/koan/pull/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            # Last call should be the result
            last_msg = notify.call_args_list[-1][0][0]
            assert "merged" in last_msg.lower()


# ---------------------------------------------------------------------------
# Issue handling — closed
# ---------------------------------------------------------------------------

class TestIssueClosed:
    def test_closed_issue_no_action(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json(state="closed")
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.mark_checked") as mock_mark:
            success, msg = run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "closed" in msg.lower()
            mock_mark.assert_called_once()


# ---------------------------------------------------------------------------
# Issue handling — no changes
# ---------------------------------------------------------------------------

class TestIssueNoChanges:
    def test_skip_when_no_updates(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.has_changed", return_value=False):
            success, msg = run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "no updates" in msg.lower()


# ---------------------------------------------------------------------------
# Issue handling — plan queued
# ---------------------------------------------------------------------------

class TestIssuePlan:
    def test_updated_issue_queues_plan(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.resolve_project_path", return_value="/home/koan"):
            success, msg = run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "/plan queued" in msg.lower()
            mock_insert.assert_called_once()
            entry = mock_insert.call_args[0][1]
            assert "github.com/sukria/koan/issues/42" in entry

    def test_plan_mission_has_project_tag(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.resolve_project_path", return_value="/home/koan"):
            run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            entry = mock_insert.call_args[0][1]
            assert "[project:koan]" in entry

    def test_plan_mission_has_run_command_when_project_found(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.resolve_project_path", return_value="/home/koan"):
            run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            entry = mock_insert.call_args[0][1]
            assert "run: `" in entry
            assert "app.plan_runner" in entry

    def test_plan_mission_fallback_when_no_project_path(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/home/koan")]), \
             patch("app.utils.resolve_project_path", return_value=None):
            run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            entry = mock_insert.call_args[0][1]
            assert "/plan" in entry


# ---------------------------------------------------------------------------
# Issue handling — fetch error
# ---------------------------------------------------------------------------

class TestIssueFetchError:
    def test_fetch_error_returns_failure(self, instance_dir, koan_root):
        from app.check_runner import run_check

        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata",
                    side_effect=RuntimeError("not found")):
            success, msg = run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert not success
            assert "not found" in msg


# ---------------------------------------------------------------------------
# Issue handling — notifications
# ---------------------------------------------------------------------------

class TestIssueNotifications:
    def test_sends_checking_notification(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json(state="closed")
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.mark_checked"):
            run_check(
                "https://github.com/sukria/koan/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            first_msg = notify.call_args_list[0][0][0]
            assert "Checking issue #42" in first_msg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_canonical_url_pr(self):
        from app.check_runner import _canonical_url
        assert _canonical_url("sukria", "koan", "pull", "42") == \
            "https://github.com/sukria/koan/pull/42"

    def test_canonical_url_issue(self):
        from app.check_runner import _canonical_url
        assert _canonical_url("sukria", "koan", "issues", "7") == \
            "https://github.com/sukria/koan/issues/7"

    def test_needs_rebase_conflicting(self):
        from app.check_runner import _needs_rebase
        assert _needs_rebase({"mergeable": "CONFLICTING"}) is True

    def test_needs_rebase_mergeable(self):
        from app.check_runner import _needs_rebase
        assert _needs_rebase({"mergeable": "MERGEABLE"}) is False

    def test_has_no_reviews_empty(self):
        from app.check_runner import _has_no_reviews
        assert _has_no_reviews({"reviewDecision": ""}) is True

    def test_has_no_reviews_approved(self):
        from app.check_runner import _has_no_reviews
        assert _has_no_reviews({"reviewDecision": "APPROVED"}) is False

    def test_resolve_project_name_known(self):
        from app.check_runner import _resolve_project_name
        with patch("app.utils.resolve_project_path",
                    return_value="/home/koan"), \
             patch("app.utils.project_name_for_path",
                    return_value="koan"):
            assert _resolve_project_name("koan") == "koan"

    def test_resolve_project_name_unknown(self):
        from app.check_runner import _resolve_project_name
        with patch("app.utils.resolve_project_path",
                    return_value=None):
            assert _resolve_project_name("myrepo") == "myrepo"

    def test_resolve_project_name_case_insensitive(self):
        from app.check_runner import _resolve_project_name
        with patch("app.utils.resolve_project_path",
                    return_value="/home/koan"), \
             patch("app.utils.project_name_for_path",
                    return_value="Koan"):
            assert _resolve_project_name("koan") == "Koan"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_success(self, instance_dir, koan_root):
        from app.check_runner import main

        pr_data = _pr_json(state="MERGED")
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.notify.send_telegram"):
            exit_code = main([
                "https://github.com/sukria/koan/pull/42",
                "--instance-dir", str(instance_dir),
                "--koan-root", koan_root,
            ])
            assert exit_code == 0

    def test_main_failure(self, instance_dir, koan_root):
        from app.check_runner import main

        with patch("app.check_runner._fetch_pr_metadata",
                    side_effect=RuntimeError("oops")), \
             patch("app.notify.send_telegram"):
            exit_code = main([
                "https://github.com/sukria/koan/pull/42",
                "--instance-dir", str(instance_dir),
                "--koan-root", koan_root,
            ])
            assert exit_code == 1

    def test_main_missing_instance_dir(self):
        from app.check_runner import main

        with patch.dict("os.environ", {}, clear=True):
            exit_code = main([
                "https://github.com/sukria/koan/pull/42",
                "--koan-root", "/tmp/koan",
            ])
            assert exit_code == 1

    def test_main_missing_koan_root(self, instance_dir):
        from app.check_runner import main

        with patch.dict("os.environ", {}, clear=True):
            exit_code = main([
                "https://github.com/sukria/koan/pull/42",
                "--instance-dir", str(instance_dir),
            ])
            assert exit_code == 1

    def test_main_invalid_url(self, instance_dir, koan_root):
        from app.check_runner import main

        with patch("app.notify.send_telegram"):
            exit_code = main([
                "https://example.com/nothing",
                "--instance-dir", str(instance_dir),
                "--koan-root", koan_root,
            ])
            assert exit_code == 1

    def test_main_uses_env_vars(self, instance_dir, koan_root):
        """CLI reads from KOAN_INSTANCE_DIR and KOAN_ROOT env vars."""
        import os
        from app.check_runner import main

        pr_data = _pr_json(state="MERGED")
        env = {
            "KOAN_INSTANCE_DIR": str(instance_dir),
            "KOAN_ROOT": koan_root,
        }
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.notify.send_telegram"), \
             patch.dict(os.environ, env):
            exit_code = main([
                "https://github.com/sukria/koan/pull/42",
            ])
            assert exit_code == 0


# ---------------------------------------------------------------------------
# URL regex edge cases
# ---------------------------------------------------------------------------

class TestUrlRegex:
    def test_http_pr_url_accepted(self, instance_dir, koan_root):
        """http:// (not just https://) should match."""
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            success, _ = run_check(
                "http://github.com/owner/repo/pull/1",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success

    def test_http_issue_url_accepted(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json(state="closed")
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.mark_checked"):
            success, _ = run_check(
                "http://github.com/owner/repo/issues/5",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success

    def test_url_with_trailing_text(self, instance_dir, koan_root):
        """URL embedded in a sentence should still match."""
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            success, _ = run_check(
                "Check this: https://github.com/foo/bar/pull/77 please",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success

    def test_pr_url_extracts_correct_groups(self):
        from app.github_url_parser import search_pr_url

        owner, repo, number = search_pr_url(
            "https://github.com/my-org/my-repo/pull/999"
        )
        assert owner == "my-org"
        assert repo == "my-repo"
        assert number == "999"

    def test_issue_url_extracts_correct_groups(self):
        from app.github_url_parser import search_issue_url

        owner, repo, number = search_issue_url(
            "https://github.com/acme/widget/issues/42"
        )
        assert owner == "acme"
        assert repo == "widget"
        assert number == "42"

    def test_pr_url_not_matching_issue(self):
        """PR parser should not match issue URLs."""
        from app.github_url_parser import search_pr_url

        with pytest.raises(ValueError):
            search_pr_url("https://github.com/o/r/issues/1")

    def test_issue_url_not_matching_pr(self):
        """Issue parser should not match PR URLs."""
        from app.github_url_parser import search_issue_url

        with pytest.raises(ValueError):
            search_issue_url("https://github.com/o/r/pull/1")


# ---------------------------------------------------------------------------
# Helper edge cases
# ---------------------------------------------------------------------------

class TestHelperEdgeCases:
    def test_needs_rebase_unknown(self):
        """UNKNOWN mergeable should not trigger rebase."""
        from app.check_runner import _needs_rebase
        assert _needs_rebase({"mergeable": "UNKNOWN"}) is False

    def test_needs_rebase_missing_key(self):
        """Missing mergeable key defaults to UNKNOWN → no rebase."""
        from app.check_runner import _needs_rebase
        assert _needs_rebase({}) is False

    def test_has_no_reviews_none_value(self):
        """None reviewDecision means no reviews."""
        from app.check_runner import _has_no_reviews
        assert _has_no_reviews({"reviewDecision": None}) is True

    def test_has_no_reviews_changes_requested(self):
        from app.check_runner import _has_no_reviews
        assert _has_no_reviews({"reviewDecision": "CHANGES_REQUESTED"}) is False

    def test_has_no_reviews_missing_key(self):
        """Missing key should return True (no decision found)."""
        from app.check_runner import _has_no_reviews
        assert _has_no_reviews({}) is True

    def test_resolve_project_name_with_owner(self):
        from app.check_runner import _resolve_project_name
        with patch("app.utils.resolve_project_path",
                    return_value="/home/test-proj") as mock_resolve, \
             patch("app.utils.project_name_for_path",
                    return_value="test-proj"):
            result = _resolve_project_name("test-proj", owner="acme")
            mock_resolve.assert_called_once_with("test-proj", owner="acme")
            assert result == "test-proj"


# ---------------------------------------------------------------------------
# _queue_rebase — direct tests
# ---------------------------------------------------------------------------

class TestQueueRebase:
    def test_rebase_entry_format(self, instance_dir, koan_root):
        from app.check_runner import _queue_rebase

        missions_path = instance_dir / "missions.md"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/proj"), \
             patch("app.utils.project_name_for_path", return_value="proj"):
            _queue_rebase("owner", "repo", "42", missions_path,
                          koan_root, instance_dir)
            mock_insert.assert_called_once()
            entry = mock_insert.call_args[0][1]
            assert "[project:proj]" in entry
            assert "Rebase PR #42" in entry
            assert "owner/repo" in entry

    def test_rebase_without_project_path(self, instance_dir, koan_root):
        from app.check_runner import _queue_rebase

        missions_path = instance_dir / "missions.md"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value=None):
            _queue_rebase("owner", "repo", "10", missions_path,
                          koan_root, instance_dir)
            entry = mock_insert.call_args[0][1]
            assert "--project-path" not in entry


# ---------------------------------------------------------------------------
# _queue_pr_review — direct tests
# ---------------------------------------------------------------------------

class TestQueuePrReview:
    def test_review_entry_format(self, instance_dir):
        from app.check_runner import _queue_pr_review

        missions_path = instance_dir / "missions.md"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value="/home/proj"), \
             patch("app.utils.project_name_for_path", return_value="proj"):
            _queue_pr_review("owner", "repo", "99", missions_path)
            mock_insert.assert_called_once()
            entry = mock_insert.call_args[0][1]
            assert "[project:proj]" in entry
            assert "Review PR #99" in entry
            assert "/pr https://github.com/owner/repo/pull/99" in entry

    def test_review_unknown_project_uses_repo_name(self, instance_dir):
        from app.check_runner import _queue_pr_review

        missions_path = instance_dir / "missions.md"
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.utils.resolve_project_path", return_value=None):
            _queue_pr_review("owner", "unknown-repo", "5", missions_path)
            entry = mock_insert.call_args[0][1]
            assert "[project:unknown-repo]" in entry


# ---------------------------------------------------------------------------
# _queue_plan — direct tests
# ---------------------------------------------------------------------------

class TestQueuePlan:
    def test_plan_with_project_path(self, instance_dir, koan_root):
        from app.check_runner import _queue_plan

        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.check_runner._resolve_project_name", return_value="proj"), \
             patch("app.check_runner._resolve_project_path",
                    return_value="/home/proj"):
            _queue_plan("owner", "repo", "42", "Fix the thing",
                        instance_dir, koan_root)
            entry = mock_insert.call_args[0][1]
            assert "[project:proj]" in entry
            assert "app.plan_runner" in entry
            assert "--issue-url" in entry
            assert "issues/42" in entry

    def test_plan_without_project_path(self, instance_dir, koan_root):
        from app.check_runner import _queue_plan

        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.check_runner._resolve_project_name", return_value="repo"), \
             patch("app.check_runner._resolve_project_path",
                    return_value=None):
            _queue_plan("owner", "repo", "42", "Fix it",
                        instance_dir, koan_root)
            entry = mock_insert.call_args[0][1]
            assert "/plan" in entry
            assert "app.plan_runner" not in entry

    def test_plan_title_truncation(self, instance_dir, koan_root):
        """Titles longer than 80 chars should be truncated."""
        from app.check_runner import _queue_plan

        long_title = "A" * 120
        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.check_runner._resolve_project_name", return_value="proj"), \
             patch("app.check_runner._resolve_project_path",
                    return_value="/home/proj"):
            _queue_plan("owner", "repo", "42", long_title,
                        instance_dir, koan_root)
            entry = mock_insert.call_args[0][1]
            # Title should be truncated to 80 chars
            assert "A" * 80 in entry
            assert "A" * 81 not in entry

    def test_plan_empty_title_fallback(self, instance_dir, koan_root):
        """Empty title should fall back to 'issue #N'."""
        from app.check_runner import _queue_plan

        with patch("app.utils.insert_pending_mission") as mock_insert, \
             patch("app.check_runner._resolve_project_name", return_value="proj"), \
             patch("app.check_runner._resolve_project_path",
                    return_value="/home/proj"):
            _queue_plan("owner", "repo", "42", "",
                        instance_dir, koan_root)
            entry = mock_insert.call_args[0][1]
            assert "issue #42" in entry


# ---------------------------------------------------------------------------
# _fetch_pr_metadata — JSON parsing
# ---------------------------------------------------------------------------

class TestFetchPrMetadata:
    def test_parses_gh_json_output(self):
        from app.check_runner import _fetch_pr_metadata

        expected = _pr_json()
        with patch("app.github.run_gh", return_value=json.dumps(expected)):
            result = _fetch_pr_metadata("sukria", "koan", "99")
            assert result["state"] == "OPEN"
            assert result["title"] == "Fix XYZ"

    def test_passes_correct_args_to_gh(self):
        from app.check_runner import _fetch_pr_metadata

        with patch("app.github.run_gh", return_value="{}") as mock_gh:
            _fetch_pr_metadata("acme", "widget", "7")
            args = mock_gh.call_args[0]
            assert "pr" in args
            assert "view" in args
            assert "7" in args
            # --repo flag
            kwargs_positionals = mock_gh.call_args[0]
            call_str = " ".join(str(a) for a in kwargs_positionals)
            assert "acme/widget" in call_str


# ---------------------------------------------------------------------------
# _fetch_issue_metadata — JSON parsing
# ---------------------------------------------------------------------------

class TestFetchIssueMetadata:
    def test_parses_api_json_output(self):
        from app.check_runner import _fetch_issue_metadata

        expected = _issue_json()
        with patch("app.github.api", return_value=json.dumps(expected)):
            result = _fetch_issue_metadata("sukria", "koan", "42")
            assert result["state"] == "open"
            assert result["title"] == "Improve performance"

    def test_passes_correct_endpoint(self):
        from app.check_runner import _fetch_issue_metadata

        with patch("app.github.api", return_value="{}") as mock_api:
            _fetch_issue_metadata("acme", "widget", "7")
            endpoint = mock_api.call_args[0][0]
            assert endpoint == "repos/acme/widget/issues/7"


# ---------------------------------------------------------------------------
# Error message truncation
# ---------------------------------------------------------------------------

class TestErrorTruncation:
    def test_pr_error_message_truncated(self, instance_dir, koan_root):
        """Long error messages should be truncated at 300 chars."""
        from app.check_runner import run_check

        long_error = "x" * 500
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata",
                    side_effect=RuntimeError(long_error)):
            _, msg = run_check(
                "https://github.com/o/r/pull/1",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            # The error in the message should be truncated
            assert len(msg) < 500
            assert "x" * 300 in msg
            assert "x" * 301 not in msg

    def test_issue_error_message_truncated(self, instance_dir, koan_root):
        from app.check_runner import run_check

        long_error = "y" * 500
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata",
                    side_effect=RuntimeError(long_error)):
            _, msg = run_check(
                "https://github.com/o/r/issues/1",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert "y" * 300 in msg
            assert "y" * 301 not in msg


# ---------------------------------------------------------------------------
# Default notify_fn
# ---------------------------------------------------------------------------

class TestDefaultNotify:
    def test_default_notify_fn_uses_send_telegram(self, instance_dir, koan_root):
        """When notify_fn=None, run_check should use send_telegram."""
        from app.check_runner import run_check

        pr_data = _pr_json(state="MERGED")
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.notify.send_telegram") as mock_tg:
            run_check(
                "https://github.com/o/r/pull/1",
                str(instance_dir), koan_root, notify_fn=None,
            )
            assert mock_tg.call_count >= 1


# ---------------------------------------------------------------------------
# Issue notification — result message
# ---------------------------------------------------------------------------

class TestIssueResultNotification:
    def test_updated_issue_sends_result_notification(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json()
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission"), \
             patch("app.utils.resolve_project_path", return_value="/home/proj"), \
             patch("app.utils.get_known_projects", return_value=[("proj", "/home/proj")]):
            run_check(
                "https://github.com/o/r/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            last_msg = notify.call_args_list[-1][0][0]
            assert "/plan queued" in last_msg.lower()

    def test_closed_issue_sends_result_notification(self, instance_dir, koan_root):
        from app.check_runner import run_check

        issue_data = _issue_json(state="closed")
        notify = MagicMock()
        with patch("app.check_runner._fetch_issue_metadata", return_value=issue_data), \
             patch("app.check_tracker.mark_checked"):
            run_check(
                "https://github.com/o/r/issues/42",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            last_msg = notify.call_args_list[-1][0][0]
            assert "closed" in last_msg.lower()


# ---------------------------------------------------------------------------
# PR status message formatting
# ---------------------------------------------------------------------------

class TestPrStatusFormatting:
    def test_pr_title_truncated_in_status_message(self, instance_dir, koan_root):
        """Long PR titles should be truncated at 60 chars in messages."""
        from app.check_runner import run_check

        long_title = "A" * 100
        pr_data = _pr_json(
            title=long_title,
            reviewDecision="APPROVED",
            mergeable="MERGEABLE",
        )
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.get_known_projects", return_value=[]):
            run_check(
                "https://github.com/o/r/pull/1",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            last_msg = notify.call_args_list[-1][0][0]
            assert "A" * 60 in last_msg
            assert "A" * 61 not in last_msg

    def test_pr_unknown_mergeable_reported(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(
            mergeable="UNKNOWN",
            reviewDecision="APPROVED",
        )
        notify = MagicMock()
        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.get_known_projects", return_value=[]):
            success, msg = run_check(
                "https://github.com/o/r/pull/1",
                str(instance_dir), koan_root, notify_fn=notify,
            )
            assert success
            assert "UNKNOWN" in msg


# ---------------------------------------------------------------------------
# CI recovery integration
# ---------------------------------------------------------------------------

class TestCIRecoveryIntegration:
    """Integration tests for CI recovery wiring in check_runner."""

    def test_failed_ci_on_koan_branch_dispatches_fix(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(headRefName="koan/my-feature", state="OPEN",
                           reviewDecision="APPROVED", mergeable="MERGEABLE")
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.check_runner._check_ci_recovery", return_value="dispatched") as mock_ci, \
             patch("app.utils.get_known_projects", return_value=[]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )
        assert success
        mock_ci.assert_called_once()
        assert "CI fix mission queued" in msg

    def test_failed_ci_at_max_retries_shows_escalation(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(headRefName="koan/my-feature", state="OPEN",
                           reviewDecision="APPROVED", mergeable="MERGEABLE")
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.check_runner._check_ci_recovery", return_value="escalated"), \
             patch("app.utils.get_known_projects", return_value=[]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )
        assert success
        assert "escalat" in msg.lower()

    def test_passing_ci_clears_status_on_merged(self, instance_dir, koan_root):
        from app.check_runner import run_check
        from app.check_tracker import set_ci_status, get_ci_status

        pr_url = "https://github.com/sukria/koan/pull/99"
        set_ci_status(instance_dir, pr_url, "fix_dispatched", 1)

        pr_data = _pr_json(state="MERGED")
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.mark_checked"):
            success, msg = run_check(
                pr_url, str(instance_dir), koan_root, notify_fn=notify,
            )
        assert success
        assert get_ci_status(instance_dir, pr_url) is None

    def test_non_koan_branch_skips_ci_check(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(headRefName="feature/my-feature", state="OPEN",
                           reviewDecision="APPROVED", mergeable="MERGEABLE")
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.check_runner._check_ci_recovery") as mock_ci, \
             patch("app.utils.get_known_projects", return_value=[]):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )
        mock_ci.assert_not_called()
