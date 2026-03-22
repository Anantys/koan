"""Tests for the /gh_request skill — natural-language GitHub request routing."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skills import Skill, SkillCommand, SkillContext, SkillRegistry

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    """Minimal SkillContext for handler tests."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    (instance_dir / "missions.md").write_text("# Pending\n\n# In Progress\n\n# Done\n")
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="gh_request",
        args="",
    )


@pytest.fixture
def gh_request_skill():
    """A github-enabled gh_request skill."""
    return Skill(
        name="gh_request",
        scope="core",
        description="Route natural-language GitHub requests",
        github_enabled=True,
        github_context_aware=True,
        commands=[SkillCommand(name="gh_request")],
    )


@pytest.fixture
def registry_with_gh_request(gh_request_skill):
    """Registry including gh_request skill."""
    reg = SkillRegistry()
    reg._register(gh_request_skill)
    for name in ("fix", "review", "rebase", "implement"):
        skill = Skill(
            name=name,
            scope="core",
            description=f"{name} skill",
            github_enabled=True,
            github_context_aware=True,
            commands=[SkillCommand(name=name)],
        )
        reg._register(skill)
    return reg


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


class TestGhRequestHandler:
    """Tests for skills/core/gh_request/handler.py."""

    def test_no_args_returns_usage(self, ctx):
        from skills.core.gh_request.handler import handle

        ctx.args = ""
        result = handle(ctx)
        assert "Usage" in result
        assert "gh_request" in result

    def test_url_with_request_text_classifies_and_queues(self, ctx):
        """URL + text → classify → queue specific mission."""
        from skills.core.gh_request.handler import handle

        with patch("skills.core.gh_request.handler.resolve_project_for_repo") as mock_resolve, \
             patch("skills.core.gh_request.handler._classify_request") as mock_classify, \
             patch("app.utils.insert_pending_mission"):
            mock_resolve.return_value = ("/path/to/koan", "koan")
            mock_classify.return_value = ("review", "check the auth logic")
            ctx.args = "https://github.com/owner/repo/pull/42 can you review the auth logic?"

            result = handle(ctx)

        assert "/review" in result
        assert "koan" in result

    def test_classification_fails_queues_generic(self, ctx):
        """When classifier returns None, queue as plain mission (no /gh_request prefix)."""
        from skills.core.gh_request.handler import handle

        with patch("skills.core.gh_request.handler.resolve_project_for_repo") as mock_resolve, \
             patch("skills.core.gh_request.handler._classify_request") as mock_classify, \
             patch("app.utils.insert_pending_mission") as mock_insert:
            mock_resolve.return_value = ("/path/to/koan", "koan")
            mock_classify.return_value = (None, "")
            ctx.args = "https://github.com/owner/repo/pull/42 do something unusual"

            result = handle(ctx)

        assert "queued" in result.lower()
        mock_insert.assert_called_once()
        mission = mock_insert.call_args[0][1]
        # No /gh_request prefix — Claude handles plain text naturally
        assert "/gh_request" not in mission
        assert "https://github.com/owner/repo/pull/42" in mission
        assert "do something unusual" in mission

    def test_no_url_returns_error(self, ctx):
        """Without a URL, can't determine project."""
        from skills.core.gh_request.handler import handle

        ctx.args = "fix the login bug"
        result = handle(ctx)
        assert "Could not determine project" in result

    def test_unknown_repo_returns_error(self, ctx):
        """URL pointing to unknown repo returns project-not-found error."""
        from skills.core.gh_request.handler import handle

        with patch("skills.core.gh_request.handler.resolve_project_for_repo") as mock_resolve, \
             patch("skills.core.gh_request.handler.format_project_not_found_error") as mock_err:
            mock_resolve.return_value = (None, None)
            mock_err.return_value = "❌ Could not find local project"
            ctx.args = "https://github.com/unknown/repo/pull/1 review this"
            result = handle(ctx)
        assert "Could not find" in result


class TestClassifyRequest:
    """Tests for _classify_request URL-type validation."""

    def _run_classify(self, text, project, url, classify_result):
        """Helper to run _classify_request with all dependencies mocked."""
        from skills.core.gh_request.handler import _classify_request

        with patch("app.skills.build_registry") as mock_br, \
             patch("app.github_command_handler.get_github_enabled_commands_with_descriptions") as mock_cmds, \
             patch("app.utils.get_known_projects") as mock_kp, \
             patch("app.github_intent.classify_intent") as mock_ci:
            mock_br.return_value = MagicMock()
            mock_cmds.return_value = [("fix", "Fix issue"), ("review", "Review PR"), ("rebase", "Rebase PR")]
            mock_kp.return_value = [("koan", "/path/to/koan")]
            mock_ci.return_value = classify_result

            return _classify_request(text, project, url)

    def test_fix_with_pr_url_returns_none(self):
        """NLP says 'fix' but URL is a PR → should NOT forward to /fix."""
        command, context = self._run_classify(
            "fix the login bug", "koan",
            "https://github.com/o/r/pull/42",
            {"command": "fix", "context": "the login bug"},
        )
        assert command is None

    def test_review_with_pr_url_succeeds(self):
        """NLP says 'review' + PR URL → should forward."""
        command, context = self._run_classify(
            "please review this", "koan",
            "https://github.com/o/r/pull/42",
            {"command": "review", "context": "check auth"},
        )
        assert command == "review"
        assert context == "check auth"

    def test_rebase_with_issue_url_returns_none(self):
        """NLP says 'rebase' but URL is an issue → should NOT forward."""
        command, context = self._run_classify(
            "rebase this", "koan",
            "https://github.com/o/r/issues/10",
            {"command": "rebase", "context": ""},
        )
        assert command is None

    def test_fix_with_issue_url_succeeds(self):
        """NLP says 'fix' + issue URL → should forward."""
        command, context = self._run_classify(
            "fix this bug", "koan",
            "https://github.com/o/r/issues/10",
            {"command": "fix", "context": "the login bug"},
        )
        assert command == "fix"

    def test_classification_returns_none_on_no_match(self):
        """Classifier returns no command → (None, "")."""
        command, context = self._run_classify(
            "do something weird", "koan",
            "https://github.com/o/r/pull/42",
            {"command": None, "context": ""},
        )
        assert command is None


# ---------------------------------------------------------------------------
# GitHub command handler routing tests
# ---------------------------------------------------------------------------


class TestGhRequestRouting:
    """Tests for /gh_request routing in github_command_handler.py."""

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    @patch("app.github_reply.extract_mention_text")
    def test_nlp_enabled_routes_to_gh_request(
        self, mock_extract, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry_with_gh_request, tmp_path,
    ):
        """When natural_language=true, unrecognized commands route to /gh_request."""
        from app.github_command_handler import process_single_notification

        notification = {
            "id": "12345",
            "reason": "mention",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
                "type": "PullRequest",
            },
            "repository": {"full_name": "sukria/koan"},
        }
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot can you take a look at this PR?",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        mock_extract.return_value = "can you take a look at this PR?"

        config = {
            "github": {
                "nickname": "testbot",
                "authorized_users": ["*"],
                "natural_language": True,
            },
        }

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                notification, registry_with_gh_request, config, None, "testbot",
            )

        assert success is True
        assert error is None
        mock_insert.assert_called_once()
        mission = mock_insert.call_args[0][1]
        assert "/gh_request" in mission
        assert "can you take a look at this PR?" in mission

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.github_command_handler._try_nlp_classification", return_value=None)
    def test_nlp_disabled_uses_legacy_path(
        self, mock_nlp, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_read,
        registry_with_gh_request,
    ):
        """Without natural_language=true, uses legacy NLP classification."""
        from app.github_command_handler import process_single_notification

        notification = {
            "id": "12345",
            "reason": "mention",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
                "type": "PullRequest",
            },
            "repository": {"full_name": "sukria/koan"},
        }
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot blahblah",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }
        config = {"github": {"nickname": "testbot"}}

        success, error = process_single_notification(
            notification, registry_with_gh_request, config, None, "testbot",
        )

        assert success is False
        mock_nlp.assert_called_once()
        assert "`blahblah`" in error

    @patch("app.github_command_handler.mark_notification_read")
    @patch("app.github_command_handler.add_reaction", return_value=True)
    @patch("app.github_command_handler.check_user_permission", return_value=True)
    @patch("app.github_command_handler.check_already_processed", return_value=False)
    @patch("app.github_command_handler.is_self_mention", return_value=False)
    @patch("app.github_command_handler.is_notification_stale", return_value=False)
    @patch("app.github_command_handler.get_comment_from_notification")
    @patch("app.github_command_handler.resolve_project_from_notification")
    @patch("app.utils.insert_pending_mission")
    def test_recognized_command_still_works_with_nlp_enabled(
        self, mock_insert, mock_resolve, mock_get_comment,
        mock_stale, mock_self, mock_processed, mock_perm,
        mock_react, mock_read, registry_with_gh_request, tmp_path,
    ):
        """Recognized commands bypass NLP even when natural_language=true."""
        from app.github_command_handler import process_single_notification

        notification = {
            "id": "12345",
            "reason": "mention",
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
                "type": "PullRequest",
            },
            "repository": {"full_name": "sukria/koan"},
        }
        mock_resolve.return_value = ("koan", "sukria", "koan")
        mock_get_comment.return_value = {
            "id": 99999,
            "body": "@testbot rebase",
            "user": {"login": "alice"},
            "url": "https://api.github.com/repos/sukria/koan/issues/comments/99999",
        }

        config = {
            "github": {
                "nickname": "testbot",
                "authorized_users": ["*"],
                "natural_language": True,
            },
        }

        with patch.dict("os.environ", {"KOAN_ROOT": str(tmp_path)}):
            success, error = process_single_notification(
                notification, registry_with_gh_request, config, None, "testbot",
            )

        assert success is True
        mock_insert.assert_called_once()
        mission = mock_insert.call_args[0][1]
        assert "/rebase" in mission
        assert "/gh_request" not in mission
