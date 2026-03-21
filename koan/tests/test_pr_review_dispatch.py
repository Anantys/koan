"""Tests for PR review comment dispatch (issue #742).

Covers:
- fetch_unresolved_review_comments: returns human comments, filters bots
- compute_comment_fingerprint: stable, order-independent
- get/store_comment_fingerprint: round-trip persistence with file locking
- dispatch_review_comments_mission: inserts mission on new fingerprint,
  returns False on unchanged fingerprint
- is_bot_user helper in review_runner
- _handle_pr wiring in check_runner
"""

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


# ---------------------------------------------------------------------------
# is_bot_user (review_runner)
# ---------------------------------------------------------------------------

class TestIsBotUser:
    def setup_method(self):
        from app.review_runner import is_bot_user
        self.is_bot_user = is_bot_user

    def test_bot_via_user_type_field(self):
        assert self.is_bot_user({"user_type": "Bot"}) is True

    def test_bot_via_nested_user_dict(self):
        assert self.is_bot_user({"user": {"type": "Bot", "login": "codecov"}}) is True

    def test_human_user(self):
        assert self.is_bot_user({"user_type": "User"}) is False

    def test_empty_dict(self):
        assert self.is_bot_user({}) is False

    def test_human_nested(self):
        assert self.is_bot_user({"user": {"type": "User", "login": "alice"}}) is False


# ---------------------------------------------------------------------------
# compute_comment_fingerprint
# ---------------------------------------------------------------------------

class TestCommentFingerprint:
    def setup_method(self):
        from app.pr_review_learning import compute_comment_fingerprint
        self.fingerprint = compute_comment_fingerprint

    def test_stable_hex_string(self):
        comments = [{"id": 1}, {"id": 2}]
        fp = self.fingerprint(comments)
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_order_independent(self):
        comments_a = [{"id": 1}, {"id": 2}]
        comments_b = [{"id": 2}, {"id": 1}]
        assert self.fingerprint(comments_a) == self.fingerprint(comments_b)

    def test_different_ids_different_fingerprint(self):
        assert self.fingerprint([{"id": 1}]) != self.fingerprint([{"id": 99}])

    def test_empty_list(self):
        fp = self.fingerprint([])
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_deterministic(self):
        comments = [{"id": 42, "body": "ignored in fp"}]
        assert self.fingerprint(comments) == self.fingerprint(comments)


# ---------------------------------------------------------------------------
# get/store_comment_fingerprint
# ---------------------------------------------------------------------------

class TestCommentFingerprintStorage:
    def setup_method(self):
        from app.pr_review_learning import (
            get_comment_fingerprint,
            store_comment_fingerprint,
        )
        self.get_fp = get_comment_fingerprint
        self.store_fp = store_comment_fingerprint

    def test_returns_none_when_no_tracker(self, instance_dir):
        assert self.get_fp(str(instance_dir), "https://github.com/o/r/pull/1") is None

    def test_round_trip(self, instance_dir):
        url = "https://github.com/owner/repo/pull/7"
        self.store_fp(str(instance_dir), url, "abc123def456ef01")
        assert self.get_fp(str(instance_dir), url) == "abc123def456ef01"

    def test_multiple_prs_stored_independently(self, instance_dir):
        url_a = "https://github.com/o/r/pull/1"
        url_b = "https://github.com/o/r/pull/2"
        self.store_fp(str(instance_dir), url_a, "aaaa")
        self.store_fp(str(instance_dir), url_b, "bbbb")
        assert self.get_fp(str(instance_dir), url_a) == "aaaa"
        assert self.get_fp(str(instance_dir), url_b) == "bbbb"

    def test_update_overwrites_old_fingerprint(self, instance_dir):
        url = "https://github.com/o/r/pull/3"
        self.store_fp(str(instance_dir), url, "first")
        self.store_fp(str(instance_dir), url, "second")
        assert self.get_fp(str(instance_dir), url) == "second"


# ---------------------------------------------------------------------------
# fetch_unresolved_review_comments
# ---------------------------------------------------------------------------

def _inline_comment(id_, user, user_type="User", path="foo.py", body="Fix this"):
    return json.dumps({
        "id": id_,
        "body": body,
        "user_login": user,
        "user_type": user_type,
        "path": path,
        "line": 10,
        "created_at": "2026-03-01T12:00:00Z",
    })


def _review_comment(id_, user, user_type="User", body="Overall looks good"):
    return json.dumps({
        "id": id_,
        "body": body,
        "user_login": user,
        "user_type": user_type,
        "path": "",
        "line": None,
        "submitted_at": "2026-03-01T13:00:00Z",
    })


class TestFetchUnresolvedReviewComments:
    def setup_method(self):
        from app.pr_review_learning import fetch_unresolved_review_comments
        self.fetch = fetch_unresolved_review_comments

    def _run_gh_side_effect(self, inline_lines, review_lines):
        """Factory returning a side_effect for run_gh that returns pre-built JSON lines."""
        calls = iter(["\n".join(inline_lines), "\n".join(review_lines)])
        def side_effect(*args, **kwargs):
            return next(calls)
        return side_effect

    def test_returns_human_inline_comments(self):
        inline = [_inline_comment(1, "alice"), _inline_comment(2, "bob")]
        with patch("app.pr_review_learning.json") as _json, \
             patch("app.github.run_gh") as mock_gh, \
             patch("app.review_runner.is_bot_user", return_value=False):
            # Restore real json
            import json as real_json
            _json.loads = real_json.loads
            mock_gh.side_effect = self._run_gh_side_effect(inline, [])
            result = self.fetch("owner", "repo", 42)
        assert len(result) == 2
        assert result[0]["user_login"] == "alice"
        assert result[0]["path"] == "foo.py"

    def test_filters_bot_comments(self):
        inline = [
            _inline_comment(1, "alice", user_type="User"),
            _inline_comment(2, "codecov[bot]", user_type="Bot"),
        ]
        with patch("app.pr_review_learning.json") as _json, \
             patch("app.github.run_gh") as mock_gh:
            import json as real_json
            _json.loads = real_json.loads
            mock_gh.side_effect = self._run_gh_side_effect(inline, [])
            result = self.fetch("owner", "repo", 42)
        # Only the human comment should be returned
        assert all(c["user_login"] == "alice" for c in result)

    def test_returns_empty_on_gh_failure(self):
        with patch("app.pr_review_learning.json"), \
             patch("app.github.run_gh", side_effect=RuntimeError("no gh")):
            result = self.fetch("owner", "repo", 42)
        assert result == []

    def test_includes_review_level_comments(self):
        reviews = [_review_comment(99, "reviewer")]
        with patch("app.pr_review_learning.json") as _json, \
             patch("app.github.run_gh") as mock_gh, \
             patch("app.review_runner.is_bot_user", return_value=False):
            import json as real_json
            _json.loads = real_json.loads
            mock_gh.side_effect = self._run_gh_side_effect([], reviews)
            result = self.fetch("owner", "repo", 42)
        assert any(c["id"] == 99 for c in result)


# ---------------------------------------------------------------------------
# dispatch_review_comments_mission
# ---------------------------------------------------------------------------

class TestDispatchMission:
    def setup_method(self):
        from app.pr_review_learning import dispatch_review_comments_mission
        self.dispatch = dispatch_review_comments_mission

    def _make_comments(self, *ids):
        return [
            {"id": i, "body": f"Comment {i}", "user_login": "reviewer",
             "path": "src/foo.py", "line": i, "created_at": "2026-03-01T00:00:00Z"}
            for i in ids
        ]

    def test_inserts_mission_on_new_fingerprint(self, instance_dir):
        comments = self._make_comments(1, 2)
        missions_path = instance_dir / "missions.md"

        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]):
            result = self.dispatch(
                "owner", "repo", 42, comments,
                missions_path, str(instance_dir), project_name="myproject",
            )

        assert result is True
        content = missions_path.read_text()
        assert "Address review comments on PR #42" in content
        assert "myproject" in content

    def test_returns_false_on_unchanged_fingerprint(self, instance_dir):
        comments = self._make_comments(1, 2)
        missions_path = instance_dir / "missions.md"

        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]):
            # First dispatch — should succeed
            first = self.dispatch(
                "owner", "repo", 42, comments,
                missions_path, str(instance_dir), project_name="proj",
            )
            # Second dispatch with same comments — fingerprint unchanged
            second = self.dispatch(
                "owner", "repo", 42, comments,
                missions_path, str(instance_dir), project_name="proj",
            )

        assert first is True
        assert second is False

    def test_new_comment_triggers_new_mission(self, instance_dir):
        comments_v1 = self._make_comments(1, 2)
        comments_v2 = self._make_comments(1, 2, 3)
        missions_path = instance_dir / "missions.md"

        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]):
            first = self.dispatch(
                "owner", "repo", 42, comments_v1,
                missions_path, str(instance_dir), project_name="proj",
            )
            second = self.dispatch(
                "owner", "repo", 42, comments_v2,
                missions_path, str(instance_dir), project_name="proj",
            )

        assert first is True
        assert second is True

    def test_returns_false_on_empty_comments(self, instance_dir):
        missions_path = instance_dir / "missions.md"
        result = self.dispatch(
            "owner", "repo", 42, [],
            missions_path, str(instance_dir), project_name="proj",
        )
        assert result is False

    def test_mission_contains_pr_url(self, instance_dir):
        comments = self._make_comments(7)
        missions_path = instance_dir / "missions.md"

        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]):
            self.dispatch(
                "owner", "repo", 7, comments,
                missions_path, str(instance_dir), project_name="proj",
            )

        content = missions_path.read_text()
        assert "https://github.com/owner/repo/pull/7" in content

    def test_mission_caps_long_body(self, instance_dir):
        long_body = "A" * 300
        comments = [{"id": 1, "body": long_body, "user_login": "reviewer",
                      "path": "x.py", "line": 1, "created_at": ""}]
        missions_path = instance_dir / "missions.md"

        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]):
            self.dispatch(
                "owner", "repo", 1, comments,
                missions_path, str(instance_dir), project_name="proj",
            )

        content = missions_path.read_text()
        assert "…" in content
        assert long_body not in content

    def test_project_name_falls_back_to_repo(self, instance_dir):
        comments = self._make_comments(1)
        missions_path = instance_dir / "missions.md"

        with patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]):
            self.dispatch(
                "owner", "myrepo", 1, comments,
                missions_path, str(instance_dir),
            )

        content = missions_path.read_text()
        assert "[project:myrepo]" in content


# ---------------------------------------------------------------------------
# _handle_pr wiring in check_runner
# ---------------------------------------------------------------------------

def _pr_json(**overrides):
    base = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
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


class TestHandlePrWiring:
    def test_review_comment_mission_queued_when_comments_present(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="CHANGES_REQUESTED")
        comments = [{"id": 1, "body": "Fix this", "user_login": "reviewer",
                     "path": "foo.py", "line": 5, "created_at": ""}]
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   return_value=comments), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.learn_from_reviews"):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        assert success
        assert "Review comment mission queued" in msg
        content = (instance_dir / "missions.md").read_text()
        assert "Address review comments on PR #99" in content

    def test_no_mission_when_no_comments(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="APPROVED")
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   return_value=[]), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.learn_from_reviews"):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        assert success
        assert "Review comment mission queued" not in msg

    def test_duplicate_dispatch_not_repeated(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="CHANGES_REQUESTED")
        comments = [{"id": 5, "body": "same comment", "user_login": "bob",
                     "path": "", "line": None, "created_at": ""}]
        notify = MagicMock()

        common_patches = [
            patch("app.check_runner._fetch_pr_metadata", return_value=pr_data),
            patch("app.check_tracker.has_changed", return_value=True),
            patch("app.check_tracker.mark_checked"),
            patch("app.pr_review_learning.fetch_unresolved_review_comments",
                  return_value=comments),
            patch("app.utils.resolve_project_path", return_value=None),
            patch("app.utils.get_known_projects", return_value=[]),
            patch("app.pr_review_learning.learn_from_reviews"),
        ]

        # Apply all patches via context manager stack
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in common_patches:
                stack.enter_context(p)
            run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=MagicMock(),
            )
            run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=MagicMock(),
            )

        content = (instance_dir / "missions.md").read_text()
        # Mission text should appear exactly once
        assert content.count("Address review comments on PR #99") == 1

    def test_dispatch_skipped_when_rebase_needed(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(mergeable="CONFLICTING")
        notify = MagicMock()
        fetch_mock = MagicMock(return_value=[{"id": 1, "body": "x",
                                               "user_login": "r", "path": "", "line": None,
                                               "created_at": ""}])

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.utils.insert_pending_mission"), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments", fetch_mock), \
             patch("app.pr_review_learning.learn_from_reviews"):
            run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        # Dispatch is skipped when rebase is needed (needs_reb guard)
        fetch_mock.assert_not_called()

    def test_draft_pr_dispatch_included_by_default(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(isDraft=True, reviewDecision="")
        comments = [{"id": 3, "body": "comment on draft", "user_login": "alice",
                     "path": "a.py", "line": 1, "created_at": ""}]
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   return_value=comments), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.learn_from_reviews"), \
             patch("app.check_runner._get_review_dispatch_include_drafts",
                   return_value=True):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        assert success
        assert "Review comment mission queued" in msg

    def test_draft_pr_dispatch_excluded_when_configured(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(isDraft=True, reviewDecision="")
        comments = [{"id": 3, "body": "comment on draft", "user_login": "alice",
                     "path": "a.py", "line": 1, "created_at": ""}]
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   return_value=comments), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.learn_from_reviews"), \
             patch("app.check_runner._get_review_dispatch_include_drafts",
                   return_value=False):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        assert success
        assert "Review comment mission queued" not in msg

    def test_dispatch_exception_does_not_abort_check(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="APPROVED")
        notify = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   side_effect=RuntimeError("gh exploded")), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.learn_from_reviews"):
            success, msg = run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        assert success  # Check must succeed even when dispatch fails

    def test_learn_from_reviews_called_with_project_path(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="APPROVED")
        notify = MagicMock()
        learn_mock = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   return_value=[]), \
             patch("app.utils.resolve_project_path", return_value="/path/to/repo"), \
             patch("app.utils.get_known_projects",
                   return_value=[("koan", "/path/to/repo")]), \
             patch("app.pr_review_learning.learn_from_reviews", learn_mock):
            run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        learn_mock.assert_called_once()
        args = learn_mock.call_args[0]
        assert args[0] == str(instance_dir)   # instance_dir
        assert args[2] == "/path/to/repo"      # project_path

    def test_learn_from_reviews_skipped_without_project_path(self, instance_dir, koan_root):
        from app.check_runner import run_check

        pr_data = _pr_json(reviewDecision="APPROVED")
        notify = MagicMock()
        learn_mock = MagicMock()

        with patch("app.check_runner._fetch_pr_metadata", return_value=pr_data), \
             patch("app.check_tracker.has_changed", return_value=True), \
             patch("app.check_tracker.mark_checked"), \
             patch("app.pr_review_learning.fetch_unresolved_review_comments",
                   return_value=[]), \
             patch("app.utils.resolve_project_path", return_value=None), \
             patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.pr_review_learning.learn_from_reviews", learn_mock):
            run_check(
                "https://github.com/sukria/koan/pull/99",
                str(instance_dir), koan_root, notify_fn=notify,
            )

        learn_mock.assert_not_called()
