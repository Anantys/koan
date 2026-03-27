"""Tests for skill handlers that had zero test coverage.

Covers: shutdown, review, implement, refactor, email.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext


def _make_ctx(tmp_path, command_name="test", args="", missions_content=None):
    """Create a minimal SkillContext for testing."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    if missions_content is not None:
        (instance_dir / "missions.md").write_text(missions_content)
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name=command_name,
        args=args,
    )


# ---------------------------------------------------------------------------
# shutdown handler
# ---------------------------------------------------------------------------


class TestShutdownHandler:
    def test_calls_request_shutdown(self, tmp_path):
        from skills.core.shutdown.handler import handle

        with patch("skills.core.shutdown.handler.request_shutdown") as mock_shutdown:
            ctx = _make_ctx(tmp_path, command_name="shutdown")
            result = handle(ctx)

        mock_shutdown.assert_called_once_with(str(tmp_path))
        assert "Shutdown requested" in result

    def test_returns_string(self, tmp_path):
        from skills.core.shutdown.handler import handle

        with patch("skills.core.shutdown.handler.request_shutdown"):
            result = handle(_make_ctx(tmp_path))
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# review handler
# ---------------------------------------------------------------------------


class TestReviewHandler:
    def test_delegates_to_handle_github_skill(self, tmp_path):
        from skills.core.review.handler import handle

        with patch("skills.core.review.handler.handle_github_skill",
                    return_value="Review queued for #42") as mock_skill:
            ctx = _make_ctx(
                tmp_path, command_name="review",
                args="https://github.com/owner/repo/pull/42",
            )
            result = handle(ctx)

        mock_skill.assert_called_once()
        assert result == "Review queued for #42"
        kwargs = mock_skill.call_args
        assert kwargs[1]["command"] == "review"
        assert kwargs[1]["url_type"] == "pr-or-issue"

    def test_passes_ctx_through(self, tmp_path):
        from skills.core.review.handler import handle

        ctx = _make_ctx(tmp_path, command_name="review", args="some-url")
        with patch("skills.core.review.handler.handle_github_skill",
                    return_value="ok") as mock_skill:
            handle(ctx)

        assert mock_skill.call_args[0][0] is ctx


# ---------------------------------------------------------------------------
# implement handler
# ---------------------------------------------------------------------------


class TestImplementHandler:
    def test_delegates_to_handle_github_skill(self, tmp_path):
        from skills.core.implement.handler import handle

        with patch("skills.core.implement.handler.handle_github_skill",
                    return_value="Implementation queued") as mock_skill:
            ctx = _make_ctx(
                tmp_path, command_name="implement",
                args="https://github.com/owner/repo/issues/42",
            )
            result = handle(ctx)

        mock_skill.assert_called_once()
        assert result == "Implementation queued"
        kwargs = mock_skill.call_args
        assert kwargs[1]["command"] == "implement"
        assert kwargs[1]["url_type"] == "pr-or-issue"

    def test_passes_ctx_through(self, tmp_path):
        from skills.core.implement.handler import handle

        ctx = _make_ctx(tmp_path, command_name="implement", args="url")
        with patch("skills.core.implement.handler.handle_github_skill",
                    return_value="ok") as mock_skill:
            handle(ctx)

        assert mock_skill.call_args[0][0] is ctx


# ---------------------------------------------------------------------------
# refactor handler
# ---------------------------------------------------------------------------

MISSIONS_TEMPLATE = (
    "# Missions\n\n## Ideas\n\n## Pending\n\n## In Progress\n\n## Done\n"
)


class TestRefactorHandler:
    def test_no_args_returns_usage(self, tmp_path):
        from skills.core.refactor.handler import handle

        ctx = _make_ctx(tmp_path, command_name="refactor", args="")
        result = handle(ctx)
        assert "Usage:" in result
        assert "/refactor" in result

    def test_github_url_delegates_to_handle_github_skill(self, tmp_path):
        from skills.core.refactor.handler import handle

        with patch("skills.core.refactor.handler.extract_github_url",
                    return_value=("owner", "repo", "42")), \
             patch("skills.core.refactor.handler.handle_github_skill",
                    return_value="Refactor queued for #42") as mock_skill:
            ctx = _make_ctx(
                tmp_path, command_name="refactor",
                args="https://github.com/owner/repo/pull/42",
            )
            result = handle(ctx)

        mock_skill.assert_called_once()
        assert result == "Refactor queued for #42"

    def test_file_path_inserts_mission(self, tmp_path):
        from skills.core.refactor.handler import handle

        ctx = _make_ctx(
            tmp_path, command_name="refactor",
            args="src/utils.py",
            missions_content=MISSIONS_TEMPLATE,
        )
        with patch("skills.core.refactor.handler.extract_github_url",
                    return_value=None):
            with patch("app.utils.insert_pending_mission") as mock_insert:
                result = handle(ctx)

        mock_insert.assert_called_once()
        call_args = mock_insert.call_args[0]
        assert "refactor" in call_args[1]
        assert "src/utils.py" in call_args[1]
        assert "src/utils.py" in result

    def test_non_url_treated_as_file_path(self, tmp_path):
        from skills.core.refactor.handler import handle

        ctx = _make_ctx(
            tmp_path, command_name="refactor",
            args="koan/app/run.py",
            missions_content=MISSIONS_TEMPLATE,
        )
        with patch("skills.core.refactor.handler.extract_github_url",
                    return_value=None):
            with patch("app.utils.insert_pending_mission"):
                result = handle(ctx)

        assert "koan/app/run.py" in result

    def test_whitespace_only_returns_usage(self, tmp_path):
        from skills.core.refactor.handler import handle

        ctx = _make_ctx(tmp_path, command_name="refactor", args="   ")
        result = handle(ctx)
        assert "Usage:" in result


# ---------------------------------------------------------------------------
# email handler
# ---------------------------------------------------------------------------


class TestEmailHandler:
    def test_status_default_no_args(self, tmp_path):
        from skills.core.email.handler import handle

        mock_stats = {
            "enabled": True,
            "sent_today": 3,
            "max_per_day": 10,
            "remaining": 7,
            "last_sent": None,
        }
        with patch("app.email_notify.get_email_stats",
                    return_value=mock_stats), \
             patch("app.email_notify.can_send_email",
                    return_value=(True, "")):
            ctx = _make_ctx(tmp_path, command_name="email", args="")
            result = handle(ctx)

        assert "Email Status" in result
        assert "3/10" in result
        assert "7" in result

    def test_status_explicit(self, tmp_path):
        from skills.core.email.handler import handle

        mock_stats = {
            "enabled": True,
            "sent_today": 0,
            "max_per_day": 5,
            "remaining": 5,
            "last_sent": None,
        }
        with patch("app.email_notify.get_email_stats",
                    return_value=mock_stats), \
             patch("app.email_notify.can_send_email",
                    return_value=(True, "")):
            ctx = _make_ctx(tmp_path, command_name="email", args="status")
            result = handle(ctx)

        assert "Email Status" in result

    def test_status_disabled(self, tmp_path):
        from skills.core.email.handler import handle

        with patch("app.email_notify.get_email_stats",
                    return_value={"enabled": False}):
            ctx = _make_ctx(tmp_path, command_name="email", args="")
            result = handle(ctx)

        assert "disabled" in result

    def test_status_with_warning(self, tmp_path):
        from skills.core.email.handler import handle

        mock_stats = {
            "enabled": True,
            "sent_today": 10,
            "max_per_day": 10,
            "remaining": 0,
            "last_sent": None,
        }
        with patch("app.email_notify.get_email_stats",
                    return_value=mock_stats), \
             patch("app.email_notify.can_send_email",
                    return_value=(False, "Daily limit reached")):
            ctx = _make_ctx(tmp_path, command_name="email", args="")
            result = handle(ctx)

        assert "Daily limit reached" in result

    def test_status_with_last_sent(self, tmp_path):
        from skills.core.email.handler import handle

        mock_stats = {
            "enabled": True,
            "sent_today": 1,
            "max_per_day": 10,
            "remaining": 9,
            "last_sent": 1708272000.0,
        }
        with patch("app.email_notify.get_email_stats",
                    return_value=mock_stats), \
             patch("app.email_notify.can_send_email",
                    return_value=(True, "")):
            ctx = _make_ctx(tmp_path, command_name="email", args="")
            result = handle(ctx)

        assert "Last sent:" in result

    def test_test_email_success(self, tmp_path):
        from skills.core.email.handler import handle

        with patch("app.email_notify.send_owner_email",
                    return_value=True):
            ctx = _make_ctx(tmp_path, command_name="email", args="test")
            result = handle(ctx)

        assert "Test email sent" in result

    def test_test_email_failure(self, tmp_path):
        from skills.core.email.handler import handle

        with patch("app.email_notify.send_owner_email",
                    return_value=False), \
             patch("app.email_notify.can_send_email",
                    return_value=(False, "SMTP not configured")):
            ctx = _make_ctx(tmp_path, command_name="email", args="test")
            result = handle(ctx)

        assert "failed" in result
        assert "SMTP not configured" in result

    def test_unknown_subcommand_returns_help(self, tmp_path):
        from skills.core.email.handler import handle

        ctx = _make_ctx(tmp_path, command_name="email", args="unknown")
        result = handle(ctx)
        assert "/email" in result
        assert "test" in result
