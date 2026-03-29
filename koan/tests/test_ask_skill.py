"""Tests for the /ask skill."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# SKILL.md validation
# ---------------------------------------------------------------------------


class TestAskSkillMd:
    """Verify SKILL.md has all required fields."""

    def _parse_skill_md(self):
        skill_md = Path(__file__).parent.parent / "skills" / "core" / "ask" / "SKILL.md"
        assert skill_md.exists(), f"SKILL.md not found at {skill_md}"
        content = skill_md.read_text()

        # Extract frontmatter
        parts = content.split("---")
        assert len(parts) >= 3, "SKILL.md missing frontmatter delimiters"
        frontmatter = parts[1]

        parsed = {}
        for line in frontmatter.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                key, _, value = line.partition(":")
                parsed[key.strip()] = value.strip()
        return parsed

    def test_has_group(self):
        fields = self._parse_skill_md()
        assert "group" in fields, "SKILL.md missing 'group' field"
        assert fields["group"] == "pr"

    def test_github_enabled(self):
        fields = self._parse_skill_md()
        assert fields.get("github_enabled") == "true"

    def test_worker_true(self):
        fields = self._parse_skill_md()
        assert fields.get("worker") == "true"

    def test_has_handler(self):
        fields = self._parse_skill_md()
        assert fields.get("handler") == "handler.py"

    def test_no_hyphen_in_name(self):
        fields = self._parse_skill_md()
        assert "-" not in fields.get("name", ""), "Skill name must not contain hyphens"


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------


class TestExtractCommentId:
    """Test comment ID extraction from URLs."""

    def setup_method(self):
        from skills.core.ask import handler
        self.extract = handler._extract_comment_id

    def test_issue_comment(self):
        url = "https://github.com/owner/repo/issues/42#issuecomment-123456"
        assert self.extract(url) == "123456"

    def test_pr_review_comment(self):
        url = "https://github.com/owner/repo/pull/42#discussion_r789012"
        assert self.extract(url) == "789012"

    def test_no_fragment(self):
        url = "https://github.com/owner/repo/pull/42"
        assert self.extract(url) is None

    def test_unknown_fragment(self):
        url = "https://github.com/owner/repo/issues/42#other-stuff"
        assert self.extract(url) is None


class TestParseGithubUrl:
    """Test GitHub URL parsing."""

    def setup_method(self):
        from skills.core.ask import handler
        self.parse = handler._parse_github_url

    def test_pull_url(self):
        url = "https://github.com/sukria/koan/pull/42"
        assert self.parse(url) == ("sukria", "koan", "42")

    def test_issues_url(self):
        url = "https://github.com/sukria/koan/issues/10"
        assert self.parse(url) == ("sukria", "koan", "10")

    def test_url_with_comment_fragment(self):
        url = "https://github.com/sukria/koan/issues/10#issuecomment-999"
        assert self.parse(url) == ("sukria", "koan", "10")

    def test_invalid_url(self):
        assert self.parse("not-a-url") is None

    def test_bare_repo_url(self):
        assert self.parse("https://github.com/sukria/koan") is None


class TestExtractCommentUrl:
    """Test GitHub URL extraction from args."""

    def setup_method(self):
        from skills.core.ask import handler
        self.extract = handler._extract_comment_url

    def test_extracts_url(self):
        args = "https://github.com/owner/repo/issues/42#issuecomment-123"
        assert self.extract(args) == "https://github.com/owner/repo/issues/42#issuecomment-123"

    def test_no_url(self):
        assert self.extract("no url here") is None

    def test_url_with_surrounding_text(self):
        url = "https://github.com/owner/repo/pull/5#issuecomment-99"
        result = self.extract(f"see {url} for details")
        assert result == url


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------


class TestAskHandlerUsage:
    """Test /ask handler returns usage when called without arguments."""

    def _make_ctx(self, args=""):
        ctx = MagicMock()
        ctx.args = args
        return ctx

    @patch("app.utils.resolve_project_path")
    def test_no_args_returns_usage(self, _mock_resolve):
        from skills.core.ask.handler import handle
        ctx = self._make_ctx("")
        result = handle(ctx)
        assert "Usage:" in result
        assert "/ask" in result

    @patch("app.utils.resolve_project_path")
    def test_no_url_returns_error(self, _mock_resolve):
        from skills.core.ask.handler import handle
        ctx = self._make_ctx("what is this?")
        result = handle(ctx)
        assert "❌" in result
        assert "URL" in result.lower() or "url" in result.lower()


class TestAskHandlerFlow:
    """Test /ask handler full flow with mocked dependencies."""

    def _make_ctx(self, args):
        ctx = MagicMock()
        ctx.args = args
        return ctx

    @patch("app.utils.resolve_project_path", return_value="/path/to/project")
    @patch("app.utils.project_name_for_path", return_value="myproject")
    @patch("app.github_reply.post_reply", return_value=True)
    @patch("app.github_reply.clean_reply", return_value="Here is my answer.")
    @patch("app.cli_provider.run_command", return_value="Here is my answer.")
    @patch("app.github_reply.fetch_thread_context")
    @patch("app.github.api")
    def test_successful_flow(
        self,
        mock_api,
        mock_fetch_ctx,
        mock_run_command,
        mock_clean,
        mock_post,
        mock_name,
        mock_resolve,
    ):
        import json as _json
        from skills.core.ask.handler import handle

        # Mock comment fetch
        mock_api.return_value = _json.dumps({
            "body": "Why does this test fail?",
            "user": {"login": "atoomic"},
        })
        mock_fetch_ctx.return_value = {
            "title": "My PR",
            "body": "Fix something",
            "comments": [],
            "is_pr": True,
            "diff_summary": "",
        }

        url = "https://github.com/sukria/koan/issues/42#issuecomment-123456"
        ctx = self._make_ctx(url)
        result = handle(ctx)

        assert "✅" in result
        assert "sukria/koan" in result
        mock_run_command.assert_called_once()
        mock_post.assert_called_once_with("sukria", "koan", "42", "Here is my answer.")

    @patch("app.utils.resolve_project_path", return_value="/path/to/project")
    @patch("app.utils.project_name_for_path", return_value="myproject")
    def test_no_comment_fragment_returns_error(self, _mock_name, _mock_resolve):
        from skills.core.ask.handler import handle

        url = "https://github.com/sukria/koan/issues/42"
        ctx = self._make_ctx(url)
        result = handle(ctx)
        assert "❌" in result
        assert "fragment" in result.lower()

    @patch("app.utils.resolve_project_path", return_value=None)
    def test_unknown_project_returns_error(self, _mock_resolve):
        from skills.core.ask.handler import handle

        url = "https://github.com/unknown/repo/issues/5#issuecomment-1"
        ctx = self._make_ctx(url)
        result = handle(ctx)
        assert "❌" in result

    @patch("app.utils.resolve_project_path", return_value="/path/to/project")
    @patch("app.utils.project_name_for_path", return_value="myproject")
    @patch("app.github.api", side_effect=RuntimeError("not found"))
    def test_comment_not_found_returns_error(self, _mock_api, _mock_name, _mock_resolve):
        from skills.core.ask.handler import handle

        url = "https://github.com/sukria/koan/issues/42#issuecomment-999"
        ctx = self._make_ctx(url)
        result = handle(ctx)
        assert "❌" in result
        assert "comment" in result.lower() or "available" in result.lower()

    @patch("app.utils.resolve_project_path", return_value="/path/to/project")
    @patch("app.utils.project_name_for_path", return_value="myproject")
    @patch("app.github_reply.post_reply", return_value=False)
    @patch("app.github_reply.clean_reply", return_value="An answer.")
    @patch("app.cli_provider.run_command", return_value="An answer.")
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "", "body": "", "comments": [], "is_pr": False, "diff_summary": ""
    })
    @patch("app.github.api")
    def test_post_failure_returns_error(
        self, mock_api, _fetch_ctx, _run_command, _clean, _post, _name, _resolve
    ):
        import json as _json
        from skills.core.ask.handler import handle

        mock_api.return_value = _json.dumps({
            "body": "What does this do?",
            "user": {"login": "user1"},
        })

        url = "https://github.com/sukria/koan/issues/42#issuecomment-123"
        ctx = self._make_ctx(url)
        result = handle(ctx)
        assert "❌" in result
        assert "post" in result.lower()

    @patch("app.utils.resolve_project_path", return_value="/path/to/project")
    @patch("app.utils.project_name_for_path", return_value="myproject")
    @patch("app.cli_provider.run_command", side_effect=RuntimeError("CLI failed"))
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "T", "body": "", "comments": [], "is_pr": False, "diff_summary": ""
    })
    @patch("app.github.api")
    def test_generate_reply_runtime_error_returns_error(
        self, mock_api, _fetch_ctx, _run_command, _name, _resolve
    ):
        """RuntimeError from run_command should be caught, not crash."""
        import json as _json
        from skills.core.ask.handler import handle

        mock_api.return_value = _json.dumps({
            "body": "Why does this fail?",
            "user": {"login": "user1"},
        })

        url = "https://github.com/sukria/koan/issues/42#issuecomment-123"
        ctx = self._make_ctx(url)
        result = handle(ctx)
        assert "❌" in result
        assert "generate" in result.lower() or "failed" in result.lower()


# ---------------------------------------------------------------------------
# build_mission_from_command — ask-specific URL override
# ---------------------------------------------------------------------------


class TestBuildMissionFromCommandAsk:
    """Test that build_mission_from_command uses comment_url for /ask."""

    def _make_skill(self, context_aware=False):
        from app.skills import Skill, SkillCommand
        return Skill(
            name="ask",
            scope="core",
            description="Ask a question",
            github_enabled=True,
            github_context_aware=context_aware,
            commands=[SkillCommand(name="ask")],
        )

    def _make_notification(self):
        return {
            "subject": {
                "url": "https://api.github.com/repos/sukria/koan/pulls/42",
            }
        }

    def test_comment_url_overrides_subject_url(self):
        from app.github_command_handler import build_mission_from_command

        skill = self._make_skill()
        notif = self._make_notification()
        comment_url = "https://github.com/sukria/koan/issues/42#issuecomment-789"

        mission = build_mission_from_command(
            skill, "ask", "why does it fail?", notif, "koan",
            comment_url=comment_url,
        )
        assert mission == f"- [project:koan] /ask {comment_url} 📬"

    def test_comment_url_excludes_question_text(self):
        from app.github_command_handler import build_mission_from_command

        skill = self._make_skill(context_aware=True)
        notif = self._make_notification()
        comment_url = "https://github.com/sukria/koan/issues/42#issuecomment-789"

        mission = build_mission_from_command(
            skill, "ask", "why does it fail?", notif, "koan",
            comment_url=comment_url,
        )
        # Question text must NOT appear in mission
        assert "why does it fail?" not in mission
        assert comment_url in mission

    def test_without_comment_url_uses_subject_url(self):
        from app.github_command_handler import build_mission_from_command

        skill = self._make_skill()
        notif = self._make_notification()

        mission = build_mission_from_command(
            skill, "ask", "", notif, "koan",
        )
        # Falls back to normal behaviour: PR URL from subject
        assert "https://github.com/sukria/koan/pull/42" in mission
        assert "📬" in mission


# ---------------------------------------------------------------------------
# ask_runner tests
# ---------------------------------------------------------------------------


class TestAskRunnerAutoDiscovery:
    """Verify skill_dispatch auto-discovers ask_runner."""

    def test_discover_runner_module(self):
        from app.skill_dispatch import _discover_runner_module
        module = _discover_runner_module("ask")
        assert module == "skills.core.ask.ask_runner"


class TestAskRunnerCli:
    """Test ask_runner main() argument parsing."""

    def test_missing_context_file_exits_1(self, tmp_path):
        from skills.core.ask.ask_runner import main
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--project-path", str(tmp_path),
                "--project-name", "test",
                "--instance-dir", str(tmp_path),
            ])
        assert exc_info.value.code == 1

    def test_empty_context_file_exits_1(self, tmp_path):
        ctx_file = tmp_path / "url.txt"
        ctx_file.write_text("")
        from skills.core.ask.ask_runner import main
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--project-path", str(tmp_path),
                "--project-name", "test",
                "--instance-dir", str(tmp_path),
                "--context-file", str(ctx_file),
            ])
        assert exc_info.value.code == 1


class TestRunAskFlow:
    """Test run_ask function with mocked GitHub dependencies."""

    @patch("app.github_reply.post_reply", return_value=True)
    @patch("app.github_reply.clean_reply", return_value="Here is my answer.")
    @patch("app.cli_provider.run_command", return_value="Here is my answer.")
    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "Test PR", "body": "Fix", "comments": [], "is_pr": True, "diff_summary": "",
    })
    @patch("app.github.api")
    def test_successful_run(
        self, mock_api, _fetch_ctx, _run_cmd, _clean, mock_post, tmp_path,
    ):
        import json as _json
        from skills.core.ask.ask_runner import run_ask

        mock_api.return_value = _json.dumps({
            "body": "Why does this test fail?",
            "user": {"login": "atoomic"},
        })

        success, summary = run_ask(
            comment_url="https://github.com/sukria/koan/issues/42#issuecomment-123456",
            project_path=str(tmp_path),
            project_name="koan",
            instance_dir=str(tmp_path),
        )

        assert success is True
        assert "Reply posted" in summary
        assert "sukria/koan#42" in summary
        mock_post.assert_called_once_with("sukria", "koan", "42", "Here is my answer.")

    def test_invalid_url(self, tmp_path):
        from skills.core.ask.ask_runner import run_ask

        success, summary = run_ask(
            comment_url="not a url",
            project_path=str(tmp_path),
            project_name="koan",
            instance_dir=str(tmp_path),
        )
        assert success is False

    def test_url_without_fragment(self, tmp_path):
        from skills.core.ask.ask_runner import run_ask

        success, summary = run_ask(
            comment_url="https://github.com/sukria/koan/issues/42",
            project_path=str(tmp_path),
            project_name="koan",
            instance_dir=str(tmp_path),
        )
        assert success is False
        assert "fragment" in summary.lower()

    @patch("app.github_reply.fetch_thread_context", return_value={
        "title": "", "body": "", "comments": [], "is_pr": False, "diff_summary": "",
    })
    @patch("app.github.api", side_effect=RuntimeError("not found"))
    def test_comment_not_found(self, _api, _fetch, tmp_path):
        from skills.core.ask.ask_runner import run_ask

        success, summary = run_ask(
            comment_url="https://github.com/sukria/koan/issues/42#issuecomment-999",
            project_path=str(tmp_path),
            project_name="koan",
            instance_dir=str(tmp_path),
        )
        assert success is False
        assert "comment" in summary.lower() or "available" in summary.lower()


class TestAskSkillDispatchIntegration:
    """Test that /ask missions are dispatched correctly through skill_dispatch."""

    def test_dispatch_builds_command(self):
        """Verify dispatch_skill_mission returns a command for /ask missions."""
        from app.skill_dispatch import dispatch_skill_mission
        import tempfile, os

        url = "https://github.com/sukria/koan/issues/42#issuecomment-123"
        mission = f"[project:koan] /ask {url}"

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = dispatch_skill_mission(
                mission_text=mission,
                project_name="koan",
                project_path=tmpdir,
                koan_root=tmpdir,
                instance_dir=tmpdir,
            )

        assert cmd is not None, "dispatch_skill_mission should return a command for /ask"
        assert any("ask_runner" in c for c in cmd), f"Command should use ask_runner: {cmd}"
        assert "--project-path" in cmd
        assert "--context-file" in cmd
