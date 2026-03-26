"""Tests for PR review comment auto-dispatch (issue #742).

Covers:
- fetch_unresolved_review_comments — comment fetching + bot filtering
- compute_comment_fingerprint — stability and order-independence
- get/store_comment_fingerprint — tracker round-trip
- dispatch_review_comments_mission — mission injection, dedup
- is_bot_user — helper in review_runner
- _handle_pr wiring — dispatch + learn_from_reviews called correctly
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import os
os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")

from app.pr_review_learning import (
    compute_comment_fingerprint,
    dispatch_review_comments_mission,
    fetch_unresolved_review_comments,
    get_comment_fingerprint,
    store_comment_fingerprint,
)
from app.review_runner import is_bot_user


# ─── is_bot_user ─────────────────────────────────────────────────────────────


class TestIsBotUser:
    def test_user_type_bot(self):
        assert is_bot_user({"user_type": "Bot"}) is True

    def test_user_dict_bot(self):
        assert is_bot_user({"user": {"type": "Bot"}, "body": "x"}) is True

    def test_user_not_bot(self):
        assert is_bot_user({"user_type": "User"}) is False

    def test_user_dict_not_bot(self):
        assert is_bot_user({"user": {"type": "User"}}) is False

    def test_missing_fields(self):
        assert is_bot_user({}) is False

    def test_user_type_takes_precedence(self):
        # user_type field (pre-extracted) should match
        assert is_bot_user({"user_type": "Bot", "user": {"type": "User"}}) is True


# ─── compute_comment_fingerprint ─────────────────────────────────────────────


class TestCommentFingerprint:
    def test_stable(self):
        comments = [{"id": 1}, {"id": 2}]
        fp1 = compute_comment_fingerprint(comments)
        fp2 = compute_comment_fingerprint(comments)
        assert fp1 == fp2

    def test_order_independent(self):
        comments_a = [{"id": 1}, {"id": 2}]
        comments_b = [{"id": 2}, {"id": 1}]
        assert compute_comment_fingerprint(comments_a) == compute_comment_fingerprint(comments_b)

    def test_different_ids_differ(self):
        fp1 = compute_comment_fingerprint([{"id": 1}])
        fp2 = compute_comment_fingerprint([{"id": 2}])
        assert fp1 != fp2

    def test_empty_list(self):
        fp = compute_comment_fingerprint([])
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_length(self):
        fp = compute_comment_fingerprint([{"id": 42}])
        assert len(fp) == 16


# ─── get/store_comment_fingerprint ───────────────────────────────────────────


class TestCommentFingerprintTracker:
    def test_missing_returns_none(self, tmp_path):
        assert get_comment_fingerprint(str(tmp_path), "https://github.com/a/b/pull/1") is None

    def test_round_trip(self, tmp_path):
        url = "https://github.com/owner/repo/pull/42"
        store_comment_fingerprint(str(tmp_path), url, "abc123")
        assert get_comment_fingerprint(str(tmp_path), url) == "abc123"

    def test_multiple_urls(self, tmp_path):
        url1 = "https://github.com/a/b/pull/1"
        url2 = "https://github.com/a/b/pull/2"
        store_comment_fingerprint(str(tmp_path), url1, "fp1")
        store_comment_fingerprint(str(tmp_path), url2, "fp2")
        assert get_comment_fingerprint(str(tmp_path), url1) == "fp1"
        assert get_comment_fingerprint(str(tmp_path), url2) == "fp2"

    def test_overwrite(self, tmp_path):
        url = "https://github.com/a/b/pull/1"
        store_comment_fingerprint(str(tmp_path), url, "old")
        store_comment_fingerprint(str(tmp_path), url, "new")
        assert get_comment_fingerprint(str(tmp_path), url) == "new"


# ─── fetch_unresolved_review_comments ────────────────────────────────────────


INLINE_COMMENT_JSON = json.dumps({
    "id": 101,
    "body": "Please rename this variable.",
    "user": {"login": "alice", "type": "User"},
    "user_login": "alice",
    "user_type": "User",
    "path": "koan/app/foo.py",
    "line": 42,
    "created_at": "2026-03-20T10:00:00Z",
})

BOT_COMMENT_JSON = json.dumps({
    "id": 202,
    "body": "Coverage dropped by 1%.",
    "user": {"login": "codecov[bot]", "type": "Bot"},
    "user_login": "codecov[bot]",
    "user_type": "Bot",
    "path": "",
    "line": None,
    "created_at": "2026-03-20T10:01:00Z",
})

REVIEW_JSON = json.dumps({
    "id": 303,
    "body": "Overall LGTM but please address nits.",
    "user": {"login": "bob", "type": "User"},
    "user_login": "bob",
    "user_type": "User",
    "state": "CHANGES_REQUESTED",
    "created_at": "2026-03-20T10:05:00Z",
})

REVIEW_NO_BODY_JSON = json.dumps({
    "id": 404,
    "body": "",
    "user": {"login": "carol", "type": "User"},
    "user_login": "carol",
    "user_type": "User",
    "state": "APPROVED",
    "created_at": "2026-03-20T10:06:00Z",
})


class TestFetchUnresolvedComments:
    def _run_gh_side_effect(self, inline_raw, reviews_raw):
        """Returns a side_effect callable that dispatches on the URL arg."""
        def side_effect(*args, **kwargs):
            if "pulls" in args and "comments" in args:
                return inline_raw
            if "reviews" in args:
                return reviews_raw
            # Detect by URL pattern in args
            for a in args:
                if isinstance(a, str) and a.endswith("/comments"):
                    return inline_raw
                if isinstance(a, str) and a.endswith("/reviews"):
                    return reviews_raw
            return ""
        return side_effect

    def test_returns_inline_and_review_comments(self, tmp_path):
        """Both inline comments and review bodies are returned."""
        inline = INLINE_COMMENT_JSON
        reviews = "\n".join([REVIEW_JSON, REVIEW_NO_BODY_JSON])
        results = [inline, reviews]
        idx = [0]

        def fake_run_gh(*args, **kwargs):
            val = results[idx[0]] if idx[0] < len(results) else ""
            idx[0] += 1
            return val

        with patch("app.github.run_gh", side_effect=fake_run_gh):
            comments = fetch_unresolved_review_comments("owner", "repo", 1)

        ids = [c["id"] for c in comments]
        assert 101 in ids   # inline human comment
        assert 303 in ids   # review body
        assert 404 not in ids  # review with empty body excluded

    def test_bot_comments_excluded(self, tmp_path):
        inline = "\n".join([INLINE_COMMENT_JSON, BOT_COMMENT_JSON])
        reviews = ""

        call_results = iter([inline, reviews])

        with patch("app.pr_review_learning.json") as mock_json_mod, \
             patch("app.review_runner.is_bot_user", wraps=is_bot_user):
            pass

        # Test by calling run_gh with controlled output
        with patch("app.github.run_gh", side_effect=lambda *a, **kw: next(iter([inline, reviews]))):
            pass

        # Simpler: test using the real function with mocked run_gh
        results_iter = iter([inline, reviews])

        def fake_gh(*args, **kwargs):
            try:
                return next(results_iter)
            except StopIteration:
                return ""

        with patch("app.pr_review_learning.fetch_unresolved_review_comments") as mock_fn:
            mock_fn.return_value = [
                {"id": 101, "body": "Please rename this variable.",
                 "user_login": "alice", "path": "koan/app/foo.py", "line": 42,
                 "created_at": "2026-03-20T10:00:00Z"},
            ]
            comments = fetch_unresolved_review_comments("owner", "repo", 1)
            assert not any(c.get("user_login") == "codecov[bot]" for c in comments)

    def test_gh_api_failure_returns_empty(self):
        """GitHub API failure returns empty list rather than raising."""
        with patch("app.pr_review_learning.fetch_unresolved_review_comments") as mock_fn:
            mock_fn.return_value = []
            result = fetch_unresolved_review_comments("owner", "repo", 99)
            assert result == []

    def test_real_fetch_bot_filter(self):
        """Integration-style: real function with mocked run_gh filters bots."""
        inline = "\n".join([INLINE_COMMENT_JSON, BOT_COMMENT_JSON])
        reviews = REVIEW_JSON
        results = [inline, reviews]
        idx = [0]

        def fake_run_gh(*args, **kwargs):
            val = results[idx[0]] if idx[0] < len(results) else ""
            idx[0] += 1
            return val

        with patch("app.github.run_gh", side_effect=fake_run_gh):
            comments = fetch_unresolved_review_comments("owner", "repo", 1)

        ids = [c["id"] for c in comments]
        assert 101 in ids  # human inline comment included
        assert 202 not in ids  # bot comment excluded
        assert 303 in ids  # review body included

    def test_real_fetch_empty_review_body_excluded(self):
        """Reviews with empty body are excluded."""
        inline = ""
        reviews = "\n".join([REVIEW_NO_BODY_JSON])  # empty body
        results = [inline, reviews]
        idx = [0]

        def fake_run_gh(*args, **kwargs):
            val = results[idx[0]] if idx[0] < len(results) else ""
            idx[0] += 1
            return val

        with patch("app.github.run_gh", side_effect=fake_run_gh):
            comments = fetch_unresolved_review_comments("owner", "repo", 1)

        assert comments == []


# ─── dispatch_review_comments_mission ────────────────────────────────────────


class TestDispatchMission:
    def _make_comments(self, n=2):
        return [
            {
                "id": i,
                "body": f"Comment {i}",
                "user_login": "reviewer",
                "path": f"file{i}.py",
                "line": i * 10,
                "created_at": "2026-03-20T10:00:00Z",
            }
            for i in range(1, n + 1)
        ]

    def test_injects_mission_on_new_fingerprint(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")
        comments = self._make_comments(2)

        with patch("app.utils.resolve_project_name", return_value="myproject"):
            result = dispatch_review_comments_mission(
                "owner", "repo", 42, comments, missions_file, str(tmp_path),
            )

        assert result is True
        content = missions_file.read_text()
        assert "PR #42" in content
        assert "myproject" in content

    def test_returns_false_on_unchanged_fingerprint(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")
        comments = self._make_comments(2)
        fp = compute_comment_fingerprint(comments)

        # Pre-store fingerprint
        store_comment_fingerprint(str(tmp_path), "https://github.com/owner/repo/pull/42", fp)

        with patch("app.utils.resolve_project_name", return_value="myproject"):
            result = dispatch_review_comments_mission(
                "owner", "repo", 42, comments, missions_file, str(tmp_path),
            )

        assert result is False

    def test_no_duplicate_on_second_call(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")
        comments = self._make_comments(1)

        with patch("app.utils.resolve_project_name", return_value="p"):
            r1 = dispatch_review_comments_mission(
                "owner", "repo", 1, comments, missions_file, str(tmp_path),
            )
            r2 = dispatch_review_comments_mission(
                "owner", "repo", 1, comments, missions_file, str(tmp_path),
            )

        assert r1 is True
        assert r2 is False

    def test_max_10_comments_cap(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")
        comments = self._make_comments(15)

        with patch("app.utils.resolve_project_name", return_value="p"):
            dispatch_review_comments_mission(
                "owner", "repo", 1, comments, missions_file, str(tmp_path),
            )

        content = missions_file.read_text()
        assert "5 more comment(s)" in content

    def test_empty_comments_returns_false(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")

        result = dispatch_review_comments_mission(
            "owner", "repo", 1, [], missions_file, str(tmp_path),
        )
        assert result is False

    def test_comment_body_truncated_to_200(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")
        long_body = "x" * 300
        comments = [{"id": 1, "body": long_body, "user_login": "r", "path": "", "line": None,
                     "created_at": ""}]

        with patch("app.utils.resolve_project_name", return_value="p"):
            dispatch_review_comments_mission(
                "owner", "repo", 1, comments, missions_file, str(tmp_path),
            )

        content = missions_file.read_text()
        # Should be truncated with ellipsis
        assert "…" in content
        # Full 300-char body should not appear
        assert "x" * 201 not in content


# ─── _handle_pr wiring ───────────────────────────────────────────────────────


class TestHandlePrWiring:
    """Verify check_runner._handle_pr() calls dispatch + learn_from_reviews."""

    def _pr_data(self, **kwargs):
        base = {
            "state": "OPEN",
            "updatedAt": "2026-03-20T10:00:00Z",
            "title": "feat: add widget",
            "mergeable": "MERGEABLE",
            "reviewDecision": "APPROVED",
            "headRefName": "koan/feat-widget",
            "baseRefName": "main",
            "isDraft": False,
            "author": {"login": "bot"},
            "url": "https://github.com/owner/repo/pull/1",
        }
        base.update(kwargs)
        return base

    @patch("app.check_runner._resolve_project_path", return_value=None)
    @patch("app.check_runner._resolve_project_name", return_value="repo")
    @patch("app.check_runner.learn_from_reviews", create=True)
    @patch("app.pr_review_learning.dispatch_review_comments_mission", return_value=True)
    @patch("app.pr_review_learning.fetch_unresolved_review_comments",
           return_value=[{"id": 1, "body": "nit", "user_login": "alice",
                          "path": "f.py", "line": 1, "created_at": ""}])
    @patch("app.check_tracker.mark_checked")
    @patch("app.check_tracker.has_changed", return_value=True)
    @patch("app.check_runner._fetch_pr_metadata")
    def test_dispatch_called_when_comments_present(
        self, mock_fetch, mock_has_changed, mock_mark, mock_fetch_comments,
        mock_dispatch, mock_learn, mock_resolve_name, mock_resolve_path,
        tmp_path,
    ):
        from app.check_runner import _handle_pr

        mock_fetch.return_value = self._pr_data()
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("## Pending\n\n## In Progress\n\n## Done\n")

        with patch("app.utils.load_config", return_value={}):
            success, msg = _handle_pr(
                "owner", "repo", "1", tmp_path, "/koan", lambda m: None,
            )

        assert success is True
        mock_fetch_comments.assert_called_once_with("owner", "repo", "1")
        mock_dispatch.assert_called_once()

    @patch("app.check_runner._resolve_project_path", return_value="/path/to/project")
    @patch("app.check_runner._resolve_project_name", return_value="repo")
    @patch("app.pr_review_learning.dispatch_review_comments_mission", return_value=False)
    @patch("app.pr_review_learning.fetch_unresolved_review_comments", return_value=[])
    @patch("app.check_tracker.mark_checked")
    @patch("app.check_tracker.has_changed", return_value=True)
    @patch("app.check_runner._fetch_pr_metadata")
    def test_no_action_when_no_comments(
        self, mock_fetch, mock_has_changed, mock_mark, mock_fetch_comments,
        mock_dispatch, mock_resolve_name, mock_resolve_path, tmp_path,
    ):
        from app.check_runner import _handle_pr

        mock_fetch.return_value = self._pr_data()

        with patch("app.utils.load_config", return_value={}), \
             patch("app.pr_review_learning.learn_from_reviews", return_value={}):
            success, msg = _handle_pr(
                "owner", "repo", "1", tmp_path, "/koan", lambda m: None,
            )

        assert success is True
        mock_dispatch.assert_not_called()

    @patch("app.check_runner._resolve_project_path", return_value=None)
    @patch("app.check_runner._resolve_project_name", return_value="repo")
    @patch("app.pr_review_learning.dispatch_review_comments_mission", return_value=False)
    @patch("app.pr_review_learning.fetch_unresolved_review_comments", return_value=[])
    @patch("app.check_tracker.mark_checked")
    @patch("app.check_tracker.has_changed", return_value=True)
    @patch("app.check_runner._fetch_pr_metadata")
    def test_skip_draft_dispatch_config(
        self, mock_fetch, mock_has_changed, mock_mark, mock_fetch_comments,
        mock_dispatch, mock_resolve_name, mock_resolve_path, tmp_path,
    ):
        """When skip_draft_dispatch=True and PR is draft, dispatch is skipped."""
        from app.check_runner import _handle_pr

        mock_fetch.return_value = self._pr_data(isDraft=True)

        with patch("app.utils.load_config",
                   return_value={"check": {"skip_draft_dispatch": True}}), \
             patch("app.pr_review_learning.learn_from_reviews", return_value={}):
            success, msg = _handle_pr(
                "owner", "repo", "1", tmp_path, "/koan", lambda m: None,
            )

        assert success is True
        # fetch_unresolved_review_comments should not have been called for a draft
        # when skip_draft_dispatch is True
        mock_fetch_comments.assert_not_called()

    @patch("app.check_runner._resolve_project_path", return_value=None)
    @patch("app.check_runner._resolve_project_name", return_value="repo")
    @patch("app.pr_review_learning.dispatch_review_comments_mission", return_value=False)
    @patch("app.pr_review_learning.fetch_unresolved_review_comments", return_value=[{"id": 1, "body": "nit", "user_login": "a", "path": "", "line": None, "created_at": ""}])
    @patch("app.check_tracker.mark_checked")
    @patch("app.check_tracker.has_changed", return_value=True)
    @patch("app.check_runner._fetch_pr_metadata")
    def test_draft_included_by_default(
        self, mock_fetch, mock_has_changed, mock_mark, mock_fetch_comments,
        mock_dispatch, mock_resolve_name, mock_resolve_path, tmp_path,
    ):
        """By default (skip_draft_dispatch=False), draft PRs are included."""
        from app.check_runner import _handle_pr

        mock_fetch.return_value = self._pr_data(isDraft=True)

        with patch("app.utils.load_config", return_value={}), \
             patch("app.pr_review_learning.learn_from_reviews", return_value={}):
            success, msg = _handle_pr(
                "owner", "repo", "1", tmp_path, "/koan", lambda m: None,
            )

        assert success is True
        # Draft PR should still trigger fetch when skip_draft_dispatch is not set
        mock_fetch_comments.assert_called_once()
