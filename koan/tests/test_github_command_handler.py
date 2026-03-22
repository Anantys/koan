"""Tests for github_command_handler.py — notification-to-mission bridge."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.github_command_handler import (
    _error_replies,
    _reply_timestamps,
    _extract_url_from_context,
    _fetch_and_filter_comment,
    _handle_help_command,
    _notify_github_question,
    _notify_github_reply,
    _post_help_reply,
    _try_nlp_classification,
    _try_reply,
    _validate_and_parse_command,
    build_mission_from_command,
    extract_issue_number_from_notification,
    format_help_list_message,
    format_help_message,
    get_github_enabled_commands,
    get_github_enabled_commands_with_descriptions,
    post_error_reply,
    process_single_notification,
    resolve_project_from_notification,
    validate_command,
)
from app.skills import Skill, SkillCommand, SkillRegistry

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_skill():
    """A github-enabled skill."""
    return Skill(
        name="rebase",
        scope="core",
        description="Rebase PR",
        github_enabled=True,
        github_context_aware=False,
        commands=[SkillCommand(name="rebase", aliases=["rb"])],
    )


@pytest.fixture
def mock_context_skill():
    """A github-enabled, context-aware skill."""
    return Skill(
        name="implement",
        scope="core",
        description="Implement issue",
        github_enabled=True,
        github_context_aware=True,
        commands=[SkillCommand(name="implement", aliases=["impl"])],
    )


@pytest.fixture
def registry(mock_skill, mock_context_skill):
    """Registry with test skills."""
    reg = SkillRegistry()
    reg._register(mock_skill)
    reg._register(mock_context_skill)
    return reg


@pytest.fixture
def sample_notification():
    return {
        "id": "12345",
        "reason": "mention",
        "updated_at": "2026-02-11T20:00:00Z",
        "repository": {
            "full_name": "sukria/koan",
        },
        "subject": {
            "type": "PullRequest",
            "url": "https://api.github.com/repos/sukria/koan/pulls/42",
            "latest_comment_url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        },
    }


@pytest.fixture(autouse=True)
def clear_reply_rate_state():
    """Keep in-memory reply rate tracking isolated between tests."""
    _reply_timestamps.clear()
    yield
    _reply_timestamps.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidateCommand:
    def test_valid_github_enabled(self, registry):
        skill = validate_command("rebase", registry)
        assert skill is not None
        assert skill.name == "rebase"

    def test_valid_via_alias(self, registry):
        skill = validate_command("rb", registry)
        assert skill is not None
        assert skill.name == "rebase"

    def test_unknown_command(self, registry):
        assert validate_command("nonexistent", registry) is None

    def test_not_github_enabled(self):
        skill = Skill(
            name="status",
            scope="core",
            github_enabled=False,
            commands=[SkillCommand(name="status")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        assert validate_command("status", reg) is None


class TestGetGithubEnabledCommands:
    def test_lists_enabled_commands(self, registry):
        commands = get_github_enabled_commands(registry)
        assert "rebase" in commands
        assert "implement" in commands

    def test_excludes_disabled(self):
        skill = Skill(
            name="status", scope="core", github_enabled=False,
            commands=[SkillCommand(name="status")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        assert get_github_enabled_commands(reg) == []

    def test_sorted(self, registry):
        commands = get_github_enabled_commands(registry)
        assert commands == sorted(commands)


class TestGetGithubEnabledCommandsWithDescriptions:
    def test_returns_name_and_description(self, registry):
        result = get_github_enabled_commands_with_descriptions(registry)
        names = [name for name, _ in result]
        assert "rebase" in names
        assert "implement" in names

    def test_uses_command_description(self):
        skill = Skill(
            name="review", scope="core", github_enabled=True,
            description="Skill description",
            commands=[SkillCommand(name="review", description="Review a PR")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        result = get_github_enabled_commands_with_descriptions(reg)
        assert result == [("review", "Review a PR")]

    def test_falls_back_to_skill_description(self):
        skill = Skill(
            name="review", scope="core", github_enabled=True,
            description="Skill-level description",
            commands=[SkillCommand(name="review", description="")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        result = get_github_enabled_commands_with_descriptions(reg)
        assert result == [("review", "Skill-level description")]

    def test_excludes_disabled(self):
        skill = Skill(
            name="status", scope="core", github_enabled=False,
            commands=[SkillCommand(name="status", description="Status check")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        assert get_github_enabled_commands_with_descriptions(reg) == []

    def test_sorted(self, registry):
        result = get_github_enabled_commands_with_descriptions(registry)
        names = [name for name, _ in result]
        assert names == sorted(names)

    def test_deduplicates_command_names(self):
        """Two skills with same command name — first one wins."""
        skill1 = Skill(
            name="review", scope="core", github_enabled=True,
            commands=[SkillCommand(name="review", description="First")],
        )
        skill2 = Skill(
            name="review2", scope="custom", github_enabled=True,
            commands=[SkillCommand(name="review", description="Second")],
        )
        reg = SkillRegistry()
        reg._register(skill1)
        reg._register(skill2)
        result = get_github_enabled_commands_with_descriptions(reg)
        assert len(result) == 1
        assert result[0] == ("review", "First")


class TestFormatHelpMessage:
    def test_contains_invalid_command(self, registry):
        msg = format_help_message("badcmd", registry, "koanbot")
        assert "`badcmd`" in msg

    def test_lists_available_commands(self, registry):
        msg = format_help_message("badcmd", registry, "koanbot")
        assert "`@koanbot rebase`" in msg
        assert "`@koanbot implement`" in msg

    def test_includes_usage_line(self, registry):
        msg = format_help_message("badcmd", registry, "koanbot")
        assert "Usage:" in msg
        assert "`@koanbot <command>`" in msg

    def test_includes_descriptions(self):
        skill = Skill(
            name="rebase", scope="core", github_enabled=True,
            commands=[SkillCommand(name="rebase", description="Rebase a PR")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        msg = format_help_message("badcmd", reg, "koanbot")
        assert "Rebase a PR" in msg

    def test_empty_registry(self):
        reg = SkillRegistry()
        msg = format_help_message("badcmd", reg, "koanbot")
        assert "`badcmd`" in msg
        assert "Usage:" in msg

    def test_suggests_closest_command(self):
        skill = Skill(
            name="rebase", scope="core", github_enabled=True,
            commands=[SkillCommand(name="rebase", description="Rebase a PR")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        msg = format_help_message("rebas", reg, "koanbot")
        assert "Did you mean `rebase`?" in msg

    def test_no_suggestion_for_unrelated_command(self):
        skill = Skill(
            name="rebase", scope="core", github_enabled=True,
            commands=[SkillCommand(name="rebase", description="Rebase a PR")],
        )
        reg = SkillRegistry()
        reg._register(skill)
        msg = format_help_message("xyzzy", reg, "koanbot")
        assert "Did you mean" not in msg


class TestBuildMissionFromCommand:
    def test_simple_command(self, mock_skill, sample_notification):
        mission = build_mission_from_command(
            mock_skill, "rebase", "", sample_notification, "koan"
        )
        assert mission == "- [project:koan] /rebase https://github.com/sukria/koan/pull/42 📬"

    def test_context_aware_with_context(self, mock_context_skill, sample_notification):
        mission = build_mission_from_command(
            mock_context_skill, "implement", "phase 1 only",
            sample_notification, "koan",
        )
        assert "phase 1 only" in mission
        assert "/implement" in mission

    def test_context_ignored_when_not_aware(self, mock_skill, sample_notification):
        mission = build_mission_from_command(
            mock_skill, "rebase", "extra context", sample_notification, "koan"
        )
        assert "extra context" not in mission

    def test_url_in_context_overrides(self, mock_context_skill, sample_notification):
        context = "https://github.com/other/repo/issues/99 phase 2"
        mission = build_mission_from_command(
            mock_context_skill, "implement", context, sample_notification, "koan"
        )
        assert "https://github.com/other/repo/issues/99" in mission

    def test_no_subject_url(self, mock_skill):
        notif = {"subject": {}}
        mission = build_mission_from_command(
            mock_skill, "rebase", "", notif, "myproject"
        )
        assert mission == "- [project:myproject] /rebase 📬"


class TestResolveProjectFromNotification:
    @patch("app.utils.resolve_project_path", return_value="/path/to/koan")
    @patch("app.utils.project_name_for_path", return_value="koan")
    def test_known_repo(self, mock_name, mock_resolve, sample_notification):
        result = resolve_project_from_notification(sample_notification)
        assert result == ("koan", "sukria", "koan")

    @patch("app.utils.resolve_project_path", return_value=None)
    def test_unknown_repo(self, mock_resolve, sample_notification):
        assert resolve_project_from_notification(sample_notification) is None

    def test_missing_repository(self):
        assert resolve_project_from_notification({}) is None


class TestExtractIssueNumber:
    def test_issue_url(self):
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/issues/42"}}
        assert extract_issue_number_from_notification(notif) == "42"

    def test_pr_url(self):
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/pulls/99"}}
        assert extract_issue_number_from_notification(notif) == "99"

    def test_missing_url(self):
        assert extract_issue_number_from_notification({}) is None


class TestPostErrorReply:
    def setup_method(self):
        _error_replies.clear()

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_posts_error(self, mock_api, mock_react):
        mock_api.return_value = ""
        result = post_error_reply("owner", "repo", "42", "123", "Test error")
        assert result is True
        mock_api.assert_called_once()

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_deduplication(self, mock_api, mock_react):
        mock_api.return_value = ""
        post_error_reply("owner", "repo", "42", "123", "Test error")
        result = post_error_reply("owner", "repo", "42", "123", "Test error")
        assert result is False  # Duplicate
        assert mock_api.call_count == 1

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_different_errors_not_deduplicated(self, mock_api, mock_react):
        mock_api.return_value = ""
        post_error_reply("owner", "repo", "42", "123", "Error A")
        post_error_reply("owner", "repo", "42", "123", "Error B")
        assert mock_api.call_count == 2

    @patch("app.github_command_handler.add_reaction", side_effect=RuntimeError("reaction failed"))
    @patch("app.github.api")
    def test_reaction_failure_allows_retry(self, mock_api, mock_react):
        """If add_reaction fails, the error should NOT be marked as sent."""
        mock_api.return_value = ""
        result = post_error_reply("owner", "repo", "42", "123", "Test error")
        # The whole try block catches RuntimeError, so returns False
        assert result is False
        # The error key should NOT be in dedup cache, allowing retry
        assert "123:Test error" not in _error_replies

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_successful_post_marked_as_sent(self, mock_api, mock_react):
        """After both comment and reaction succeed, error is marked as sent."""
        mock_api.return_value = ""
        result = post_error_reply("owner", "repo", "42", "123", "Test error")
        assert result is True
        assert "123:Test error" in _error_replies

    @patch("app.github_command_handler.add_reaction", return_value=False)
    @patch("app.github.api")
    def test_reaction_false_allows_retry(self, mock_api, mock_react):
        """If add_reaction returns False (not exception), error key must NOT
        be added to dedup cache — allowing retry on next poll."""
        mock_api.return_value = ""
        result = post_error_reply("owner", "repo", "42", "123", "Test error")
        assert result is True  # Comment was posted successfully
        # Reaction failed silently — error should NOT be in dedup cache
        assert "123:Test error" not in _error_replies
        # Second call should attempt to post again (not deduplicated)
        result2 = post_error_reply("owner", "repo", "42", "123", "Test error")
        assert result2 is True
        assert mock_api.call_count == 2


class TestProcessSingleNotification:
    """Integration-style tests for the full notification processing pipeline."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_happy_path(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }

        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        assert error is None
        mock_insert.assert_called_once()
        mock_react.assert_called_once()

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.is_notification_stale", return_value=True)
    def test_stale_notification_skipped(self, mock_stale, mock_read, registry, sample_notification):
        success, error = process_single_notification(
            sample_notification, registry, {}, None, "bot",
        )
        assert success is False
        assert error is None

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification", return_value=None)
    def test_no_comment_skipped(self, mock_comment, mock_stale, mock_read, registry, sample_notification):
        success, error = process_single_notification(
            sample_notification, registry, {}, None, "bot",
        )
        assert success is False

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification", return_value=None)
    def test_unknown_repo_error(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_read, registry, sample_notification,
    ):
        mock_comment.return_value = {
            "id": 99999, "body": "@bot rebase", "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "bot"}}

        success, error = process_single_notification(
            sample_notification, registry, config, None, "bot",
        )
        assert success is False
        assert "Unknown repository" in error
        # Notification must be marked as read to prevent re-processing loop
        mock_read.assert_called_once_with("12345")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_invalid_command_returns_help(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_read, registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99999, "body": "@testbot badcmd",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot"}}

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        assert success is False
        assert "`badcmd`" in error
        assert "`@testbot rebase`" in error
        assert "`@testbot implement`" in error
        assert "Usage:" in error

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_invalid_command_marks_notification_read(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_read, registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99999, "body": "@testbot badcmd",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot"}}

        process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        # Notification should be marked as read for invalid commands
        mock_read.assert_called_with("12345")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_user_permission", return_value=False)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_permission_denied_returns_error(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_perm, mock_read, registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99999, "body": "@testbot rebase",
            "user": {"login": "eve"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["alice"]}}

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        assert success is False
        assert "Permission denied" in error

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_user_permission", return_value=False)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_permission_denied_marks_notification_read(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_perm, mock_read, registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99999, "body": "@testbot rebase",
            "user": {"login": "eve"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["alice"]}}

        process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        # Permission-denied must mark notification as read to prevent
        # repeated processing on every poll cycle
        mock_read.assert_called_with("12345")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission", side_effect=OSError("disk full"))
    def test_insert_mission_failure_marks_read(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """When insert_pending_mission fails, notification must still be marked
        as read to prevent infinite re-processing."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is False
        assert "Failed to queue mission" in error
        mock_read.assert_called_with("12345")
        # Reaction should NOT have been added (mission wasn't persisted)
        mock_react.assert_not_called()

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission", side_effect=PermissionError("read-only"))
    def test_insert_mission_permission_error_handled(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """PermissionError (subclass of OSError) is also caught gracefully."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is False
        assert "read-only" in error
        mock_read.assert_called_with("12345")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_empty_koan_root_rejects_mission(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification,
    ):
        """Empty KOAN_ROOT should fail early, not write to relative path."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": ""}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is False
        assert "KOAN_ROOT" in error
        mock_insert.assert_not_called()
        mock_read.assert_called_with("12345")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_missing_koan_root_rejects_mission(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, monkeypatch,
    ):
        """Unset KOAN_ROOT should fail early, not write to relative path."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        monkeypatch.delenv("KOAN_ROOT", raising=False)
        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )

        assert success is False
        assert "KOAN_ROOT" in error
        mock_insert.assert_not_called()


class TestTryReply:
    """Tests for the _try_reply helper that generates AI replies."""

    @pytest.fixture
    def reply_notification(self):
        return {
            "id": "77777",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/issues/42",
            },
            "repository": {"full_name": "sukria/koan"},
        }

    @pytest.fixture
    def reply_comment(self):
        return {
            "id": 55555,
            "body": "@bot what do you think about this?",
            "user": {"login": "alice"},
        }

    @pytest.fixture
    def reply_config(self):
        return {
            "github": {
                "nickname": "bot",
                "reply_enabled": True,
                "authorized_users": ["*"],
            }
        }

    def test_disabled_returns_false(self, reply_notification, reply_comment):
        config = {"github": {"reply_enabled": False}}
        result = _try_reply(
            reply_notification, reply_comment, config, None,
            "bot", "sukria", "koan", "koan", "what?",
        )
        assert result is False

    def test_missing_config_returns_false(self, reply_notification, reply_comment):
        result = _try_reply(
            reply_notification, reply_comment, {}, None,
            "bot", "sukria", "koan", "koan", "what?",
        )
        assert result is False

    @patch("app.github_command_handler.check_user_permission", return_value=False)
    def test_unauthorized_user_returns_false(
        self, mock_perm, reply_notification, reply_comment, reply_config,
    ):
        result = _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "what?",
        )
        assert result is False

    @patch("app.github_command_handler.check_user_permission", return_value=False)
    def test_falls_back_to_command_authorized_users_when_reply_list_unset(
        self, mock_perm, reply_notification, reply_comment
    ):
        config = {
            "github": {
                "nickname": "bot",
                "reply_enabled": True,
                "authorized_users": ["maintainer"],
            }
        }
        _try_reply(
            reply_notification, reply_comment, config, None,
            "bot", "sukria", "koan", "koan", "what?",
        )
        mock_perm.assert_called_once_with("sukria", "koan", "alice", ["maintainer"])

    @patch("app.github_command_handler.check_user_permission")
    def test_reply_wildcard_skips_permission_check(
        self, mock_perm, reply_comment
    ):
        notif = {"id": "1", "subject": {"url": ""}, "repository": {"full_name": "o/r"}}
        config = {
            "github": {
                "nickname": "bot",
                "reply_enabled": True,
                "authorized_users": ["maintainer"],
                "reply_authorized_users": ["*"],
            }
        }
        result = _try_reply(
            notif, reply_comment, config, None,
            "bot", "o", "r", "koan", "what?",
        )
        assert result is False
        mock_perm.assert_not_called()

    @patch("app.github_command_handler.check_user_permission", return_value=False)
    def test_reply_empty_authorized_users_disables_replies(
        self, mock_perm, reply_notification, reply_comment
    ):
        config = {
            "github": {
                "nickname": "bot",
                "reply_enabled": True,
                "authorized_users": ["*"],
                "reply_authorized_users": [],
            }
        }
        result = _try_reply(
            reply_notification, reply_comment, config, None,
            "bot", "sukria", "koan", "koan", "what?",
        )
        assert result is False
        mock_perm.assert_called_once_with("sukria", "koan", "alice", [])

    @patch("app.github_command_handler.check_user_permission")
    @patch("app.github_command_handler.extract_issue_number_from_notification")
    def test_reply_rate_limit_applies_per_user_per_hour(
        self, mock_issue, mock_perm, reply_notification, reply_comment
    ):
        mock_issue.return_value = "42"
        mock_perm.return_value = True
        config = {
            "github": {
                "nickname": "bot",
                "reply_enabled": True,
                "authorized_users": ["*"],
                "reply_rate_limit": 1,
            }
        }

        _try_reply(
            reply_notification, reply_comment, config, None,
            "bot", "sukria", "koan", "koan", "first?",
        )
        _try_reply(
            reply_notification, reply_comment, config, None,
            "bot", "sukria", "koan", "koan", "second?",
        )

        assert mock_issue.call_count == 1

    @patch("app.github_command_handler._notify_github_reply")
    @patch("app.github_command_handler._notify_github_question")
    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_reply.post_reply", return_value=True)
    @patch("app.github_reply.generate_reply", return_value="Here is my reply")
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "B", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_successful_reply(
        self, mock_resolve, mock_ctx, mock_gen, mock_post,
        mock_perm, mock_react, mock_read,
        mock_notify_q, mock_notify_r,
        reply_notification, reply_comment, reply_config,
    ):
        result = _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "what do you think?",
        )
        assert result is True
        mock_gen.assert_called_once()
        mock_post.assert_called_once_with("sukria", "koan", "42", "Here is my reply")
        # Should react with eyes emoji (not thumbs up), using comment API URL
        mock_react.assert_called_once_with(
            "sukria", "koan", "55555", emoji="eyes",
            comment_api_url="",
        )
        # Should send Telegram notifications
        mock_notify_q.assert_called_once_with(
            "alice", "sukria", "koan", "42", "what do you think?",
        )
        mock_notify_r.assert_called_once_with(
            "sukria", "koan", "42", "Here is my reply",
        )

    @patch("app.github_command_handler._notify_github_reply")
    @patch("app.github_command_handler._notify_github_question")
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_reply.generate_reply", return_value=None)
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "B", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_generation_failure_returns_false(
        self, mock_resolve, mock_ctx, mock_gen, mock_perm,
        mock_notify_q, mock_notify_r,
        reply_notification, reply_comment, reply_config,
    ):
        result = _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "question",
        )
        assert result is False
        # Question notification is sent, but reply notification is NOT
        mock_notify_q.assert_called_once()
        mock_notify_r.assert_not_called()

    @patch("app.github_command_handler._notify_github_reply")
    @patch("app.github_command_handler._notify_github_question")
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_reply.post_reply", return_value=False)
    @patch("app.github_reply.generate_reply", return_value="reply text")
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "B", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_post_failure_returns_false(
        self, mock_resolve, mock_ctx, mock_gen, mock_post, mock_perm,
        mock_notify_q, mock_notify_r,
        reply_notification, reply_comment, reply_config,
    ):
        result = _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "question",
        )
        assert result is False
        # Question notification sent, but reply notification NOT (post failed)
        mock_notify_q.assert_called_once()
        mock_notify_r.assert_not_called()

    def test_no_issue_number_returns_false(self, reply_comment, reply_config):
        notif = {"id": "1", "subject": {"url": ""}, "repository": {"full_name": "o/r"}}
        with patch("app.github_command_handler.check_user_permission", return_value=True):
            result = _try_reply(
                notif, reply_comment, reply_config, None,
                "bot", "o", "r", "proj", "q",
            )
        assert result is False

    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.utils.resolve_project_path", return_value=None)
    def test_no_project_path_returns_false(
        self, mock_resolve, mock_perm, reply_notification, reply_comment, reply_config,
    ):
        result = _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "q",
        )
        assert result is False


class TestProcessNotificationWithReply:
    """Tests for reply integration in process_single_notification."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.github_reply.post_reply", return_value=True)
    @patch("app.github_reply.generate_reply", return_value="AI reply here")
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "B", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_unknown_command_triggers_reply_when_enabled(
        self, mock_resolve_path, mock_ctx, mock_gen, mock_post,
        mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_perm, mock_react, mock_read,
        registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99999, "body": "@testbot what do you think about this PR?",
            "user": {"login": "alice"},
        }
        config = {
            "github": {
                "nickname": "testbot",
                "reply_enabled": True,
                "authorized_users": ["*"],
            }
        }

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )

        # Reply was posted, no error message
        assert success is False
        assert error is None
        mock_gen.assert_called_once()
        mock_post.assert_called_once()

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_unknown_command_falls_back_to_help_when_reply_disabled(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_read, registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99999, "body": "@testbot what do you think?",
            "user": {"login": "alice"},
        }
        config = {
            "github": {
                "nickname": "testbot",
                "reply_enabled": False,
            }
        }

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )

        # Falls back to help message
        assert success is False
        assert error is not None
        assert "`what`" in error


class TestGitHubTelegramNotifications:
    """Tests for ❓ and 💬 Telegram notifications from GitHub interactions."""

    @patch("app.notify.send_telegram", return_value=True)
    def test_question_notification_sends_telegram(self, mock_send):
        _notify_github_question("alice", "sukria", "koan", "42", "What about this?")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "❓" in msg
        assert "@alice" in msg
        assert "sukria/koan#42" in msg
        assert "What about this?" in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_question_notification_truncates_long_text(self, mock_send):
        long_question = "x" * 300
        _notify_github_question("bob", "owner", "repo", "7", long_question)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "…" in msg
        # Should truncate to 200 chars + ellipsis
        assert len(long_question) > len(msg)

    @patch("app.notify.send_telegram", return_value=True)
    def test_reply_notification_sends_telegram(self, mock_send):
        _notify_github_reply("sukria", "koan", "42", "Here is my analysis.")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "💬" in msg
        assert "sukria/koan#42" in msg
        assert "Here is my analysis." in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_reply_notification_truncates_long_text(self, mock_send):
        long_reply = "y" * 300
        _notify_github_reply("owner", "repo", "7", long_reply)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "…" in msg

    @patch("app.notify.send_telegram", side_effect=Exception("network error"))
    def test_question_notification_swallows_errors(self, mock_send):
        # Should not raise — error is caught internally
        _notify_github_question("alice", "o", "r", "1", "question")

    @patch("app.notify.send_telegram", side_effect=Exception("network error"))
    def test_reply_notification_swallows_errors(self, mock_send):
        # Should not raise — error is caught internally
        _notify_github_reply("o", "r", "1", "reply")


# ---------------------------------------------------------------------------
# _extract_url_from_context — URL regex precision
# ---------------------------------------------------------------------------


class TestExtractUrlFromContext:
    """Tests for URL extraction regex that shouldn't capture trailing punctuation."""

    def test_clean_pr_url(self):
        result = _extract_url_from_context(
            "rebase https://github.com/owner/repo/pull/42"
        )
        assert result is not None
        url, remaining = result
        assert url == "https://github.com/owner/repo/pull/42"
        assert remaining == "rebase"

    def test_clean_issue_url(self):
        result = _extract_url_from_context(
            "fix https://github.com/owner/repo/issues/10"
        )
        assert result is not None
        url, _ = result
        assert url == "https://github.com/owner/repo/issues/10"

    def test_url_with_trailing_paren(self):
        """Trailing ')' should NOT be captured."""
        result = _extract_url_from_context(
            "see (https://github.com/owner/repo/pull/5)"
        )
        assert result is not None
        url, _ = result
        assert not url.endswith(")")

    def test_url_with_trailing_bracket(self):
        """Trailing ']' should NOT be captured."""
        result = _extract_url_from_context(
            "[https://github.com/owner/repo/pull/5]"
        )
        assert result is not None
        url, _ = result
        assert not url.endswith("]")

    def test_repo_url_without_path_ignored(self):
        """Bare repo URL (no /pull/N or /issues/N) should NOT be extracted."""
        result = _extract_url_from_context("check https://github.com/owner/repo")
        assert result is None

    def test_no_url_returns_none(self):
        result = _extract_url_from_context("just some text without urls")
        assert result is None

    def test_url_with_dots_in_repo_name(self):
        result = _extract_url_from_context(
            "fix https://github.com/perl-actions/perl-versions/issues/31"
        )
        assert result is not None
        url, _ = result
        assert url == "https://github.com/perl-actions/perl-versions/issues/31"

    def test_url_with_hyphens_in_owner(self):
        result = _extract_url_from_context(
            "rebase https://github.com/my-org/my-repo/pull/99"
        )
        assert result is not None
        url, _ = result
        assert url == "https://github.com/my-org/my-repo/pull/99"


class TestCommentApiUrlThreading:
    """Tests that comment_api_url is properly threaded through the pipeline."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_pr_review_comment_url_threaded(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """PR review comment URL is passed to add_reaction for correct endpoint."""
        pr_comment_url = "https://api.github.com/repos/sukria/koan/pulls/comments/42"
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 42,
            "url": pr_comment_url,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }

        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        # Verify comment_api_url was passed to add_reaction
        mock_react.assert_called_once_with(
            "sukria", "koan", "42",
            comment_api_url=pr_comment_url,
        )

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_comment_url_passed_to_check_already_processed(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """comment_api_url is passed to check_already_processed."""
        issue_comment_url = "https://api.github.com/repos/sukria/koan/issues/comments/99"
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99,
            "url": issue_comment_url,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
        }

        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        # check_already_processed should receive the URL
        mock_processed.assert_called_once_with(
            "99", "testbot", "sukria", "koan",
            comment_api_url=issue_comment_url,
        )


# ---------------------------------------------------------------------------
# _fetch_and_filter_comment — direct unit tests
# ---------------------------------------------------------------------------


class TestFetchAndFilterComment:
    """Direct tests for _fetch_and_filter_comment helper."""

    @pytest.fixture
    def notification(self):
        return {
            "id": "7777",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "url": "https://api.github.com/repos/owner/repo/issues/10",
                "latest_comment_url": "https://api.github.com/repos/owner/repo/issues/comments/555",
            },
        }

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.is_notification_stale", return_value=True)
    def test_stale_notification_returns_none_and_marks_read(
        self, mock_stale, mock_read, notification,
    ):
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result is None
        mock_read.assert_called_once_with("7777")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.find_mention_in_thread", return_value=None)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification", return_value=None)
    def test_no_comment_falls_back_to_thread_search(self, mock_comment, mock_stale, mock_find, mock_read, notification):
        """When latest_comment_url fails, search thread before giving up."""
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result is None
        mock_find.assert_called_once_with(notification, "bot")
        mock_read.assert_called_once_with("7777")

    @patch("app.github_command_handler.find_mention_in_thread")
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification", return_value=None)
    def test_no_comment_thread_search_finds_mention(self, mock_comment, mock_stale, mock_find, notification):
        """When latest_comment_url fails but thread has an unprocessed @mention."""
        real_mention = {"id": 999, "body": "@bot rebase", "user": {"login": "alice"}}
        mock_find.return_value = real_mention
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result == real_mention
        mock_find.assert_called_once_with(notification, "bot")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.find_mention_in_thread", return_value=None)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_no_mention_in_body_searches_thread(self, mock_comment, mock_stale, mock_self, mock_find, mock_read, notification):
        """When latest comment doesn't mention bot (URL shifted), search thread."""
        mock_comment.return_value = {
            "id": 555, "body": "CI passed", "user": {"login": "ci-bot"},
        }
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result is None
        mock_find.assert_called_once_with(notification, "bot")
        mock_read.assert_called_once_with("7777")

    @patch("app.github_command_handler.find_mention_in_thread")
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_no_mention_in_body_thread_search_finds_mention(self, mock_comment, mock_stale, mock_self, mock_find, notification):
        """When latest comment doesn't mention bot, but thread has the real @mention."""
        mock_comment.return_value = {
            "id": 555, "body": "CI passed", "user": {"login": "ci-bot"},
        }
        real_mention = {"id": 888, "body": "@bot rebase", "user": {"login": "alice"}}
        mock_find.return_value = real_mention
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result == real_mention

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.find_mention_in_thread", return_value=None)
    @patch("app.github_command_handler.is_self_mention", return_value=True)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_self_mention_searches_thread_then_skips(
        self, mock_comment, mock_stale, mock_self, mock_find, mock_read, notification,
    ):
        """When latest_comment_url is bot's own, search thread; skip if no mention found."""
        mock_comment.return_value = {
            "id": 555, "body": "my fix", "user": {"login": "bot"},
        }
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result is None
        mock_find.assert_called_once_with(notification, "bot")
        mock_read.assert_called_once_with("7777")

    @patch("app.github_command_handler.find_mention_in_thread")
    @patch("app.github_command_handler.is_self_mention", return_value=True)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_self_mention_fallback_returns_real_mention(
        self, mock_comment, mock_stale, mock_self, mock_find, notification,
    ):
        """When latest_comment_url is bot's own, but thread has an unprocessed @mention."""
        mock_comment.return_value = {
            "id": 555, "body": "my fix", "user": {"login": "bot"},
        }
        real_mention = {"id": 888, "body": "@bot rebase", "user": {"login": "alice"}}
        mock_find.return_value = real_mention
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result == real_mention

    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_valid_comment_returned(self, mock_comment, mock_stale, mock_self, notification):
        comment = {"id": 555, "body": "@bot rebase", "user": {"login": "alice"}}
        mock_comment.return_value = comment
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result == comment

    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_missing_notification_id_still_works(self, mock_comment, mock_stale, mock_self):
        """Notification with missing id field should not crash."""
        notification = {"repository": {"full_name": "o/r"}, "subject": {}}
        mock_comment.return_value = {
            "id": 1, "body": "@bot test", "user": {"login": "alice"},
        }
        result = _fetch_and_filter_comment(notification, "bot", max_age_hours=24)
        assert result is not None


# ---------------------------------------------------------------------------
# _validate_and_parse_command — direct unit tests
# ---------------------------------------------------------------------------


class TestValidateAndParseCommand:
    """Direct tests for _validate_and_parse_command helper."""

    @pytest.fixture
    def notification(self):
        return {
            "id": "8888",
            "repository": {"full_name": "sukria/koan"},
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
            },
        }

    @pytest.fixture
    def config(self):
        return {"github": {"nickname": "testbot"}}

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=True)
    def test_already_processed_returns_none_tuple(
        self, mock_processed, mock_read, notification, config, registry,
    ):
        comment = {"id": 42, "url": "", "body": "@testbot rebase", "user": {"login": "a"}}
        skill, cmd, ctx = _validate_and_parse_command(
            notification, comment, config, registry, "testbot", "sukria", "koan",
        )
        assert skill is None
        assert cmd is None
        mock_read.assert_called_once_with("8888")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.parse_mention_command", return_value=None)
    def test_no_mention_returns_none_tuple(
        self, mock_parse, mock_processed, mock_read, notification, config, registry,
    ):
        comment = {"id": 42, "url": "", "body": "some text", "user": {"login": "a"}}
        skill, cmd, ctx = _validate_and_parse_command(
            notification, comment, config, registry, "testbot", "sukria", "koan",
        )
        assert skill is None
        assert cmd is None
        mock_read.assert_called_once_with("8888")

    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.parse_mention_command", return_value=("rebase", ""))
    def test_valid_command_returns_skill(
        self, mock_parse, mock_processed, notification, config, registry,
    ):
        comment = {"id": 42, "url": "", "body": "@testbot rebase", "user": {"login": "a"}}
        skill, cmd, ctx = _validate_and_parse_command(
            notification, comment, config, registry, "testbot", "sukria", "koan",
        )
        assert skill is not None
        assert skill.name == "rebase"
        assert cmd == "rebase"

    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.parse_mention_command", return_value=("badcmd", "extra"))
    def test_invalid_command_returns_none_skill_with_name(
        self, mock_parse, mock_processed, notification, config, registry,
    ):
        comment = {"id": 42, "url": "", "body": "@testbot badcmd extra", "user": {"login": "a"}}
        skill, cmd, ctx = _validate_and_parse_command(
            notification, comment, config, registry, "testbot", "sukria", "koan",
        )
        assert skill is None
        assert cmd == "badcmd"
        assert ctx == "extra"

    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.parse_mention_command", return_value=("implement", "do it"))
    def test_context_passed_through(
        self, mock_parse, mock_processed, notification, config, registry,
    ):
        comment = {"id": 42, "url": "", "body": "@testbot implement do it", "user": {"login": "a"}}
        skill, cmd, ctx = _validate_and_parse_command(
            notification, comment, config, registry, "testbot", "sukria", "koan",
        )
        assert skill is not None
        assert cmd == "implement"
        assert ctx == "do it"


# ---------------------------------------------------------------------------
# Additional edge cases for process_single_notification
# ---------------------------------------------------------------------------


class TestProcessNotificationEdgeCases:
    """Edge cases for process_single_notification not covered by existing tests."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.find_mention_in_thread", return_value=None)
    @patch("app.github_command_handler.is_self_mention", return_value=True)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    def test_self_mention_skipped_when_no_thread_mention(
        self, mock_comment, mock_stale, mock_self, mock_find, mock_read,
        registry, sample_notification,
    ):
        """Self-mentions with no unprocessed @mention in thread should be silently skipped."""
        mock_comment.return_value = {
            "id": 99, "body": "my fix", "user": {"login": "bot"},
        }
        success, error = process_single_notification(
            sample_notification, registry, {}, None, "bot",
        )
        assert success is False
        assert error is None
        mock_read.assert_called()

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=True)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_already_processed_skipped(
        self, mock_resolve, mock_comment, mock_stale, mock_self,
        mock_processed, mock_read, registry, sample_notification,
    ):
        """Already-processed comments should be silently skipped."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99, "url": "", "body": "@testbot rebase",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot"}}
        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        assert success is False
        assert error is None

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_context_aware_command_includes_context_in_mission(
        self, mock_insert, mock_resolve, mock_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """Context-aware commands should include context in mission text."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99, "body": "@testbot implement focus on API layer",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        mission_arg = mock_insert.call_args[0][1]
        assert "focus on API layer" in mission_arg
        assert "/implement" in mission_arg

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_non_context_aware_command_excludes_context(
        self, mock_insert, mock_resolve, mock_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """Non-context-aware commands should NOT include extra context."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99, "body": "@testbot rebase this needs fixing",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        mission_arg = mock_insert.call_args[0][1]
        assert "this needs fixing" not in mission_arg
        assert "/rebase" in mission_arg

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_url_in_context_overrides_notification_url(
        self, mock_insert, mock_resolve, mock_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """URL in user context should override notification's subject URL."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99,
            "body": "@testbot rebase https://github.com/other/repo/pull/7",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        mission_arg = mock_insert.call_args[0][1]
        assert "https://github.com/other/repo/pull/7" in mission_arg
        # Original notification URL should not appear
        assert "sukria/koan/pull/42" not in mission_arg

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_reaction_called_after_successful_insert(
        self, mock_insert, mock_resolve, mock_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """Reaction should only happen AFTER mission is successfully persisted."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_comment.return_value = {
            "id": 99, "body": "@testbot rebase",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        call_order = []
        mock_insert.side_effect = lambda *a, **kw: call_order.append("insert")
        mock_react.side_effect = lambda *a, **kw: call_order.append("react")

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert call_order == ["insert", "react"]


# ---------------------------------------------------------------------------
# Additional edge cases for post_error_reply
# ---------------------------------------------------------------------------


class TestPostErrorReplyEdgeCases:
    """Edge cases for post_error_reply."""

    def setup_method(self):
        _error_replies.clear()

    @patch("app.github.api", side_effect=RuntimeError("API error"))
    def test_api_failure_returns_false(self, mock_api):
        result = post_error_reply("owner", "repo", "42", "123", "error msg")
        assert result is False

    @patch("app.github.api", side_effect=RuntimeError("API error"))
    def test_api_failure_not_cached_as_sent(self, mock_api):
        """API failures should NOT be cached — allow retry."""
        post_error_reply("owner", "repo", "42", "123", "error msg")
        assert "123:error msg" not in _error_replies

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_comment_api_url_passed_to_reaction(self, mock_api, mock_react):
        mock_api.return_value = ""
        post_error_reply(
            "owner", "repo", "42", "123", "error",
            comment_api_url="https://api.github.com/repos/o/r/pulls/comments/123",
        )
        mock_react.assert_called_once_with(
            "owner", "repo", "123",
            comment_api_url="https://api.github.com/repos/o/r/pulls/comments/123",
        )

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_error_body_has_emoji_prefix(self, mock_api, mock_react):
        """Error reply body should start with ❌."""
        mock_api.return_value = ""
        post_error_reply("owner", "repo", "42", "123", "Test error")
        call_args = mock_api.call_args
        # Check -f body= arg
        extra_args = call_args[1].get("extra_args", call_args[0][1] if len(call_args[0]) > 1 else [])
        body_arg = [a for a in extra_args if a.startswith("body=")]
        assert body_arg
        assert body_arg[0].startswith("body=❌")

    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github.api")
    def test_different_comments_same_error_not_deduplicated(self, mock_api, mock_react):
        """Same error text on different comments should not be deduplicated."""
        mock_api.return_value = ""
        post_error_reply("owner", "repo", "42", "111", "same error")
        post_error_reply("owner", "repo", "42", "222", "same error")
        assert mock_api.call_count == 2


# ---------------------------------------------------------------------------
# Additional edge cases for _extract_url_from_context
# ---------------------------------------------------------------------------


class TestExtractUrlFromContextEdgeCases:
    """Additional edge cases for URL extraction."""

    def test_url_at_beginning_of_context(self):
        result = _extract_url_from_context("https://github.com/owner/repo/pull/1 please fix")
        assert result is not None
        url, remaining = result
        assert url == "https://github.com/owner/repo/pull/1"
        assert remaining == "please fix"

    def test_url_at_end_of_context(self):
        result = _extract_url_from_context("please fix https://github.com/owner/repo/pull/1")
        assert result is not None
        url, remaining = result
        assert url == "https://github.com/owner/repo/pull/1"
        assert remaining == "please fix"

    def test_url_only(self):
        result = _extract_url_from_context("https://github.com/owner/repo/pull/1")
        assert result is not None
        url, remaining = result
        assert url == "https://github.com/owner/repo/pull/1"
        assert remaining == ""

    def test_http_url(self):
        """HTTP URLs should be extracted too."""
        result = _extract_url_from_context("fix http://github.com/owner/repo/issues/5")
        assert result is not None
        url, _ = result
        assert url == "http://github.com/owner/repo/issues/5"

    def test_multiple_urls_returns_first(self):
        text = "see https://github.com/a/b/pull/1 and https://github.com/c/d/pull/2"
        result = _extract_url_from_context(text)
        assert result is not None
        url, _ = result
        assert url == "https://github.com/a/b/pull/1"

    def test_non_github_url_ignored(self):
        result = _extract_url_from_context("check https://example.com/page")
        assert result is None

    def test_empty_string(self):
        assert _extract_url_from_context("") is None

    def test_underscore_in_repo_name(self):
        result = _extract_url_from_context("https://github.com/org/my_repo/issues/3")
        assert result is not None
        url, _ = result
        assert url == "https://github.com/org/my_repo/issues/3"

    def test_bare_repo_url_with_extra_text_ignored(self):
        """Bare repo URL with surrounding text should NOT match."""
        result = _extract_url_from_context(
            "check https://github.com/other/repo for reference"
        )
        assert result is None

    def test_repo_url_with_tree_path_ignored(self):
        """Repo URL with /tree/ or /blob/ path should NOT match."""
        result = _extract_url_from_context(
            "see https://github.com/owner/repo/tree/main/src"
        )
        assert result is None

    def test_pull_url_still_matches(self):
        """PR URL should still be extracted correctly."""
        result = _extract_url_from_context(
            "rebase https://github.com/owner/repo/pull/42 please"
        )
        assert result is not None
        url, remaining = result
        assert url == "https://github.com/owner/repo/pull/42"
        assert remaining == "rebase please"


# ---------------------------------------------------------------------------
# Additional edge cases for extract_issue_number_from_notification
# ---------------------------------------------------------------------------


class TestExtractIssueNumberEdgeCases:

    def test_commits_url_no_number(self):
        """Commit URLs should return None (no issues/pulls pattern)."""
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/commits/abc123"}}
        assert extract_issue_number_from_notification(notif) is None

    def test_empty_subject(self):
        notif = {"subject": {}}
        assert extract_issue_number_from_notification(notif) is None

    def test_large_issue_number(self):
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/issues/99999"}}
        assert extract_issue_number_from_notification(notif) == "99999"


# ---------------------------------------------------------------------------
# Additional edge cases for resolve_project_from_notification
# ---------------------------------------------------------------------------


class TestResolveProjectEdgeCases:

    def test_empty_full_name(self):
        notif = {"repository": {"full_name": ""}}
        assert resolve_project_from_notification(notif) is None

    def test_no_slash_in_full_name(self):
        notif = {"repository": {"full_name": "noowner"}}
        assert resolve_project_from_notification(notif) is None

    def test_missing_repository_key(self):
        assert resolve_project_from_notification({"id": "1"}) is None


# ---------------------------------------------------------------------------
# Telegram notification truncation boundary tests
# ---------------------------------------------------------------------------


class TestTelegramNotificationBoundaries:
    """Exact boundary tests for 200-char truncation."""

    @patch("app.notify.send_telegram", return_value=True)
    def test_question_exactly_200_chars_no_truncation(self, mock_send):
        text = "x" * 200
        _notify_github_question("alice", "o", "r", "1", text)
        msg = mock_send.call_args[0][0]
        assert "…" not in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_question_201_chars_truncated(self, mock_send):
        text = "x" * 201
        _notify_github_question("alice", "o", "r", "1", text)
        msg = mock_send.call_args[0][0]
        assert "…" in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_reply_exactly_200_chars_no_truncation(self, mock_send):
        text = "y" * 200
        _notify_github_reply("o", "r", "1", text)
        msg = mock_send.call_args[0][0]
        assert "…" not in msg

    @patch("app.notify.send_telegram", return_value=True)
    def test_reply_201_chars_truncated(self, mock_send):
        text = "y" * 201
        _notify_github_reply("o", "r", "1", text)
        msg = mock_send.call_args[0][0]
        assert "…" in msg


# ---------------------------------------------------------------------------
# _try_reply — additional edge cases
# ---------------------------------------------------------------------------


class TestTryReplyEdgeCases:

    @pytest.fixture
    def reply_notification(self):
        return {
            "id": "77777",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/issues/42",
            },
            "repository": {"full_name": "sukria/koan"},
        }

    @pytest.fixture
    def reply_comment(self):
        return {
            "id": 55555,
            "body": "@bot what about this?",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/55555",
        }

    @pytest.fixture
    def reply_config(self):
        return {
            "github": {
                "nickname": "bot",
                "reply_enabled": True,
                "authorized_users": ["*"],
            }
        }

    @patch("app.github_command_handler._notify_github_question")
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_reply.generate_reply", return_value=None)
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "B", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_question_notification_sent_before_generation(
        self, mock_resolve, mock_ctx, mock_gen, mock_perm,
        mock_notify_q, reply_notification, reply_comment, reply_config,
    ):
        """Question notification should be sent BEFORE generation (even if it fails)."""
        _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "question?",
        )
        mock_notify_q.assert_called_once()

    @patch("app.github_command_handler._notify_github_reply")
    @patch("app.github_command_handler._notify_github_question")
    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_reply.post_reply", return_value=True)
    @patch("app.github_reply.generate_reply", return_value="reply text")
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "B", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_eyes_reaction_used_for_replies(
        self, mock_resolve, mock_ctx, mock_gen, mock_post,
        mock_perm, mock_react, mock_read,
        mock_notify_q, mock_notify_r,
        reply_notification, reply_comment, reply_config,
    ):
        """Reply reactions should use 'eyes' emoji, not the default 'thumbsup'."""
        _try_reply(
            reply_notification, reply_comment, reply_config, None,
            "bot", "sukria", "koan", "koan", "question?",
        )
        mock_react.assert_called_once()
        assert mock_react.call_args[1].get("emoji") == "eyes"

    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.utils.resolve_project_path", return_value="/tmp/koan")
    def test_projects_config_passed_to_authorized_users(
        self, mock_resolve, mock_perm,
        reply_notification, reply_comment, reply_config,
    ):
        """projects_config should be passed through to get_github_authorized_users."""
        projects_cfg = {"koan": {"github": {"authorized_users": ["bob"]}}}
        with patch("app.github_command_handler.get_github_authorized_users") as mock_auth:
            mock_auth.return_value = ["bob"]
            with patch("app.github_command_handler.extract_issue_number_from_notification", return_value="42"):
                with patch("app.github_reply.fetch_thread_context"):
                    with patch("app.github_reply.generate_reply", return_value=None):
                        with patch("app.github_command_handler._notify_github_question"):
                            _try_reply(
                                reply_notification, reply_comment, reply_config,
                                projects_cfg, "bot", "sukria", "koan", "koan", "q",
                            )
            mock_auth.assert_called_once_with(reply_config, "koan", projects_cfg)


# ---------------------------------------------------------------------------
# build_mission_from_command — additional edge cases
# ---------------------------------------------------------------------------


class TestBuildMissionEdgeCases:

    def test_context_url_but_not_context_aware(self):
        """URL in context should still be used even if skill is not context-aware."""
        skill = Skill(
            name="rebase", scope="core", github_enabled=True,
            github_context_aware=False,
            commands=[SkillCommand(name="rebase")],
        )
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/pulls/1"}}
        mission = build_mission_from_command(
            skill, "rebase",
            "https://github.com/other/repo/pull/5 extra text",
            notif, "proj",
        )
        # URL override happens before context_aware check
        assert "https://github.com/other/repo/pull/5" in mission
        # Context text should NOT be included (not context-aware)
        assert "extra text" not in mission

    def test_empty_context_no_trailing_space(self):
        """Empty context should not add trailing spaces."""
        skill = Skill(
            name="fix", scope="core", github_enabled=True,
            github_context_aware=True,
            commands=[SkillCommand(name="fix")],
        )
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/issues/3"}}
        mission = build_mission_from_command(skill, "fix", "", notif, "myproj")
        assert mission == "- [project:myproj] /fix https://github.com/o/r/issues/3 📬"

    def test_no_web_url_and_no_context(self):
        """Mission with no URL and no context."""
        skill = Skill(
            name="check", scope="core", github_enabled=True,
            github_context_aware=False,
            commands=[SkillCommand(name="check")],
        )
        notif = {"subject": {}}
        mission = build_mission_from_command(skill, "check", "", notif, "proj")
        assert mission == "- [project:proj] /check 📬"

    @patch("app.github_command_handler._resolve_project_from_url", return_value="grep")
    def test_context_url_cross_repo_resolves_project(self, mock_resolve):
        """When context URL points to a different repo, project tag should update."""
        skill = Skill(
            name="plan", scope="core", github_enabled=True,
            github_context_aware=True,
            commands=[SkillCommand(name="plan")],
        )
        # Notification from investmindr (project: backend)
        notif = {"subject": {"url": "https://api.github.com/repos/Anantys/investmindr/pulls/100"}}
        # Context URL points to metacpan (project: grep)
        context = "https://github.com/metacpan/metacpan-grep-front-end/pull/75"
        mission = build_mission_from_command(
            skill, "plan", context, notif, "backend"
        )
        # Project should be re-resolved to grep, not stay as backend
        assert "[project:grep]" in mission
        assert "[project:backend]" not in mission
        assert "metacpan-grep-front-end/pull/75" in mission

    @patch("app.github_command_handler._resolve_project_from_url", return_value=None)
    def test_context_url_cross_repo_unknown_keeps_original(self, mock_resolve):
        """When context URL repo is unknown, keep the original project."""
        skill = Skill(
            name="plan", scope="core", github_enabled=True,
            github_context_aware=True,
            commands=[SkillCommand(name="plan")],
        )
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/pulls/1"}}
        context = "https://github.com/unknown/repo/issues/5"
        mission = build_mission_from_command(
            skill, "plan", context, notif, "original"
        )
        # Should keep original project since URL repo is unknown
        assert "[project:original]" in mission

    def test_no_context_url_keeps_original_project(self):
        """Without a context URL, project tag stays from notification."""
        skill = Skill(
            name="rebase", scope="core", github_enabled=True,
            github_context_aware=False,
            commands=[SkillCommand(name="rebase")],
        )
        notif = {"subject": {"url": "https://api.github.com/repos/o/r/pulls/1"}}
        mission = build_mission_from_command(
            skill, "rebase", "", notif, "backend"
        )
        assert "[project:backend]" in mission


# ---------------------------------------------------------------------------
# _resolve_project_from_url
# ---------------------------------------------------------------------------


class TestResolveProjectFromUrl:
    @patch("app.utils.resolve_project_path", return_value="/path/to/grep")
    @patch("app.utils.project_name_for_path", return_value="grep")
    def test_resolves_from_pr_url(self, mock_name, mock_resolve):
        from app.github_command_handler import _resolve_project_from_url
        result = _resolve_project_from_url(
            "https://github.com/metacpan/metacpan-grep-front-end/pull/75"
        )
        assert result == "grep"
        mock_resolve.assert_called_with("metacpan-grep-front-end", owner="metacpan")

    @patch("app.utils.resolve_project_path", return_value="/path/to/proj")
    @patch("app.utils.project_name_for_path", return_value="proj")
    def test_resolves_from_issue_url(self, mock_name, mock_resolve):
        from app.github_command_handler import _resolve_project_from_url
        result = _resolve_project_from_url(
            "https://github.com/owner/myrepo/issues/42"
        )
        assert result == "proj"

    @patch("app.utils.resolve_project_path", return_value=None)
    def test_unknown_repo_returns_none(self, mock_resolve):
        from app.github_command_handler import _resolve_project_from_url
        result = _resolve_project_from_url(
            "https://github.com/unknown/repo/pull/1"
        )
        assert result is None

    def test_non_github_url_returns_none(self):
        from app.github_command_handler import _resolve_project_from_url
        assert _resolve_project_from_url("https://example.com/foo") is None

    def test_empty_string_returns_none(self):
        from app.github_command_handler import _resolve_project_from_url
        assert _resolve_project_from_url("") is None


# ---------------------------------------------------------------------------
# validate_command — additional edge cases
# ---------------------------------------------------------------------------


class TestValidateCommandEdgeCases:

    def test_empty_command_name(self, registry):
        assert validate_command("", registry) is None

    def test_case_sensitive(self, registry):
        """Command lookup should be case-sensitive."""
        assert validate_command("REBASE", registry) is None

    def test_empty_registry(self):
        reg = SkillRegistry()
        assert validate_command("rebase", reg) is None


# ---------------------------------------------------------------------------
# format_help_list_message — clean help (no "Unknown command" prefix)
# ---------------------------------------------------------------------------


class TestFormatHelpListMessage:
    def test_does_not_contain_unknown_command(self, registry):
        msg = format_help_list_message(registry, "koanbot")
        assert "Unknown command" not in msg

    def test_lists_available_commands(self, registry):
        msg = format_help_list_message(registry, "koanbot")
        assert "`@koanbot rebase`" in msg
        assert "`@koanbot implement`" in msg

    def test_includes_help_entry(self, registry):
        msg = format_help_list_message(registry, "koanbot")
        assert "`@koanbot help`" in msg

    def test_includes_usage_line(self, registry):
        msg = format_help_list_message(registry, "koanbot")
        assert "Usage:" in msg
        assert "`@koanbot <command>`" in msg

    def test_starts_with_friendly_intro(self, registry):
        msg = format_help_list_message(registry, "koanbot")
        assert msg.startswith("Here are the commands I support:")

    def test_empty_registry(self):
        reg = SkillRegistry()
        msg = format_help_list_message(reg, "koanbot")
        assert "help" in msg
        assert "Usage:" in msg


# ---------------------------------------------------------------------------
# _post_help_reply
# ---------------------------------------------------------------------------


class TestPostHelpReply:
    @patch("app.github.api")
    def test_posts_comment(self, mock_api):
        result = _post_help_reply("owner", "repo", "42", "Help text here")
        assert result is True
        mock_api.assert_called_once()
        call_args = mock_api.call_args
        assert "repos/owner/repo/issues/42/comments" in call_args[0][0]
        extra_args = call_args[1].get("extra_args", [])
        body_arg = [a for a in extra_args if a.startswith("body=")]
        assert body_arg
        assert "Help text here" in body_arg[0]

    @patch("app.github.api", side_effect=RuntimeError("API error"))
    def test_api_failure_returns_false(self, mock_api):
        result = _post_help_reply("owner", "repo", "42", "Help text")
        assert result is False


# ---------------------------------------------------------------------------
# _handle_help_command
# ---------------------------------------------------------------------------


class TestHandleHelpCommand:
    @pytest.fixture
    def help_notification(self):
        return {
            "id": "55555",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
            },
            "repository": {"full_name": "sukria/koan"},
        }

    @pytest.fixture
    def help_comment(self):
        return {
            "id": 77777,
            "body": "@koanbot help",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/77777",
        }

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler._post_help_reply", return_value=True)
    def test_posts_help_and_reacts(
        self, mock_post, mock_react, mock_read,
        help_notification, help_comment, registry,
    ):
        result = _handle_help_command(
            help_notification, help_comment, registry, "koanbot",
            "sukria", "koan",
        )
        assert result is True
        mock_post.assert_called_once()
        # Help message should list commands
        help_text = mock_post.call_args[0][3]
        assert "rebase" in help_text
        assert "implement" in help_text
        # Reaction should use eyes emoji
        mock_react.assert_called_once()
        assert mock_react.call_args[1].get("emoji") == "eyes"
        # Notification marked as read
        mock_read.assert_called_once_with("55555")

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler._post_help_reply", return_value=False)
    def test_post_failure_returns_false(
        self, mock_post, mock_read,
        help_notification, help_comment, registry,
    ):
        result = _handle_help_command(
            help_notification, help_comment, registry, "koanbot",
            "sukria", "koan",
        )
        assert result is False
        mock_read.assert_called_once()

    @patch("app.github_command_handler.mark_notification_read")
    def test_no_issue_number_returns_false(self, mock_read, help_comment, registry):
        notif = {"id": "55555", "subject": {}}
        result = _handle_help_command(
            notif, help_comment, registry, "koanbot", "sukria", "koan",
        )
        assert result is False
        mock_read.assert_called_once()


# ---------------------------------------------------------------------------
# process_single_notification — help command integration
# ---------------------------------------------------------------------------


class TestProcessNotificationHelpCommand:
    """Integration tests for the built-in help command in process_single_notification."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler._post_help_reply", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_help_command_posts_reply(
        self, mock_resolve, mock_get_comment, mock_stale, mock_self,
        mock_processed, mock_post, mock_react, mock_read,
        registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot help",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        config = {"github": {"nickname": "testbot"}}

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        # Help does not create a mission, so success=False, error=None
        assert success is False
        assert error is None
        # Help reply was posted
        mock_post.assert_called_once()

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler._post_help_reply", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_help_command_does_not_queue_mission(
        self, mock_resolve, mock_get_comment, mock_stale, mock_self,
        mock_processed, mock_post, mock_react, mock_read,
        registry, sample_notification,
    ):
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot help",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        config = {"github": {"nickname": "testbot"}}

        with patch("app.utils.insert_pending_mission") as mock_insert:
            process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )
            mock_insert.assert_not_called()

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler._post_help_reply", return_value=False)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    def test_help_post_failure_still_no_error(
        self, mock_resolve, mock_get_comment, mock_stale, mock_self,
        mock_processed, mock_post, mock_read,
        registry, sample_notification,
    ):
        """Even if help reply fails to post, no error is returned to avoid error reply."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot help",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        config = {"github": {"nickname": "testbot"}}

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )
        assert success is False
        assert error is None


# ---------------------------------------------------------------------------
# process_single_notification — plan command via github_enabled
# ---------------------------------------------------------------------------


class TestProcessNotificationPlanCommand:
    """Test that plan works as a github_enabled command via @mention."""

    @pytest.fixture
    def plan_registry(self):
        """Registry with plan skill github_enabled."""
        plan_skill = Skill(
            name="plan",
            scope="core",
            description="Plan an idea or iterate on an existing GitHub issue",
            github_enabled=True,
            github_context_aware=True,
            commands=[SkillCommand(name="plan", description="Plan an idea or iterate on an existing GitHub issue")],
        )
        reg = SkillRegistry()
        reg._register(plan_skill)
        return reg

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_plan_queues_mission_with_url(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, plan_registry, tmp_path,
    ):
        notif = {
            "id": "44444",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/issues/100",
            },
            "repository": {"full_name": "sukria/koan"},
        }
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 88888,
            "body": "@testbot plan",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                notif, plan_registry, config, None, "testbot",
            )

        assert success is True
        assert error is None
        mock_insert.assert_called_once()
        mission = mock_insert.call_args[0][1]
        assert "/plan" in mission
        assert "https://github.com/sukria/koan/issues/100" in mission

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_plan_includes_context(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, plan_registry, tmp_path,
    ):
        notif = {
            "id": "44444",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/issues/100",
            },
            "repository": {"full_name": "sukria/koan"},
        }
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 88888,
            "body": "@testbot plan focus on auth module",
            "user": {"login": "alice"},
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                notif, plan_registry, config, None, "testbot",
            )

        assert success is True
        mission = mock_insert.call_args[0][1]
        assert "focus on auth module" in mission


# ---------------------------------------------------------------------------
# NLP intent classification tests
# ---------------------------------------------------------------------------


class TestTryNlpClassification:
    """Unit tests for _try_nlp_classification."""

    def test_disabled_by_default(self, registry):
        comment = {"body": "@bot fix this bug", "user": {"login": "alice"}}
        config = {"github": {"nickname": "bot"}}
        result = _try_nlp_classification(
            comment, config, None, registry, "bot", "myproject", "owner", "repo",
        )
        assert result is None

    @patch("app.github_intent.classify_intent")
    @patch("app.github_reply.extract_mention_text", return_value="fix this bug")
    @patch("app.utils.resolve_project_path", return_value="/tmp/myproject")
    def test_successful_classification(
        self, mock_resolve, mock_extract, mock_classify, registry,
    ):
        mock_classify.return_value = {"command": "implement", "context": "the auth bug"}
        comment = {"body": "@bot fix this bug", "user": {"login": "alice"}}
        config = {"github": {"nickname": "bot", "natural_language": True}}

        result = _try_nlp_classification(
            comment, config, None, registry, "bot", "myproject", "owner", "repo",
        )
        assert result is not None
        skill, cmd, ctx = result
        assert skill.name == "implement"
        assert cmd == "implement"
        assert ctx == "the auth bug"

    @patch("app.github_intent.classify_intent")
    @patch("app.github_reply.extract_mention_text", return_value="do something")
    @patch("app.utils.resolve_project_path", return_value="/tmp/myproject")
    def test_no_match_returns_none(
        self, mock_resolve, mock_extract, mock_classify, registry,
    ):
        mock_classify.return_value = {"command": None, "context": ""}
        comment = {"body": "@bot do something", "user": {"login": "alice"}}
        config = {"github": {"nickname": "bot", "natural_language": True}}

        result = _try_nlp_classification(
            comment, config, None, registry, "bot", "myproject", "owner", "repo",
        )
        assert result is None

    @patch("app.github_intent.classify_intent", return_value=None)
    @patch("app.github_reply.extract_mention_text", return_value="test")
    @patch("app.utils.resolve_project_path", return_value="/tmp/myproject")
    def test_cli_failure_returns_none(
        self, mock_resolve, mock_extract, mock_classify, registry,
    ):
        comment = {"body": "@bot test", "user": {"login": "alice"}}
        config = {"github": {"nickname": "bot", "natural_language": True}}

        result = _try_nlp_classification(
            comment, config, None, registry, "bot", "myproject", "owner", "repo",
        )
        assert result is None

    @patch("app.github_intent.classify_intent")
    @patch("app.github_reply.extract_mention_text", return_value="do status check")
    @patch("app.utils.resolve_project_path", return_value="/tmp/myproject")
    def test_classified_non_github_command_returns_none(
        self, mock_resolve, mock_extract, mock_classify,
    ):
        """If NLP classifies to a command that isn't github_enabled, return None."""
        mock_classify.return_value = {"command": "status", "context": ""}
        # Registry with only a non-github-enabled skill
        skill = Skill(
            name="status", scope="core", github_enabled=False,
            commands=[SkillCommand(name="status")],
        )
        reg = SkillRegistry()
        reg._register(skill)

        comment = {"body": "@bot do status check", "user": {"login": "alice"}}
        config = {"github": {"nickname": "bot", "natural_language": True}}

        result = _try_nlp_classification(
            comment, config, None, reg, "bot", "myproject", "owner", "repo",
        )
        assert result is None

    def test_per_project_override_disables(self, registry):
        """Per-project natural_language=false overrides global true."""
        comment = {"body": "@bot fix this", "user": {"login": "alice"}}
        config = {"github": {"nickname": "bot", "natural_language": True}}
        projects_config = {
            "projects": {
                "myproject": {
                    "path": "/tmp/myproject",
                    "github": {"natural_language": False},
                }
            }
        }

        result = _try_nlp_classification(
            comment, config, projects_config, registry, "bot",
            "myproject", "owner", "repo",
        )
        assert result is None


class TestProcessNotificationWithNLP:
    """Integration tests for NLP fallback in process_single_notification."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    @patch("app.github_command_handler._try_nlp_classification")
    def test_nlp_fallback_creates_mission(
        self, mock_nlp, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """NLP classification succeeds → mission is created."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot this is a bug for you, fix it",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }

        # Rigid parse will produce command_name="this" which is invalid
        # NLP fallback should kick in
        impl_skill = registry.find_by_command("implement")
        mock_nlp.return_value = (impl_skill, "implement", "fix the login bug")

        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        assert error is None
        mock_insert.assert_called_once()
        mission = mock_insert.call_args[0][1]
        assert "/implement" in mission

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.github_command_handler._try_nlp_classification", return_value=None)
    def test_nlp_fails_falls_through_to_error(
        self, mock_nlp, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_read,
        registry, sample_notification,
    ):
        """NLP returns None → existing error path with help message."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot blahblah",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        config = {"github": {"nickname": "testbot"}}

        success, error = process_single_notification(
            sample_notification, registry, config, None, "testbot",
        )

        assert success is False
        assert "`blahblah`" in error  # "Unknown command `blahblah`"

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    @patch("app.github_command_handler._try_nlp_classification")
    def test_rigid_parse_takes_priority(
        self, mock_nlp, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry, sample_notification, tmp_path,
    ):
        """Rigid parse succeeds → NLP is NOT called."""
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        config = {"github": {"nickname": "testbot", "authorized_users": ["*"]}}

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                sample_notification, registry, config, None, "testbot",
            )

        assert success is True
        mock_nlp.assert_not_called()  # NLP should not be invoked


# ---------------------------------------------------------------------------
# Phase 1: github_context_aware flag on PR-manipulation core skills
# ---------------------------------------------------------------------------


class TestContextAwareCoreSkills:
    """Verify that rebase, recreate, and refactor have github_context_aware=True.

    These tests load the actual SKILL.md files via the registry so that any
    future edit to those files will be caught immediately.
    """

    @pytest.fixture
    def core_registry(self):
        """Registry loaded from the real core skills directory."""
        from pathlib import Path

        skills_dir = Path(__file__).parent.parent / "skills" / "core"
        return SkillRegistry(skills_dir)

    @pytest.mark.parametrize("command_name", ["rebase", "recreate", "refactor"])
    def test_skill_is_context_aware(self, core_registry, command_name):
        """Each PR-manipulation skill must have github_context_aware=True."""
        skill = core_registry.find_by_command(command_name)
        assert skill is not None, f"Skill '{command_name}' not found in core registry"
        assert skill.github_context_aware is True, (
            f"Skill '{command_name}' must have github_context_aware: true in SKILL.md"
        )

    @pytest.mark.parametrize("command_name", ["rebase", "recreate", "refactor"])
    def test_context_included_in_mission(self, core_registry, command_name):
        """When context is provided, it should appear in the built mission."""
        skill = core_registry.find_by_command(command_name)
        assert skill is not None

        notif = {"subject": {"url": f"https://api.github.com/repos/o/r/pulls/42"}}
        context = "please squash into one commit"
        mission = build_mission_from_command(skill, command_name, context, notif, "myproject")

        assert context in mission, (
            f"Mission for '{command_name}' should contain context text"
        )
        assert f"/{command_name}" in mission

    @pytest.mark.parametrize("command_name", ["rebase", "recreate", "refactor"])
    def test_no_context_mission_unchanged(self, core_registry, command_name):
        """Without extra context, mission format should be unchanged."""
        skill = core_registry.find_by_command(command_name)
        assert skill is not None

        notif = {"subject": {"url": f"https://api.github.com/repos/o/r/pulls/42"}}
        mission = build_mission_from_command(skill, command_name, "", notif, "myproject")

        assert mission == f"- [project:myproject] /{command_name} https://github.com/o/r/pull/42 📬"
