"""Tests for GitHub notification debug logging and communication layer messaging.

Verifies that:
1. Every notification received is logged at DEBUG level
2. Notifications skipped for various reasons are logged with the reason
3. When a notification creates a mission, send_telegram is called
"""

import logging
from unittest.mock import MagicMock, patch

import pytest


# --- Tests for fetch_unread_notifications debug logging ---


class TestFetchNotificationsLogging:
    """Verify debug logs in fetch_unread_notifications."""

    @patch("app.github_notifications.api")
    def test_logs_total_unread_count(self, mock_api, caplog):
        import json
        from app.github_notifications import fetch_unread_notifications

        notifications = [
            {"reason": "mention", "repository": {"full_name": "o/r"}},
            {"reason": "assign", "repository": {"full_name": "o/r"}},
            {"reason": "mention", "repository": {"full_name": "o/other"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            fetch_unread_notifications()

        assert "3 total unread notifications" in caplog.text

    @patch("app.github_notifications.api")
    def test_logs_drain_only_notifications(self, mock_api, caplog):
        import json
        from app.github_notifications import fetch_unread_notifications

        notifications = [
            {"reason": "assign", "repository": {"full_name": "o/r"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            result = fetch_unread_notifications()

        assert "drain-only" in caplog.text
        assert "assign=1" in caplog.text
        assert len(result.drain) == 1

    @patch("app.github_notifications.api")
    def test_logs_skipped_unknown_repo(self, mock_api, caplog):
        import json
        from app.github_notifications import fetch_unread_notifications

        notifications = [
            {"reason": "mention", "repository": {"full_name": "o/unknown"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            fetch_unread_notifications(known_repos={"o/known"})

        assert "unknown repos" in caplog.text
        assert "o/unknown" in caplog.text

    @patch("app.github_notifications.api")
    def test_logs_actionable_count_after_filtering(self, mock_api, caplog):
        import json
        from app.github_notifications import fetch_unread_notifications

        notifications = [
            {"reason": "mention", "repository": {"full_name": "o/r"}},
            {"reason": "assign", "repository": {"full_name": "o/r"}},
        ]
        mock_api.return_value = json.dumps(notifications)

        with caplog.at_level(logging.DEBUG, logger="app.github_notifications"):
            result = fetch_unread_notifications()

        assert len(result.actionable) == 1
        assert "1 actionable + 1 drain notification(s)" in caplog.text


# --- Tests for _fetch_and_filter_comment debug logging ---


class TestFetchAndFilterCommentLogging:
    """Verify debug logs in _fetch_and_filter_comment."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.is_notification_stale", return_value=True)
    def test_logs_stale_notification(self, mock_stale, mock_read, caplog):
        from app.github_command_handler import _fetch_and_filter_comment

        notif = {"id": "42"}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            result = _fetch_and_filter_comment(notif, "bot", 24)

        assert result is None
        assert "stale" in caplog.text
        assert "42" in caplog.text

    @patch("app.github_command_handler.find_mention_in_thread", return_value=None)
    @patch("app.github_command_handler.get_comment_from_notification", return_value=None)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    def test_logs_no_comment(self, mock_stale, mock_comment, mock_find, caplog):
        from app.github_command_handler import _fetch_and_filter_comment

        notif = {"id": "99", "repository": {"full_name": "o/r"}}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            result = _fetch_and_filter_comment(notif, "bot", 24)

        assert result is None
        assert "latest_comment_url failed" in caplog.text

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.find_mention_in_thread", return_value=None)
    @patch("app.github_command_handler.is_self_mention", return_value=True)
    @patch("app.github_command_handler.get_comment_from_notification", return_value={"id": "c1", "user": {"login": "bot"}})
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    def test_logs_self_mention(self, mock_stale, mock_comment, mock_self, mock_find, mock_read, caplog):
        from app.github_command_handler import _fetch_and_filter_comment

        notif = {"id": "77"}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            result = _fetch_and_filter_comment(notif, "bot", 24)

        assert result is None
        assert "self-authored" in caplog.text


# --- Tests for _validate_and_parse_command debug logging ---


class TestValidateAndParseLogging:
    """Verify debug logs in _validate_and_parse_command."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=True)
    def test_logs_already_processed(self, mock_processed, mock_read, caplog):
        from app.github_command_handler import _validate_and_parse_command

        notif = {"id": "1"}
        comment = {"id": "c100"}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            skill, cmd, ctx = _validate_and_parse_command(
                notif, comment, {}, MagicMock(), "bot", "o", "r"
            )

        assert skill is None
        assert "already processed" in caplog.text

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.parse_mention_command", return_value=None)
    @patch("app.github_command_handler.get_github_nickname", return_value="bot")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    def test_logs_no_valid_mention(self, mock_proc, mock_nick, mock_parse, mock_read, caplog):
        from app.github_command_handler import _validate_and_parse_command

        notif = {"id": "1"}
        comment = {"id": "c200", "body": "just a comment"}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            skill, cmd, ctx = _validate_and_parse_command(
                notif, comment, {}, MagicMock(), "bot", "o", "r"
            )

        assert skill is None
        assert "no valid @mention command" in caplog.text

    @patch("app.github_command_handler.validate_command", return_value=None)
    @patch("app.github_command_handler.parse_mention_command", return_value=("badcmd", ""))
    @patch("app.github_command_handler.get_github_nickname", return_value="bot")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    def test_logs_invalid_command(self, mock_proc, mock_nick, mock_parse, mock_validate, caplog):
        from app.github_command_handler import _validate_and_parse_command

        notif = {"id": "1"}
        comment = {"id": "c300", "body": "@bot badcmd"}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            skill, cmd, ctx = _validate_and_parse_command(
                notif, comment, {}, MagicMock(), "bot", "o", "r"
            )

        assert skill is None
        assert cmd == "badcmd"
        assert "not github-enabled" in caplog.text

    @patch("app.github_command_handler.validate_command")
    @patch("app.github_command_handler.parse_mention_command", return_value=("rebase", "some context"))
    @patch("app.github_command_handler.get_github_nickname", return_value="bot")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    def test_logs_parsed_command(self, mock_proc, mock_nick, mock_parse, mock_validate, caplog):
        from app.github_command_handler import _validate_and_parse_command

        mock_skill = MagicMock()
        mock_validate.return_value = mock_skill

        notif = {"id": "1"}
        comment = {"id": "c400", "body": "@bot rebase some context"}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            skill, cmd, ctx = _validate_and_parse_command(
                notif, comment, {}, MagicMock(), "bot", "o", "r"
            )

        assert skill is mock_skill
        assert "parsed command=rebase" in caplog.text
        assert "context=some context" in caplog.text


# --- Tests for process_single_notification logging ---


class TestProcessSingleNotificationLogging:
    """Verify debug logs in process_single_notification."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.resolve_project_from_notification", return_value=None)
    def test_logs_unknown_repo(self, mock_project, mock_stale, mock_comment, mock_read, caplog):
        from app.github_command_handler import process_single_notification

        mock_comment.return_value = {"id": "c1", "user": {"login": "alice"}, "body": "@bot rebase"}

        notif = {
            "id": "1",
            "repository": {"full_name": "unknown/repo"},
            "subject": {"url": ""},
        }
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            success, error = process_single_notification(
                notif, MagicMock(), {}, None, "bot", 24,
            )

        assert not success
        assert "not found in projects.yaml" in caplog.text

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_user_permission", return_value=False)
    @patch("app.github_command_handler.get_github_authorized_users", return_value=["allowed"])
    @patch("app.github_command_handler._validate_and_parse_command")
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.resolve_project_from_notification", return_value=("myproj", "o", "r"))
    def test_logs_permission_denied(
        self, mock_project, mock_stale, mock_comment, mock_validate, mock_auth, mock_perm, mock_read, caplog
    ):
        from app.github_command_handler import process_single_notification

        mock_comment.return_value = {"id": "c1", "user": {"login": "intruder"}, "body": "@bot rebase"}
        mock_validate.return_value = (MagicMock(), "rebase", "")

        notif = {"id": "1", "repository": {"full_name": "o/r"}, "subject": {"url": ""}}
        with caplog.at_level(logging.DEBUG, logger="app.github_command_handler"):
            success, error = process_single_notification(
                notif, MagicMock(), {}, None, "bot", 24,
            )

        assert not success
        assert "permission denied" in caplog.text
        assert "intruder" in caplog.text


# --- Tests for _log_notification ---


class TestLogNotification:
    """Verify _log_notification logs notification details."""

    def test_logs_notification_details(self, caplog):
        from app.loop_manager import _log_notification

        notif = {
            "repository": {"full_name": "owner/repo"},
            "subject": {"title": "Fix bug #42", "type": "PullRequest"},
            "updated_at": "2026-02-14T10:00:00Z",
        }
        with caplog.at_level(logging.DEBUG, logger="app.loop_manager"):
            _log_notification(notif)

        assert "owner/repo" in caplog.text
        assert "PullRequest" in caplog.text
        assert "Fix bug #42" in caplog.text

    def test_handles_missing_fields(self, caplog):
        from app.loop_manager import _log_notification

        notif = {}
        with caplog.at_level(logging.DEBUG, logger="app.loop_manager"):
            _log_notification(notif)

        # Should not raise, uses defaults
        assert "?" in caplog.text


# --- Tests for _notify_mission_from_mention ---


class TestNotifyMissionFromMention:
    """Verify send_telegram is called when a mission is created from a mention."""

    @patch("app.notify.send_telegram")
    def test_sends_telegram_on_mission(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        mock_send.return_value = True
        notif = {
            "repository": {"full_name": "owner/repo"},
            "subject": {"title": "Fix important bug", "type": "PullRequest"},
        }
        _notify_mission_from_mention(notif)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "owner/repo" in msg
        assert "Fix important bug" in msg
        assert "pullrequest" in msg

    @patch("app.notify.send_telegram")
    def test_includes_thread_url_from_subject(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        mock_send.return_value = True
        notif = {
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Fix important bug",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/owner/repo/pulls/99",
            },
        }
        _notify_mission_from_mention(notif)

        msg = mock_send.call_args[0][0]
        assert "https://github.com/owner/repo/pull/99" in msg

    @patch("app.notify.send_telegram")
    def test_issue_url_included(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        mock_send.return_value = True
        notif = {
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Bug report",
                "type": "Issue",
                "url": "https://api.github.com/repos/owner/repo/issues/7",
            },
        }
        _notify_mission_from_mention(notif)

        msg = mock_send.call_args[0][0]
        assert "https://github.com/owner/repo/issues/7" in msg

    @patch("app.notify.send_telegram", side_effect=OSError("network error"))
    def test_handles_send_failure_gracefully(self, mock_send, caplog):
        from app.loop_manager import _notify_mission_from_mention

        notif = {
            "repository": {"full_name": "o/r"},
            "subject": {"title": "t", "type": "Issue"},
        }
        with caplog.at_level(logging.DEBUG, logger="app.loop_manager"):
            _notify_mission_from_mention(notif)  # Should not raise

        assert "Failed to send" in caplog.text

    @patch("app.notify.send_telegram")
    def test_missing_fields_uses_defaults(self, mock_send):
        from app.loop_manager import _notify_mission_from_mention

        mock_send.return_value = True
        _notify_mission_from_mention({})

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "?" in msg


# --- Integration: process_github_notifications calls _notify_mission_from_mention ---


class TestProcessNotificationsIntegration:
    """Verify the full flow sends telegram when a mission is created."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def _make_fetch_result(self, actionable, drain=None):
        from app.github_notifications import FetchResult
        return FetchResult(actionable, drain or [])

    @patch("app.loop_manager._notify_mission_from_mention")
    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_notify_called_on_successful_mission(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, mock_notify, tmp_path
    ):
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        fake_notif = {
            "id": "1",
            "repository": {"full_name": "o/r"},
            "subject": {"url": "https://api.github.com/repos/o/r/issues/1", "title": "Bug", "type": "Issue"},
        }
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=self._make_fetch_result([fake_notif])), \
             patch("app.github_command_handler.process_single_notification", return_value=(True, None)):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 1
        mock_notify.assert_called_once_with(fake_notif)

    @patch("app.loop_manager._notify_mission_from_mention")
    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_notify_not_called_on_failed_mission(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, mock_notify, tmp_path
    ):
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        fake_notif = {
            "id": "1",
            "repository": {"full_name": "o/r"},
            "subject": {"url": "", "title": "PR", "type": "PullRequest"},
        }
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=self._make_fetch_result([fake_notif])), \
             patch("app.github_command_handler.process_single_notification", return_value=(False, "err")), \
             patch("app.loop_manager._post_error_for_notification"):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 0
        mock_notify.assert_not_called()

    @patch("app.loop_manager._notify_mission_from_mention")
    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_notify_called_for_each_successful_mission(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, mock_notify, tmp_path
    ):
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        notifs = [
            {"id": "1", "repository": {"full_name": "o/r"}, "subject": {"url": "", "title": "PR1", "type": "PR"}},
            {"id": "2", "repository": {"full_name": "o/r"}, "subject": {"url": "", "title": "PR2", "type": "PR"}},
            {"id": "3", "repository": {"full_name": "o/r"}, "subject": {"url": "", "title": "Issue", "type": "Issue"}},
        ]
        # First two succeed, third fails
        side_effects = [(True, None), (True, None), (False, None)]
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=self._make_fetch_result(notifs)), \
             patch("app.github_command_handler.process_single_notification", side_effect=side_effects):
            result = process_github_notifications(str(tmp_path), str(tmp_path))

        assert result == 2
        assert mock_notify.call_count == 2


# --- Tests for _log_notification in process_github_notifications ---


class TestProcessNotificationsDebugLogging:
    """Verify debug logs during notification processing."""

    def setup_method(self):
        from app.loop_manager import reset_github_backoff
        reset_github_backoff()

    def _make_fetch_result(self, actionable, drain=None):
        from app.github_notifications import FetchResult
        return FetchResult(actionable, drain or [])

    @patch("app.loop_manager._load_github_config")
    @patch("app.loop_manager._build_skill_registry")
    @patch("app.loop_manager._get_known_repos_from_projects")
    @patch("app.utils.load_config")
    def test_logs_fetched_count(
        self, mock_config, mock_repos, mock_registry, mock_gh_config, tmp_path, capsys
    ):
        from app.loop_manager import process_github_notifications

        mock_config.return_value = {}
        mock_gh_config.return_value = {"bot_username": "bot", "max_age": 300}
        mock_registry.return_value = MagicMock()
        mock_repos.return_value = set()

        notifs = [
            {"id": "1", "repository": {"full_name": "o/r"}, "subject": {"url": "", "title": "T", "type": "PR"}},
        ]
        with patch("app.projects_config.load_projects_config", return_value={}), \
             patch("app.github_notifications.fetch_unread_notifications", return_value=self._make_fetch_result(notifs)), \
             patch("app.github_command_handler.process_single_notification", return_value=(False, None)):
            process_github_notifications(str(tmp_path), str(tmp_path))

        captured = capsys.readouterr()
        assert "Fetched 1" in captured.out


class TestGetKnownReposFromProjects:
    """Unit tests for _get_known_repos_from_projects."""

    @patch("app.projects_config.load_projects_config")
    def test_includes_github_url_singular(self, mock_config, tmp_path):
        from app.loop_manager import _get_known_repos_from_projects

        mock_config.return_value = {
            "projects": {
                "myproj": {"github_url": "https://github.com/owner/repo"},
            },
        }
        repos = _get_known_repos_from_projects(str(tmp_path))
        assert "owner/repo" in repos

    @patch("app.projects_config.load_projects_config")
    def test_includes_github_urls_plural(self, mock_config, tmp_path):
        """github_urls (all remotes) must be included for fork workflows."""
        from app.loop_manager import _get_known_repos_from_projects

        mock_config.return_value = {
            "projects": {
                "myproj": {
                    "github_url": "https://github.com/fork-owner/repo",
                    "github_urls": [
                        "https://github.com/fork-owner/repo",
                        "https://github.com/upstream-owner/repo",
                    ],
                },
            },
        }
        repos = _get_known_repos_from_projects(str(tmp_path))
        assert "fork-owner/repo" in repos
        assert "upstream-owner/repo" in repos

    @patch("app.projects_config.load_projects_config")
    def test_handles_empty_github_urls(self, mock_config, tmp_path):
        from app.loop_manager import _get_known_repos_from_projects

        mock_config.return_value = {
            "projects": {
                "myproj": {
                    "github_url": "https://github.com/owner/repo",
                    "github_urls": [],
                },
            },
        }
        repos = _get_known_repos_from_projects(str(tmp_path))
        assert "owner/repo" in repos

    @patch("app.projects_config.load_projects_config")
    def test_handles_missing_github_urls_key(self, mock_config, tmp_path):
        from app.loop_manager import _get_known_repos_from_projects

        mock_config.return_value = {
            "projects": {
                "myproj": {"github_url": "https://github.com/owner/repo"},
            },
        }
        repos = _get_known_repos_from_projects(str(tmp_path))
        # Should not crash, just use github_url
        assert repos is not None
        assert "owner/repo" in repos
