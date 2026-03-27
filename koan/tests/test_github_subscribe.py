"""Tests for GitHub thread subscription handling."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.github_command_handler import (
    _fetch_new_comments_since,
    _try_subscription_notification,
    process_single_notification,
)
from app.skills import Skill, SkillCommand, SkillRegistry

pytestmark = pytest.mark.slow


@pytest.fixture
def mock_skill():
    return Skill(
        name="rebase",
        scope="core",
        description="Rebase PR",
        github_enabled=True,
        github_context_aware=False,
        commands=[SkillCommand(name="rebase", aliases=["rb"])],
    )


@pytest.fixture
def registry(mock_skill):
    r = SkillRegistry()
    r._skills = {"core.rebase": mock_skill}
    r._command_map = {"rebase": mock_skill, "rb": mock_skill}
    return r


@pytest.fixture
def subscribe_config():
    return {
        "github": {
            "nickname": "koan-bot",
            "commands_enabled": True,
            "subscribe_enabled": True,
            "subscribe_max_per_cycle": 5,
            "authorized_users": ["*"],
        }
    }


@pytest.fixture
def sample_subscription_notification():
    return {
        "id": "99999",
        "reason": "subscribed",
        "updated_at": "2026-03-13T10:00:00Z",
        "repository": {"full_name": "sukria/koan"},
        "subject": {
            "type": "Issue",
            "url": "https://api.github.com/repos/sukria/koan/issues/42",
            "latest_comment_url": "https://api.github.com/repos/sukria/koan/issues/comments/500",
        },
    }


class TestFetchNewCommentsSince:
    def test_filters_bot_comments(self):
        comments_json = json.dumps([
            {"id": 1, "body": "user comment", "user_login": "alice"},
            {"id": 2, "body": "bot reply", "user_login": "koan-bot"},
            {"id": 3, "body": "another user", "user_login": "bob"},
        ])
        with patch("app.github.api", return_value=comments_json):
            result = _fetch_new_comments_since("o", "r", "1", None, "koan-bot")
        assert len(result) == 2
        assert result[0]["user_login"] == "alice"
        assert result[1]["user_login"] == "bob"

    def test_filters_by_since_id(self):
        comments_json = json.dumps([
            {"id": 10, "body": "old", "user_login": "alice"},
            {"id": 20, "body": "new", "user_login": "alice"},
        ])
        with patch("app.github.api", return_value=comments_json):
            result = _fetch_new_comments_since("o", "r", "1", 10, "koan-bot")
        assert len(result) == 1
        assert result[0]["id"] == 20

    def test_handles_api_error(self):
        with patch("app.github.api", side_effect=RuntimeError("API down")):
            result = _fetch_new_comments_since("o", "r", "1", None, "koan-bot")
        assert result == []


class TestTrySubscriptionNotification:
    def test_queues_reply_mission(self, sample_subscription_notification, subscribe_config, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        missions_path = instance_dir / "missions.md"
        missions_path.write_text("# Pending\n\n# In Progress\n\n# Done\n")

        new_comments = [{"id": 500, "body": "Can you check this?", "user_login": "alice"}]

        with patch.dict(os.environ, {"KOAN_ROOT": str(tmp_path)}), \
             patch("app.github_command_handler.resolve_project_from_notification",
                   return_value=("koan", "sukria", "koan")), \
             patch("app.github_command_handler._fetch_new_comments_since",
                   return_value=new_comments):
            result = _try_subscription_notification(
                sample_subscription_notification,
                subscribe_config,
                None,
                "koan-bot",
            )

        assert result is True
        content = missions_path.read_text()
        assert "/reply" in content
        assert "[project:koan]" in content

    def test_skips_when_subscribe_disabled(self, sample_subscription_notification):
        config = {"github": {"subscribe_enabled": False}}
        result = _try_subscription_notification(
            sample_subscription_notification, config, None, "koan-bot",
        )
        assert result is False

    def test_skips_non_subscription_reason(self, subscribe_config):
        notif = {"id": "1", "reason": "mention", "repository": {"full_name": "o/r"},
                 "subject": {"url": "https://api.github.com/repos/o/r/issues/1"}}
        result = _try_subscription_notification(notif, subscribe_config, None, "bot")
        assert result is False

    def test_skips_when_pending_mission_exists(self, sample_subscription_notification, subscribe_config, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        missions_path = instance_dir / "missions.md"
        missions_path.write_text("# Pending\n\n# In Progress\n\n# Done\n")

        from app.thread_subscriptions import set_pending_mission
        set_pending_mission(instance_dir, "sukria/koan#42", True)

        with patch.dict(os.environ, {"KOAN_ROOT": str(tmp_path)}), \
             patch("app.github_command_handler.resolve_project_from_notification",
                   return_value=("koan", "sukria", "koan")):
            result = _try_subscription_notification(
                sample_subscription_notification,
                subscribe_config,
                None,
                "koan-bot",
            )

        assert result is False

    def test_skips_when_no_new_comments(self, sample_subscription_notification, subscribe_config, tmp_path):
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        with patch.dict(os.environ, {"KOAN_ROOT": str(tmp_path)}), \
             patch("app.github_command_handler.resolve_project_from_notification",
                   return_value=("koan", "sukria", "koan")), \
             patch("app.github_command_handler._fetch_new_comments_since",
                   return_value=[]):
            result = _try_subscription_notification(
                sample_subscription_notification,
                subscribe_config,
                None,
                "koan-bot",
            )

        assert result is False


class TestSubscriptionInProcessNotification:
    """Test subscription path integration in process_single_notification."""

    def test_subscription_notification_queues_reply(
        self, sample_subscription_notification, subscribe_config, registry, tmp_path,
    ):
        """A subscribed notification with no @mention should queue /reply."""
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        missions_path = instance_dir / "missions.md"
        missions_path.write_text("# Pending\n\n# In Progress\n\n# Done\n")

        new_comments = [{"id": 500, "body": "What about this?", "user_login": "alice"}]

        with patch.dict(os.environ, {"KOAN_ROOT": str(tmp_path)}), \
             patch("app.github_command_handler._fetch_and_filter_comment", return_value=None), \
             patch("app.github_command_handler.resolve_project_from_notification",
                   return_value=("koan", "sukria", "koan")), \
             patch("app.github_command_handler._fetch_new_comments_since",
                   return_value=new_comments), \
             patch("app.github_command_handler.mark_notification_read"):
            success, error = process_single_notification(
                sample_subscription_notification,
                registry,
                subscribe_config,
                None,
                "koan-bot",
            )

        assert success is True
        assert error is None
        content = missions_path.read_text()
        assert "/reply" in content

    def test_mention_takes_priority_over_subscription(
        self, registry, subscribe_config, tmp_path,
    ):
        """When @mention is found, subscription path should not be triggered."""
        notif = {
            "id": "123",
            "reason": "subscribed",
            "updated_at": "2026-03-13T10:00:00Z",
            "repository": {"full_name": "sukria/koan"},
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
                "latest_comment_url": "https://api.github.com/repos/sukria/koan/issues/comments/100",
            },
        }
        comment = {
            "id": 100,
            "body": "@koan-bot rebase",
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/100",
            "user": {"login": "alice"},
        }

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        missions_path = instance_dir / "missions.md"
        missions_path.write_text("# Pending\n\n# In Progress\n\n# Done\n")

        with patch.dict(os.environ, {"KOAN_ROOT": str(tmp_path)}), \
             patch("app.github_command_handler._fetch_and_filter_comment", return_value=comment), \
             patch("app.github_command_handler.resolve_project_from_notification",
                   return_value=("koan", "sukria", "koan")), \
             patch("app.github_command_handler.check_already_processed", return_value=False), \
             patch("app.github_command_handler.parse_mention_command",
                   return_value=("rebase", "")), \
             patch("app.github_command_handler.check_user_permission", return_value=True), \
             patch("app.github_command_handler.add_reaction"), \
             patch("app.github_command_handler.mark_notification_read"), \
             patch("app.github_command_handler.api_url_to_web_url",
                   return_value="https://github.com/sukria/koan/pull/42"):
            success, error = process_single_notification(
                notif, registry, subscribe_config, None, "koan-bot",
            )

        assert success is True
        content = missions_path.read_text()
        assert "/rebase" in content
        # Should NOT have /reply — the @mention path handled it
        assert "/reply" not in content
