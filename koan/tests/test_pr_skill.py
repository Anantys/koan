"""Tests for the /pr core skill handler."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from app.skills import SkillContext

# The PR handler imports lazily inside handle():
#   from app.github_url_parser import parse_pr_url → patch at app.github_url_parser.parse_pr_url
#   from app.utils import resolve_project_path     → patch at app.utils.resolve_project_path
#   from app.pr_review import run_pr_review        → patch at app.pr_review.run_pr_review
#   from app.utils import get_known_projects       → patch at app.utils.get_known_projects

_P_PARSE = "app.github_url_parser.parse_pr_url"
_P_RESOLVE = "app.utils.resolve_project_path"
_P_REVIEW = "app.pr_review.run_pr_review"
_P_KNOWN = "app.utils.get_known_projects"


def _make_ctx(args="", instance_dir=None, send_message=None):
    """Create a minimal SkillContext for testing."""
    ctx = MagicMock(spec=SkillContext)
    ctx.command_name = "pr"
    ctx.args = args
    ctx.instance_dir = instance_dir or Path("/tmp/test-instance")
    ctx.send_message = send_message
    return ctx


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestPrInputValidation:
    """Test argument parsing and URL validation."""

    def test_no_args_shows_usage(self):
        """No arguments returns usage instructions."""
        from skills.core.pr.handler import handle
        ctx = _make_ctx("")
        result = handle(ctx)
        assert "Usage" in result
        assert "/pr" in result

    def test_invalid_url_rejected(self):
        """Non-GitHub URL is rejected."""
        from skills.core.pr.handler import handle
        ctx = _make_ctx("https://gitlab.com/owner/repo/merge_requests/1")
        result = handle(ctx)
        assert "No valid GitHub PR URL" in result

    def test_plain_text_rejected(self):
        """Plain text without URL is rejected."""
        from skills.core.pr.handler import handle
        ctx = _make_ctx("fix the bug please")
        result = handle(ctx)
        assert "No valid GitHub PR URL" in result

    def test_valid_url_extracted(self):
        """Valid GitHub PR URL is extracted from args."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE) as mock_parse, \
             patch(_P_RESOLVE, return_value="/path/to/repo"), \
             patch(_P_REVIEW, return_value=(True, "OK")):
            mock_parse.return_value = ("owner", "repo", 42)
            ctx = _make_ctx("https://github.com/owner/repo/pull/42", send_message=MagicMock())
            handle(ctx)
            mock_parse.assert_called_once_with("https://github.com/owner/repo/pull/42")

    def test_url_with_fragment_stripped(self):
        """URL fragment (#discussion_r123) is stripped."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE) as mock_parse, \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "OK")):
            mock_parse.return_value = ("owner", "repo", 42)
            ctx = _make_ctx("https://github.com/owner/repo/pull/42#discussion_r123", send_message=MagicMock())
            handle(ctx)
            mock_parse.assert_called_once_with("https://github.com/owner/repo/pull/42")

    def test_url_embedded_in_text(self):
        """URL is extracted even when surrounded by text."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE) as mock_parse, \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "OK")):
            mock_parse.return_value = ("owner", "repo", 7)
            ctx = _make_ctx("please review https://github.com/owner/repo/pull/7 thanks", send_message=MagicMock())
            handle(ctx)
            mock_parse.assert_called_once_with("https://github.com/owner/repo/pull/7")

    def test_http_url_works(self):
        """HTTP (non-HTTPS) URLs also work."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE) as mock_parse, \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "OK")):
            mock_parse.return_value = ("owner", "repo", 1)
            ctx = _make_ctx("http://github.com/owner/repo/pull/1", send_message=MagicMock())
            handle(ctx)
            mock_parse.assert_called_once()


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

class TestPrProjectResolution:
    """Test project path lookup from repo name."""

    def test_unknown_project_returns_error(self):
        """Unrecognized repo name returns error with known projects."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "unknown-repo", 1)), \
             patch(_P_RESOLVE, return_value=None), \
             patch(_P_KNOWN, return_value=[("koan", "/p1"), ("webapp", "/p2")]):
            ctx = _make_ctx("https://github.com/owner/unknown-repo/pull/1")
            result = handle(ctx)
        assert "Could not find" in result
        assert "unknown-repo" in result
        assert "koan" in result
        assert "webapp" in result

    def test_known_project_proceeds(self):
        """Known project proceeds to review pipeline."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("sukria", "koan", 42)), \
             patch(_P_RESOLVE, return_value="/Users/test/koan"), \
             patch(_P_REVIEW, return_value=(True, "All good")) as mock_review:
            ctx = _make_ctx("https://github.com/sukria/koan/pull/42", send_message=MagicMock())
            handle(ctx)
            mock_review.assert_called_once()

    def test_no_known_projects_shows_none(self):
        """When no projects are configured, shows 'none'."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "repo", 1)), \
             patch(_P_RESOLVE, return_value=None), \
             patch(_P_KNOWN, return_value=[]):
            ctx = _make_ctx("https://github.com/owner/repo/pull/1")
            result = handle(ctx)
        assert "none" in result


# ---------------------------------------------------------------------------
# Review pipeline invocation
# ---------------------------------------------------------------------------

class TestPrReviewPipeline:
    """Test the PR review pipeline interaction."""

    def test_successful_review_sends_message(self):
        """Successful review sends confirmation via send_message."""
        from skills.core.pr.handler import handle
        send = MagicMock()
        with patch(_P_PARSE, return_value=("owner", "repo", 5)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "Rebased and pushed")):
            ctx = _make_ctx("https://github.com/owner/repo/pull/5", send_message=send)
            result = handle(ctx)

        # Two sends: initial notification + success report
        assert send.call_count == 2
        success_msg = send.call_args_list[1][0][0]
        assert "PR #5" in success_msg
        assert result is None  # already sent via send_message

    def test_successful_review_returns_none(self):
        """Successful review returns None (already sent via callback)."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "repo", 5)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "OK")):
            ctx = _make_ctx("https://github.com/owner/repo/pull/5", send_message=MagicMock())
            result = handle(ctx)
        assert result is None

    def test_failed_review_returns_error(self):
        """Failed review returns error message string."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "repo", 5)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(False, "Rebase conflict")):
            ctx = _make_ctx("https://github.com/owner/repo/pull/5", send_message=MagicMock())
            result = handle(ctx)
        assert "failed" in result
        assert "Rebase conflict" in result

    def test_review_exception_returns_error(self):
        """Exception during review returns error message."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "repo", 5)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, side_effect=RuntimeError("git crash")):
            ctx = _make_ctx("https://github.com/owner/repo/pull/5", send_message=MagicMock())
            result = handle(ctx)
        assert "error" in result.lower()
        assert "git crash" in result

    def test_parse_url_error_returns_message(self):
        """ValueError from parse_pr_url is returned as message."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, side_effect=ValueError("Invalid PR URL")):
            ctx = _make_ctx("https://github.com/owner/repo/pull/999")
            result = handle(ctx)
        assert "Invalid PR URL" in result

    def test_sends_initial_notification(self):
        """Sends a 'starting' notification before review begins."""
        from skills.core.pr.handler import handle
        send = MagicMock()
        with patch(_P_PARSE, return_value=("sukria", "koan", 42)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "Done")):
            ctx = _make_ctx("https://github.com/sukria/koan/pull/42", send_message=send)
            handle(ctx)

        first_call = send.call_args_list[0][0][0]
        assert "#42" in first_call
        assert "sukria/koan" in first_call

    def test_long_summary_truncated(self):
        """Very long review summary is truncated to 400 chars."""
        from skills.core.pr.handler import handle
        long_summary = "x" * 600
        with patch(_P_PARSE, return_value=("owner", "repo", 1)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(False, long_summary)):
            ctx = _make_ctx("https://github.com/owner/repo/pull/1", send_message=MagicMock())
            result = handle(ctx)
        # Result includes prefix text, check that summary part is truncated
        assert len(result) < 500

    def test_skill_dir_passed_to_review(self):
        """skill_dir is passed pointing to the /pr skill directory."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "repo", 1)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "OK")) as mock_review:
            ctx = _make_ctx("https://github.com/owner/repo/pull/1", send_message=MagicMock())
            handle(ctx)

        kwargs = mock_review.call_args[1]
        skill_dir = kwargs["skill_dir"]
        assert "pr" in str(skill_dir)

    def test_no_send_message_successful(self):
        """Without send_message, successful review still returns None."""
        from skills.core.pr.handler import handle
        with patch(_P_PARSE, return_value=("owner", "repo", 1)), \
             patch(_P_RESOLVE, return_value="/path"), \
             patch(_P_REVIEW, return_value=(True, "OK")):
            ctx = _make_ctx("https://github.com/owner/repo/pull/1", send_message=None)
            result = handle(ctx)
        assert result is None
