"""Tests for github_notifications.py — notification fetching, parsing, reactions."""

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.github import SSOAuthRequired
from app.github_notifications import (
    SSO_ESCALATION_THRESHOLD,
    FetchResult,
    _FETCH_FAILURE_THRESHOLD,
    _processed_comments,
    _reactions_endpoint,
    _search_comments_for_mention,
    add_reaction,
    api_url_to_web_url,
    check_already_processed,
    check_sso_escalation,
    check_user_permission,
    reset_consecutive_sso_state,
    extract_comment_metadata,
    fetch_unread_notifications,
    find_mention_in_thread,
    get_comment_from_notification,
    get_consecutive_sso_failures,
    get_fetch_failure_count,
    get_sso_failure_count,
    is_notification_stale,
    is_self_mention,
    parse_mention_command,
    reset_fetch_failure_count,
    reset_sso_failure_count,
    update_consecutive_sso_failures,
)


class TestParseMentionCommand:
    def test_simple_command(self):
        result = parse_mention_command("@bot rebase", "bot")
        assert result == ("rebase", "")

    def test_command_with_context(self):
        result = parse_mention_command("@bot implement phase 1 only", "bot")
        assert result == ("implement", "phase 1 only")

    def test_command_case_insensitive(self):
        result = parse_mention_command("@Bot REBASE", "Bot")
        assert result == ("rebase", "")

    def test_command_with_url(self):
        result = parse_mention_command(
            "@koan rebase https://github.com/owner/repo/pull/42", "koan"
        )
        assert result == ("rebase", "https://github.com/owner/repo/pull/42")

    def test_no_mention(self):
        assert parse_mention_command("just a comment", "bot") is None

    def test_mention_in_code_block(self):
        body = "```\n@bot rebase\n```"
        assert parse_mention_command(body, "bot") is None

    def test_mention_in_inline_code(self):
        body = "use `@bot rebase` to trigger"
        assert parse_mention_command(body, "bot") is None

    def test_empty_command(self):
        # @bot with nothing after it — no word to capture
        assert parse_mention_command("@bot ", "bot") is None

    def test_empty_body(self):
        assert parse_mention_command("", "bot") is None

    def test_empty_nickname(self):
        assert parse_mention_command("@bot rebase", "") is None

    def test_mention_with_surrounding_text(self):
        body = "Hey can you please @bot rebase this PR? Thanks!"
        result = parse_mention_command(body, "bot")
        assert result == ("rebase", "this PR? Thanks!")

    def test_multiple_mentions_first_wins(self):
        body = "@bot rebase\n@bot review"
        result = parse_mention_command(body, "bot")
        assert result == ("rebase", "")

    def test_command_with_slash_prefix(self):
        """Users often type @bot /command (Telegram habit) — slash must be stripped."""
        result = parse_mention_command("@bot /squash", "bot")
        assert result == ("squash", "")

    def test_command_with_slash_prefix_and_url(self):
        result = parse_mention_command(
            "@koan /squash https://github.com/owner/repo/pull/42", "koan"
        )
        assert result == ("squash", "https://github.com/owner/repo/pull/42")

    def test_command_with_slash_prefix_and_context(self):
        result = parse_mention_command("@bot /plan fix the login page", "bot")
        assert result == ("plan", "fix the login page")


class TestApiUrlToWebUrl:
    def test_pr_url(self):
        result = api_url_to_web_url(
            "https://api.github.com/repos/sukria/koan/pulls/42"
        )
        assert result == "https://github.com/sukria/koan/pull/42"

    def test_issue_url(self):
        result = api_url_to_web_url(
            "https://api.github.com/repos/owner/repo/issues/123"
        )
        assert result == "https://github.com/owner/repo/issues/123"

    def test_already_web_url(self):
        url = "https://github.com/owner/repo/pull/1"
        assert api_url_to_web_url(url) == url


class TestFetchUnreadNotifications:
    @patch("app.github_notifications.api")
    def test_returns_actionable_reasons(self, mock_api):
        notifications = [
            {"reason": "mention", "repository": {"full_name": "owner/repo"}},
            {"reason": "ci_activity", "repository": {"full_name": "owner/repo"}},
            {"reason": "mention", "repository": {"full_name": "other/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert isinstance(result, FetchResult)
        assert len(result.actionable) == 2
        assert all(n["reason"] == "mention" for n in result.actionable)

    @patch("app.github_notifications.api")
    def test_filters_by_known_repos(self, mock_api):
        """Non-mention reasons are filtered by known_repos; mentions bypass."""
        notifications = [
            {"reason": "comment", "repository": {"full_name": "owner/repo"}},
            {"reason": "comment", "repository": {"full_name": "other/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications(known_repos={"owner/repo"})
        assert len(result.actionable) == 1
        assert result.actionable[0]["repository"]["full_name"] == "owner/repo"

    @patch("app.github_notifications.api")
    def test_mention_bypasses_known_repos_filter(self, mock_api):
        """Direct @mentions pass through even when repo is not in known_repos."""
        notifications = [
            {"reason": "mention", "repository": {"full_name": "unknown/repo"}},
            {"reason": "comment", "repository": {"full_name": "unknown/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications(known_repos={"owner/repo"})
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "mention"

    @patch("app.github_notifications.api")
    def test_handles_api_error(self, mock_api):
        mock_api.side_effect = RuntimeError("API error")
        result = fetch_unread_notifications()
        assert isinstance(result, FetchResult)
        assert result.actionable == []
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_handles_empty_response(self, mock_api):
        mock_api.return_value = ""
        result = fetch_unread_notifications()
        assert isinstance(result, FetchResult)
        assert result.actionable == []
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_handles_invalid_json(self, mock_api):
        mock_api.return_value = "not json"
        result = fetch_unread_notifications()
        assert isinstance(result, FetchResult)
        assert result.actionable == []
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_author_reason_in_actionable(self, mock_api):
        """Notifications with reason=author should appear in actionable."""
        notifications = [
            {"reason": "author", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "author"
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_comment_reason_in_actionable(self, mock_api):
        """Notifications with reason=comment should appear in actionable."""
        notifications = [
            {"reason": "comment", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "comment"
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_ci_activity_in_drain(self, mock_api):
        """Notifications with reason=ci_activity should appear in drain."""
        notifications = [
            {"reason": "ci_activity", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert result.actionable == []
        assert len(result.drain) == 1
        assert result.drain[0]["reason"] == "ci_activity"

    @patch("app.github_notifications.api")
    def test_review_requested_in_actionable(self, mock_api):
        """Notifications with reason=review_requested should appear in actionable."""
        notifications = [
            {"reason": "review_requested", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "review_requested"
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_team_mention_in_actionable(self, mock_api):
        """Notifications with reason=team_mention should appear in actionable."""
        notifications = [
            {"reason": "team_mention", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "team_mention"
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_subscribed_in_actionable(self, mock_api):
        """Notifications with reason=subscribed should appear in actionable."""
        notifications = [
            {"reason": "subscribed", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "subscribed"
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_mixed_reasons_categorized_correctly(self, mock_api):
        """Mixed reasons are split between actionable and drain correctly."""
        notifications = [
            {"reason": "mention", "repository": {"full_name": "o/r"}},
            {"reason": "author", "repository": {"full_name": "o/r"}},
            {"reason": "comment", "repository": {"full_name": "o/r"}},
            {"reason": "review_requested", "repository": {"full_name": "o/r"}},
            {"reason": "subscribed", "repository": {"full_name": "o/r"}},
            {"reason": "team_mention", "repository": {"full_name": "o/r"}},
            {"reason": "ci_activity", "repository": {"full_name": "o/r"}},
            {"reason": "assign", "repository": {"full_name": "o/r"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications()
        assert len(result.actionable) == 6
        assert len(result.drain) == 2  # ci_activity, assign
        actionable_reasons = {n["reason"] for n in result.actionable}
        assert actionable_reasons == {
            "mention", "author", "comment",
            "review_requested", "subscribed", "team_mention",
        }

    @patch("app.github_notifications.api")
    def test_unknown_repo_non_mention_skipped(self, mock_api):
        """Non-mention notifications from unknown repos are skipped entirely."""
        notifications = [
            {"reason": "ci_activity", "repository": {"full_name": "unknown/repo"}},
            {"reason": "author", "repository": {"full_name": "unknown/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications(known_repos={"owner/repo"})
        assert result.actionable == []
        assert result.drain == []

    @patch("app.github_notifications.api")
    def test_unknown_repo_mention_passes(self, mock_api):
        """Mention notifications from unknown repos still pass through."""
        notifications = [
            {"reason": "mention", "repository": {"full_name": "unknown/repo"}},
            {"reason": "ci_activity", "repository": {"full_name": "unknown/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications(known_repos={"owner/repo"})
        assert len(result.actionable) == 1
        assert result.actionable[0]["reason"] == "mention"

    @patch("app.github_notifications.api")
    def test_since_parameter_passes_all_true(self, mock_api):
        """When since is provided, all=true is passed as query params in endpoint URL."""
        notifications = [
            {"reason": "mention", "repository": {"full_name": "owner/repo"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        result = fetch_unread_notifications(since="2026-03-08T17:00:00Z")
        assert len(result.actionable) == 1

        # Verify the API was called with since and all=true in the endpoint URL
        # (not as -f flags, which would cause gh to send POST instead of GET)
        call_args = mock_api.call_args
        endpoint = call_args[0][0]
        assert "since=2026-03-08T17:00:00Z" in endpoint
        assert "all=true" in endpoint

    @patch("app.github_notifications.api")
    def test_no_since_parameter_omits_all_true(self, mock_api):
        """Without since, all=true is NOT passed (only unread notifications)."""
        mock_api.return_value = json.dumps([])

        fetch_unread_notifications()

        call_args = mock_api.call_args
        endpoint = call_args[0][0]
        assert "all=true" not in endpoint
        assert endpoint == "notifications"

    @patch("app.github_notifications.api")
    def test_since_none_same_as_no_since(self, mock_api):
        """since=None should behave like no since parameter."""
        mock_api.return_value = json.dumps([])

        fetch_unread_notifications(since=None)

        call_args = mock_api.call_args
        endpoint = call_args[0][0]
        assert "all=true" not in endpoint
        assert endpoint == "notifications"


class TestCheckAlreadyProcessed:
    def setup_method(self):
        _processed_comments.clear()

    @patch("app.github_notifications.api")
    def test_in_memory_check(self, mock_api):
        _processed_comments.add("123")
        assert check_already_processed("123", "bot", "owner", "repo") is True
        mock_api.assert_not_called()

    @patch("app.github_notifications.api")
    def test_reaction_check_found(self, mock_api):
        reactions = [{"user": {"login": "bot"}, "content": "+1"}]
        mock_api.return_value = json.dumps(reactions)

        assert check_already_processed("456", "bot", "owner", "repo") is True
        assert "456" in _processed_comments

    @patch("app.github_notifications.api")
    def test_reaction_check_not_found(self, mock_api):
        reactions = [{"user": {"login": "other"}, "content": "+1"}]
        mock_api.return_value = json.dumps(reactions)

        assert check_already_processed("789", "bot", "owner", "repo") is False

    @patch("app.github_notifications.api")
    def test_eyes_reaction_detected(self, mock_api):
        """AI replies use 'eyes' reaction — should also be detected."""
        reactions = [{"user": {"login": "bot"}, "content": "eyes"}]
        mock_api.return_value = json.dumps(reactions)

        assert check_already_processed("500", "bot", "owner", "repo") is True
        assert "500" in _processed_comments

    @patch("app.github_notifications.api")
    def test_any_bot_reaction_detected(self, mock_api):
        """Any reaction from the bot marks the comment as processed."""
        reactions = [{"user": {"login": "bot"}, "content": "heart"}]
        mock_api.return_value = json.dumps(reactions)

        assert check_already_processed("501", "bot", "owner", "repo") is True

    @patch("app.github_notifications.api")
    def test_other_user_eyes_not_detected(self, mock_api):
        """Reactions from other users should not trigger dedup."""
        reactions = [{"user": {"login": "someone-else"}, "content": "eyes"}]
        mock_api.return_value = json.dumps(reactions)

        assert check_already_processed("502", "bot", "owner", "repo") is False

    @patch("app.github_notifications.api")
    def test_mixed_reactions_bot_detected(self, mock_api):
        """Bot reaction found among reactions from multiple users."""
        reactions = [
            {"user": {"login": "alice"}, "content": "+1"},
            {"user": {"login": "bot"}, "content": "eyes"},
            {"user": {"login": "bob"}, "content": "heart"},
        ]
        mock_api.return_value = json.dumps(reactions)

        assert check_already_processed("503", "bot", "owner", "repo") is True

    @patch("app.github_notifications.api")
    def test_api_error_returns_false(self, mock_api):
        mock_api.side_effect = RuntimeError("fail")
        assert check_already_processed("999", "bot", "owner", "repo") is False


class TestAddReaction:
    def setup_method(self):
        _processed_comments.clear()

    @patch("app.github_notifications.api")
    def test_success(self, mock_api):
        mock_api.return_value = ""
        assert add_reaction("owner", "repo", "123") is True
        assert "123" in _processed_comments

    @patch("app.github_notifications.api")
    def test_failure(self, mock_api):
        mock_api.side_effect = RuntimeError("fail")
        assert add_reaction("owner", "repo", "123") is False


class TestCheckUserPermission:
    @patch("app.github_notifications.api")
    def test_wildcard_with_write_access(self, mock_api):
        mock_api.return_value = json.dumps({"permission": "write"})
        assert check_user_permission("o", "r", "anyone", ["*"]) is True

    @patch("app.github_notifications.api")
    def test_wildcard_read_only_denied(self, mock_api):
        mock_api.return_value = json.dumps({"permission": "read"})
        assert check_user_permission("o", "r", "anyone", ["*"]) is False

    @patch("app.github_notifications.api")
    def test_not_in_allowlist(self, mock_api):
        assert check_user_permission("o", "r", "charlie", ["alice", "bob"]) is False
        mock_api.assert_not_called()

    @patch("app.github_notifications.api")
    def test_in_allowlist_with_write(self, mock_api):
        assert check_user_permission("o", "r", "alice", ["alice"]) is True
        mock_api.assert_not_called()

    @patch("app.github_notifications.api")
    def test_in_allowlist_no_api_call(self, mock_api):
        """Explicit user in allowlist returns True without any GitHub API call."""
        assert check_user_permission("o", "r", "bob", ["alice", "bob"]) is True
        mock_api.assert_not_called()


class TestIsNotificationStale:
    def test_fresh_notification(self):
        now = datetime.now(timezone.utc).isoformat()
        notif = {"updated_at": now}
        assert is_notification_stale(notif, max_age_hours=24) is False

    def test_stale_notification(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        notif = {"updated_at": old}
        assert is_notification_stale(notif, max_age_hours=24) is True

    def test_missing_timestamp(self):
        assert is_notification_stale({}) is True

    def test_invalid_timestamp(self):
        assert is_notification_stale({"updated_at": "not-a-date"}) is True


class TestIsSelfMention:
    def test_self_mention(self):
        comment = {"user": {"login": "koan-bot"}}
        assert is_self_mention(comment, "koan-bot") is True

    def test_not_self_mention(self):
        comment = {"user": {"login": "alice"}}
        assert is_self_mention(comment, "koan-bot") is False

    def test_missing_user(self):
        assert is_self_mention({}, "koan-bot") is False


class TestExtractCommentMetadata:
    def test_api_url(self):
        result = extract_comment_metadata(
            "https://api.github.com/repos/sukria/koan/issues/comments/123456"
        )
        assert result == ("sukria", "koan", "123456")

    def test_web_url_issue(self):
        result = extract_comment_metadata(
            "https://github.com/sukria/koan/issues/42#issuecomment-789"
        )
        assert result == ("sukria", "koan", "789")

    def test_web_url_pull(self):
        result = extract_comment_metadata(
            "https://github.com/owner/repo/pull/1#issuecomment-999"
        )
        assert result == ("owner", "repo", "999")

    def test_api_url_pulls_comments(self):
        """PR review comment API URLs use pulls/comments, not issues/comments."""
        result = extract_comment_metadata(
            "https://api.github.com/repos/sukria/koan/pulls/comments/456789"
        )
        assert result == ("sukria", "koan", "456789")

    def test_invalid_url(self):
        assert extract_comment_metadata("https://example.com/foo") is None

    def test_empty_string(self):
        assert extract_comment_metadata("") is None


class TestReactionsEndpoint:
    """Tests for _reactions_endpoint helper — correct endpoint for all comment types."""

    def test_issue_comment_url(self):
        url = "https://api.github.com/repos/owner/repo/issues/comments/123"
        result = _reactions_endpoint(comment_api_url=url)
        assert result == "repos/owner/repo/issues/comments/123/reactions"

    def test_pr_review_comment_url(self):
        url = "https://api.github.com/repos/owner/repo/pulls/comments/456"
        result = _reactions_endpoint(comment_api_url=url)
        assert result == "repos/owner/repo/pulls/comments/456/reactions"

    def test_commit_comment_url(self):
        url = "https://api.github.com/repos/owner/repo/comments/789"
        result = _reactions_endpoint(comment_api_url=url)
        assert result == "repos/owner/repo/comments/789/reactions"

    def test_fallback_without_url(self):
        result = _reactions_endpoint(owner="owner", repo="repo", comment_id="42")
        assert result == "repos/owner/repo/issues/comments/42/reactions"

    def test_fallback_with_empty_url(self):
        result = _reactions_endpoint(
            comment_api_url="", owner="o", repo="r", comment_id="1"
        )
        assert result == "repos/o/r/issues/comments/1/reactions"

    def test_non_github_url_falls_back(self):
        """Non-GitHub API URLs should trigger fallback."""
        result = _reactions_endpoint(
            comment_api_url="https://example.com/comments/5",
            owner="o", repo="r", comment_id="5",
        )
        assert result == "repos/o/r/issues/comments/5/reactions"


class TestCheckAlreadyProcessedWithUrl:
    """Tests for check_already_processed with comment_api_url parameter."""

    def setup_method(self):
        _processed_comments.clear()

    @patch("app.github_notifications.api")
    def test_pr_review_comment_uses_correct_endpoint(self, mock_api):
        """PR review comments should use pulls/comments endpoint."""
        reactions = [{"user": {"login": "bot"}, "content": "+1"}]
        mock_api.return_value = json.dumps(reactions)

        url = "https://api.github.com/repos/owner/repo/pulls/comments/42"
        result = check_already_processed("42", "bot", "owner", "repo",
                                          comment_api_url=url)
        assert result is True
        mock_api.assert_called_once_with(
            "repos/owner/repo/pulls/comments/42/reactions"
        )

    @patch("app.github_notifications.api")
    def test_issue_comment_uses_correct_endpoint(self, mock_api):
        """Issue comments should use issues/comments endpoint."""
        reactions = [{"user": {"login": "bot"}, "content": "eyes"}]
        mock_api.return_value = json.dumps(reactions)

        url = "https://api.github.com/repos/owner/repo/issues/comments/99"
        result = check_already_processed("99", "bot", "owner", "repo",
                                          comment_api_url=url)
        assert result is True
        mock_api.assert_called_once_with(
            "repos/owner/repo/issues/comments/99/reactions"
        )

    @patch("app.github_notifications.api")
    def test_fallback_without_url(self, mock_api):
        """Without comment_api_url, falls back to issues/comments."""
        mock_api.return_value = json.dumps([])

        check_already_processed("50", "bot", "owner", "repo")
        mock_api.assert_called_once_with(
            "repos/owner/repo/issues/comments/50/reactions"
        )


class TestAddReactionWithUrl:
    """Tests for add_reaction with comment_api_url parameter."""

    def setup_method(self):
        _processed_comments.clear()

    @patch("app.github_notifications.api")
    def test_pr_review_comment_reaction(self, mock_api):
        """Reaction on PR review comment uses pulls/comments endpoint."""
        mock_api.return_value = ""
        url = "https://api.github.com/repos/owner/repo/pulls/comments/77"
        result = add_reaction("owner", "repo", "77",
                              comment_api_url=url)
        assert result is True
        mock_api.assert_called_once_with(
            "repos/owner/repo/pulls/comments/77/reactions",
            method="POST",
            extra_args=["-f", "content=+1"],
        )

    @patch("app.github_notifications.api")
    def test_issue_comment_reaction_with_url(self, mock_api):
        """Issue comment reaction via URL uses correct endpoint."""
        mock_api.return_value = ""
        url = "https://api.github.com/repos/o/r/issues/comments/88"
        result = add_reaction("o", "r", "88", emoji="eyes",
                              comment_api_url=url)
        assert result is True
        mock_api.assert_called_once_with(
            "repos/o/r/issues/comments/88/reactions",
            method="POST",
            extra_args=["-f", "content=eyes"],
        )

    @patch("app.github_notifications.api")
    def test_fallback_without_url(self, mock_api):
        """Without URL, falls back to issues/comments endpoint."""
        mock_api.return_value = ""
        add_reaction("owner", "repo", "33")
        mock_api.assert_called_once_with(
            "repos/owner/repo/issues/comments/33/reactions",
            method="POST",
            extra_args=["-f", "content=+1"],
        )


class TestGetCommentFromNotification:
    """Tests for get_comment_from_notification."""

    @patch("app.github_notifications.api")
    def test_returns_comment_dict(self, mock_api):
        comment = {"id": 42, "body": "hello", "user": {"login": "alice"}}
        mock_api.return_value = json.dumps(comment)
        notif = {
            "subject": {
                "latest_comment_url": "https://api.github.com/repos/o/r/issues/comments/42",
            },
        }
        result = get_comment_from_notification(notif)
        assert result == comment

    @patch("app.github_notifications.api")
    def test_returns_none_on_runtime_error(self, mock_api):
        mock_api.side_effect = RuntimeError("API error")
        notif = {
            "subject": {
                "latest_comment_url": "https://api.github.com/repos/o/r/issues/comments/42",
            },
        }
        result = get_comment_from_notification(notif)
        assert result is None

    @patch("app.github_notifications.api")
    def test_returns_none_on_timeout(self, mock_api):
        """TimeoutExpired must be caught to avoid crashing the notification loop."""
        mock_api.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        notif = {
            "subject": {
                "latest_comment_url": "https://api.github.com/repos/o/r/issues/comments/42",
            },
        }
        result = get_comment_from_notification(notif)
        assert result is None

    @patch("app.github_notifications.api")
    def test_returns_none_on_invalid_json(self, mock_api):
        mock_api.return_value = "not json"
        notif = {
            "subject": {
                "latest_comment_url": "https://api.github.com/repos/o/r/issues/comments/42",
            },
        }
        result = get_comment_from_notification(notif)
        assert result is None

    def test_returns_none_on_missing_url(self):
        notif = {"subject": {}}
        result = get_comment_from_notification(notif)
        assert result is None

    def test_returns_none_on_non_api_url(self):
        notif = {
            "subject": {
                "latest_comment_url": "https://github.com/o/r/issues/1",
            },
        }
        result = get_comment_from_notification(notif)
        assert result is None


# ---------------------------------------------------------------------------
# find_mention_in_thread — fallback when latest_comment_url is stale
# ---------------------------------------------------------------------------


class TestFindMentionInThread:
    """Tests for find_mention_in_thread."""

    @pytest.fixture
    def notification(self):
        return {
            "subject": {
                "url": "https://api.github.com/repos/cpanel/Test-MockFile/pulls/208",
            },
        }

    @patch("app.github_notifications.check_already_processed", return_value=False)
    @patch("app.github_notifications.api")
    def test_finds_unprocessed_mention(self, mock_api, mock_processed, notification):
        """Should return the first unprocessed @mention by a non-bot user."""
        comments = [
            # Newest first (direction=desc)
            {"id": 300, "url": "u/300", "body": "my fix", "user": {"login": "Koan-Bot"}},
            {"id": 200, "url": "u/200", "body": "@Koan-Bot rebase", "user": {"login": "atoomic"}},
            {"id": 100, "url": "u/100", "body": "smoker failure output", "user": {"login": "atoomic"}},
        ]
        mock_api.return_value = json.dumps(comments)

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is not None
        assert result["id"] == 200

    @patch("app.github_notifications.check_already_processed", return_value=True)
    @patch("app.github_notifications.api")
    def test_skips_already_processed_mention(self, mock_api, mock_processed, notification):
        """Should skip mentions that already have a bot reaction."""
        comments = [
            {"id": 200, "url": "u/200", "body": "@Koan-Bot rebase", "user": {"login": "atoomic"}},
        ]
        mock_api.return_value = json.dumps(comments)

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is None

    @patch("app.github_notifications.api")
    def test_skips_bot_comments(self, mock_api, notification):
        """Should never return the bot's own comments."""
        comments = [
            {"id": 300, "url": "u/300", "body": "@Koan-Bot test", "user": {"login": "Koan-Bot"}},
        ]
        mock_api.return_value = json.dumps(comments)

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is None

    @patch("app.github_notifications.api")
    def test_returns_none_when_no_mentions(self, mock_api, notification):
        """Should return None when no comments mention the bot."""
        comments = [
            {"id": 100, "url": "u/100", "body": "just a regular comment", "user": {"login": "alice"}},
        ]
        mock_api.return_value = json.dumps(comments)

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is None

    @patch("app.github_notifications.api")
    def test_returns_none_on_api_error(self, mock_api, notification):
        """Should handle API errors gracefully."""
        mock_api.side_effect = RuntimeError("API error")

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is None

    def test_returns_none_on_missing_subject_url(self):
        """Should return None when notification has no subject URL."""
        result = find_mention_in_thread({"subject": {}}, "Koan-Bot")
        assert result is None

    def test_returns_none_on_invalid_subject_url(self):
        """Should return None when subject URL doesn't match expected pattern."""
        notif = {"subject": {"url": "https://example.com/not-github"}}
        result = find_mention_in_thread(notif, "Koan-Bot")
        assert result is None

    @patch("app.github_notifications.check_already_processed", return_value=False)
    @patch("app.github_notifications.api")
    def test_case_insensitive_mention_matching(self, mock_api, mock_processed, notification):
        """Should match @mentions case-insensitively."""
        comments = [
            {"id": 200, "url": "u/200", "body": "@koan-bot rebase", "user": {"login": "alice"}},
        ]
        mock_api.return_value = json.dumps(comments)

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is not None
        assert result["id"] == 200

    @patch("app.github_notifications.api")
    def test_handles_non_list_response(self, mock_api, notification):
        """Should handle unexpected API response type."""
        mock_api.return_value = json.dumps({"error": "not found"})

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is None

    @patch("app.github_notifications.api")
    def test_works_with_issues_url(self, mock_api):
        """Should work for issue threads — only issue comments searched (no PR review comments)."""
        notif = {
            "subject": {
                "url": "https://api.github.com/repos/owner/repo/issues/42",
            },
        }
        mock_api.return_value = json.dumps([])

        result = find_mention_in_thread(notif, "bot")
        assert result is None
        # Only issue comments endpoint called (no PR review comments for issues)
        assert mock_api.call_count == 1
        call_args = mock_api.call_args[0][0]
        assert "repos/owner/repo/issues/42/comments" in call_args

    @patch("app.github_notifications.check_already_processed", return_value=False)
    @patch("app.github_notifications.api")
    def test_searches_pr_review_comments(self, mock_api, mock_processed, notification):
        """Should search PR review comments when issue comments have no @mention."""
        issue_comments = [
            {"id": 100, "url": "u/100", "body": "just a fix", "user": {"login": "alice"}},
        ]
        review_comments = [
            {"id": 500, "url": "u/500", "body": "@Koan-Bot rebase", "user": {"login": "alice"}},
        ]
        # First call = issue comments (no mention), second = PR review comments (has mention)
        mock_api.side_effect = [json.dumps(issue_comments), json.dumps(review_comments)]

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is not None
        assert result["id"] == 500
        # Verify both endpoints were called
        assert mock_api.call_count == 2
        calls = [c[0][0] for c in mock_api.call_args_list]
        assert "issues/208/comments" in calls[0]
        assert "pulls/208/comments" in calls[1]

    @patch("app.github_notifications.check_already_processed", return_value=False)
    @patch("app.github_notifications.api")
    def test_issue_comments_checked_before_review_comments(self, mock_api, mock_processed, notification):
        """Should prefer issue comment @mention over PR review comment @mention."""
        issue_comments = [
            {"id": 200, "url": "u/200", "body": "@Koan-Bot fix", "user": {"login": "alice"}},
        ]
        # PR review comments should not even be fetched
        mock_api.return_value = json.dumps(issue_comments)

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is not None
        assert result["id"] == 200
        # Only issue comments endpoint called
        assert mock_api.call_count == 1

    @patch("app.github_notifications.api")
    def test_issue_url_skips_review_comments(self, mock_api):
        """Issues (not PRs) should not search PR review comments endpoint."""
        notif = {
            "subject": {
                "url": "https://api.github.com/repos/owner/repo/issues/10",
            },
        }
        mock_api.return_value = json.dumps([])

        find_mention_in_thread(notif, "bot")
        assert mock_api.call_count == 1
        assert "issues/10/comments" in mock_api.call_args[0][0]

    @patch("app.github_notifications.api")
    def test_pr_review_comments_api_error_handled(self, mock_api, notification):
        """API error on PR review comments should not crash, returns None."""
        mock_api.side_effect = [json.dumps([]), RuntimeError("API error")]

        result = find_mention_in_thread(notification, "Koan-Bot")
        assert result is None


class TestSearchCommentsForMention:
    """Tests for _search_comments_for_mention helper."""

    @patch("app.github_notifications.check_already_processed", return_value=False)
    def test_finds_mention(self, mock_processed):
        comments = [
            {"id": 1, "url": "u/1", "body": "@bot rebase", "user": {"login": "alice"}},
        ]
        result = _search_comments_for_mention(comments, "bot", "owner", "repo")
        assert result is not None
        assert result["id"] == 1

    def test_skips_bot_comments(self):
        comments = [
            {"id": 1, "url": "u/1", "body": "@bot rebase", "user": {"login": "bot"}},
        ]
        result = _search_comments_for_mention(comments, "bot", "owner", "repo")
        assert result is None

    @patch("app.github_notifications.check_already_processed", return_value=True)
    def test_skips_processed_comments(self, mock_processed):
        comments = [
            {"id": 1, "url": "u/1", "body": "@bot rebase", "user": {"login": "alice"}},
        ]
        result = _search_comments_for_mention(comments, "bot", "owner", "repo")
        assert result is None

    def test_skips_comments_without_mention(self):
        comments = [
            {"id": 1, "url": "u/1", "body": "just a comment", "user": {"login": "alice"}},
        ]
        result = _search_comments_for_mention(comments, "bot", "owner", "repo")
        assert result is None

    def test_empty_list(self):
        result = _search_comments_for_mention([], "bot", "owner", "repo")
        assert result is None

    @patch("app.github_notifications.check_already_processed", return_value=False)
    def test_case_insensitive(self, mock_processed):
        comments = [
            {"id": 1, "url": "u/1", "body": "@BOT rebase", "user": {"login": "alice"}},
        ]
        result = _search_comments_for_mention(comments, "bot", "owner", "repo")
        assert result is not None


# ---------------------------------------------------------------------------
# SSO failure tracking
# ---------------------------------------------------------------------------

class TestSSOFailureTracking:
    def setup_method(self):
        reset_sso_failure_count()
        reset_consecutive_sso_state()

    def teardown_method(self):
        reset_sso_failure_count()
        reset_consecutive_sso_state()

    @patch("app.github_notifications.api")
    def test_get_comment_sso_failure_records_count(self, mock_api):
        mock_api.side_effect = SSOAuthRequired("SAML enforcement")
        notif = {
            "subject": {
                "latest_comment_url": "https://api.github.com/repos/org/repo/issues/comments/1"
            }
        }
        result = get_comment_from_notification(notif)
        assert result is None
        assert get_sso_failure_count() == 1

    @patch("app.github_notifications.api")
    def test_find_mention_sso_failure_records_count(self, mock_api):
        mock_api.side_effect = SSOAuthRequired("SSO required")
        notif = {
            "subject": {
                "url": "https://api.github.com/repos/org/repo/issues/42"
            }
        }
        result = find_mention_in_thread(notif, "bot")
        assert result is None
        assert get_sso_failure_count() >= 1

    @patch("app.github_notifications.api")
    def test_check_already_processed_sso_failure(self, mock_api):
        mock_api.side_effect = SSOAuthRequired("SAML enforcement")
        result = check_already_processed("123", "bot", "org", "repo")
        assert result is False
        assert get_sso_failure_count() == 1

    @patch("app.github_notifications.api")
    def test_check_user_permission_sso_failure(self, mock_api):
        mock_api.side_effect = SSOAuthRequired("SSO required")
        result = check_user_permission("org", "repo", "alice", ["*"])
        assert result is False
        assert get_sso_failure_count() == 1

    def test_reset_clears_count(self):
        # Manually bump the count
        from app.github_notifications import _record_sso_failure
        _record_sso_failure("test")
        assert get_sso_failure_count() > 0
        reset_sso_failure_count()
        assert get_sso_failure_count() == 0

    @patch("app.github_notifications.api")
    def test_multiple_sso_failures_aggregate(self, mock_api):
        mock_api.side_effect = SSOAuthRequired("SAML enforcement")
        # Call multiple functions that each record SSO failures
        get_comment_from_notification({
            "subject": {"latest_comment_url": "https://api.github.com/repos/o/r/issues/comments/1"}
        })
        check_already_processed("123", "bot", "org", "repo")
        assert get_sso_failure_count() == 2


class TestConsecutiveFetchFailures:
    """Tests for fetch failure escalation: debug → warning after threshold."""

    def setup_method(self):
        reset_fetch_failure_count()

    def teardown_method(self):
        reset_fetch_failure_count()

    @patch("app.github_notifications.api")
    def test_single_failure_stays_debug(self, mock_api, caplog):
        """Below threshold, failures log at debug only."""
        mock_api.side_effect = RuntimeError("timeout")
        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            fetch_unread_notifications()

        assert get_fetch_failure_count() == 1
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    @patch("app.github_notifications.api")
    def test_threshold_triggers_warning(self, mock_api, caplog):
        """After _FETCH_FAILURE_THRESHOLD consecutive failures, log at WARNING."""
        mock_api.side_effect = RuntimeError("network error")
        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            for _ in range(_FETCH_FAILURE_THRESHOLD):
                fetch_unread_notifications()

        assert get_fetch_failure_count() == _FETCH_FAILURE_THRESHOLD
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "consecutive fetch failures" in warnings[0].message

    @patch("app.github_notifications._send_fetch_failure_alert")
    @patch("app.github_notifications.api")
    def test_outbox_alert_sent_once(self, mock_api, mock_alert):
        """Outbox alert fires once at threshold, not on subsequent failures."""
        mock_api.side_effect = RuntimeError("down")
        for _ in range(_FETCH_FAILURE_THRESHOLD + 2):
            fetch_unread_notifications()

        assert mock_alert.call_count == 1

    @patch("app.github_notifications._send_fetch_failure_alert")
    @patch("app.github_notifications.api")
    def test_success_resets_counter(self, mock_api, mock_alert):
        """A successful fetch resets the failure counter."""
        # Accumulate failures just below threshold
        mock_api.side_effect = RuntimeError("err")
        for _ in range(_FETCH_FAILURE_THRESHOLD - 1):
            fetch_unread_notifications()
        assert get_fetch_failure_count() == _FETCH_FAILURE_THRESHOLD - 1

        # Successful fetch resets
        mock_api.side_effect = None
        mock_api.return_value = json.dumps([])
        fetch_unread_notifications()
        assert get_fetch_failure_count() == 0
        mock_alert.assert_not_called()

    @patch("app.github_notifications._send_fetch_failure_alert")
    @patch("app.github_notifications.api")
    def test_recovery_after_alert_allows_new_alert(self, mock_api, mock_alert):
        """After recovery and new failure streak, a new alert can fire."""
        # First streak: hit threshold
        mock_api.side_effect = RuntimeError("err")
        for _ in range(_FETCH_FAILURE_THRESHOLD):
            fetch_unread_notifications()
        assert mock_alert.call_count == 1

        # Recover
        mock_api.side_effect = None
        mock_api.return_value = json.dumps([])
        fetch_unread_notifications()

        # Second streak
        mock_api.side_effect = RuntimeError("err again")
        for _ in range(_FETCH_FAILURE_THRESHOLD):
            fetch_unread_notifications()
        assert mock_alert.call_count == 2

    @patch("app.github_notifications.api")
    def test_empty_response_counts_as_failure(self, mock_api):
        """Empty API response increments the failure counter."""
        mock_api.return_value = ""
        fetch_unread_notifications()
        assert get_fetch_failure_count() == 1

    @patch("app.github_notifications.api")
    def test_invalid_json_counts_as_failure(self, mock_api):
        """Invalid JSON increments the failure counter."""
        mock_api.return_value = "not json"
        fetch_unread_notifications()
        assert get_fetch_failure_count() == 1

    @patch("app.github_notifications.api")
    def test_unexpected_type_counts_as_failure(self, mock_api):
        """Non-list JSON response increments the failure counter."""
        mock_api.return_value = json.dumps({"error": "bad"})
        fetch_unread_notifications()
        assert get_fetch_failure_count() == 1

    @patch("app.github_notifications.api")
    def test_send_fetch_failure_alert_writes_outbox(self, mock_api, tmp_path):
        """_send_fetch_failure_alert writes to outbox.md."""
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        outbox = instance_dir / "outbox.md"
        outbox.write_text("")

        with patch.dict(os.environ, {"KOAN_ROOT": str(tmp_path)}):
            from app.github_notifications import _send_fetch_failure_alert
            _send_fetch_failure_alert(3, "network error")

        content = outbox.read_text()
        assert "failed 3 times" in content
        assert "network error" in content


# ---------------------------------------------------------------------------
# Consecutive SSO failure tracking and escalation
# ---------------------------------------------------------------------------

class TestConsecutiveSSOFailures:
    def setup_method(self):
        reset_sso_failure_count()
        reset_consecutive_sso_state()

    def teardown_method(self):
        reset_sso_failure_count()
        reset_consecutive_sso_state()

    def test_consecutive_counter_accumulates_across_cycles(self):
        from app.github_notifications import _record_sso_failure

        # Cycle 1: 2 failures
        _record_sso_failure("a")
        _record_sso_failure("b")
        update_consecutive_sso_failures()
        assert get_consecutive_sso_failures() == 2

        # Cycle 2: reset per-cycle, add 1 more failure
        reset_sso_failure_count()
        _record_sso_failure("c")
        update_consecutive_sso_failures()
        assert get_consecutive_sso_failures() == 3

    def test_clean_cycle_resets_consecutive_counter(self):
        from app.github_notifications import _record_sso_failure

        # Build up failures
        _record_sso_failure("a")
        _record_sso_failure("b")
        update_consecutive_sso_failures()
        assert get_consecutive_sso_failures() == 2

        # Clean cycle
        reset_sso_failure_count()
        update_consecutive_sso_failures()
        assert get_consecutive_sso_failures() == 0

    def test_escalation_below_threshold_returns_false(self, monkeypatch):
        from app.github_notifications import _record_sso_failure
        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")

        # Only 2 failures, below threshold of 5
        _record_sso_failure("a")
        _record_sso_failure("b")
        update_consecutive_sso_failures()
        assert check_sso_escalation() is False

    def test_escalation_at_threshold_writes_outbox(self, monkeypatch):
        from app.github_notifications import _record_sso_failure
        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")

        # Accumulate SSO_ESCALATION_THRESHOLD failures
        for i in range(SSO_ESCALATION_THRESHOLD):
            _record_sso_failure(f"fail-{i}")
        update_consecutive_sso_failures()

        with patch("app.utils.append_to_outbox") as mock_outbox:
            result = check_sso_escalation()
            assert result is True
            mock_outbox.assert_called_once()
            msg = mock_outbox.call_args[0][1]
            assert "SSO" in msg
            assert "gh auth refresh" in msg
            assert str(SSO_ESCALATION_THRESHOLD) in msg

    def test_escalation_fires_only_once_per_streak(self, monkeypatch):
        from app.github_notifications import _record_sso_failure
        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")

        for i in range(SSO_ESCALATION_THRESHOLD):
            _record_sso_failure(f"fail-{i}")
        update_consecutive_sso_failures()

        with patch("app.utils.append_to_outbox") as mock_outbox:
            assert check_sso_escalation() is True
            assert check_sso_escalation() is False  # second call suppressed
            assert mock_outbox.call_count == 1

    def test_escalation_rearms_after_clean_cycle(self, monkeypatch):
        from app.github_notifications import _record_sso_failure
        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")

        # First streak
        for i in range(SSO_ESCALATION_THRESHOLD):
            _record_sso_failure(f"fail-{i}")
        update_consecutive_sso_failures()

        with patch("app.utils.append_to_outbox"):
            check_sso_escalation()

        # Clean cycle resets everything
        reset_sso_failure_count()
        update_consecutive_sso_failures()

        # New streak
        for i in range(SSO_ESCALATION_THRESHOLD):
            reset_sso_failure_count()
            _record_sso_failure(f"fail2-{i}")
            update_consecutive_sso_failures()

        with patch("app.utils.append_to_outbox") as mock_outbox:
            assert check_sso_escalation() is True
            mock_outbox.assert_called_once()

    def test_no_escalation_without_koan_root(self, monkeypatch):
        from app.github_notifications import _record_sso_failure
        monkeypatch.delenv("KOAN_ROOT", raising=False)

        for i in range(SSO_ESCALATION_THRESHOLD):
            _record_sso_failure(f"fail-{i}")
        update_consecutive_sso_failures()

        # KOAN_ROOT not set — should return False gracefully
        assert check_sso_escalation() is False
