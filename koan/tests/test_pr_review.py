"""Tests for pr_review.py — PR URL parsing, context fetching, prompt building, pipeline."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from app.claude_step import (
    _run_git,
    run_claude as _run_claude,
    commit_if_changes as _commit_if_changes,
    run_claude_step as _run_claude_step,
    _rebase_onto_target,
)
from app.github import run_gh
from app.claude_step import run_project_tests
from app.github_url_parser import parse_pr_url
from app.pr_review import (
    fetch_pr_context,
    build_pr_prompt,
    build_refactor_prompt,
    build_quality_review_prompt,
    detect_test_command,
    detect_skills,
    run_pr_review,
    _build_pr_comment,
)


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_standard_url(self):
        owner, repo, num = parse_pr_url("https://github.com/sukria/koan/pull/29")
        assert owner == "sukria"
        assert repo == "koan"
        assert num == "29"

    def test_url_with_fragment(self):
        owner, repo, num = parse_pr_url(
            "https://github.com/sukria/koan/pull/29#pullrequestreview-123"
        )
        assert owner == "sukria"
        assert repo == "koan"
        assert num == "29"

    def test_url_with_trailing_whitespace(self):
        owner, repo, num = parse_pr_url("  https://github.com/foo/bar/pull/1  ")
        assert owner == "foo"
        assert repo == "bar"
        assert num == "1"

    def test_http_url(self):
        owner, repo, num = parse_pr_url("http://github.com/a/b/pull/99")
        assert owner == "a"
        assert repo == "b"
        assert num == "99"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("https://github.com/sukria/koan/issues/29")

    def test_not_github_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("https://gitlab.com/sukria/koan/pull/29")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("")

    def test_no_pr_number_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("https://github.com/sukria/koan/pull/")


# ---------------------------------------------------------------------------
# truncate_text (now in utils.py)
# ---------------------------------------------------------------------------

class TestTruncateText:
    def test_short_text_unchanged(self):
        from app.utils import truncate_text
        assert truncate_text("hello", 100) == "hello"

    def test_exact_length_unchanged(self):
        from app.utils import truncate_text
        assert truncate_text("12345", 5) == "12345"

    def test_long_text_truncated(self):
        from app.utils import truncate_text
        result = truncate_text("a" * 20, 10)
        assert len(result) < 30
        assert "truncated" in result

    def test_empty_string(self):
        from app.utils import truncate_text
        assert truncate_text("", 100) == ""


# ---------------------------------------------------------------------------
# run_gh (via app.github) — detailed tests in test_github.py
# ---------------------------------------------------------------------------

class TestRunGhIntegration:
    @patch("app.github.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="output\n")
        result = run_gh("pr", "view", "1")
        assert result == "output"

    @patch("app.github.subprocess.run")
    def test_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        with pytest.raises(RuntimeError, match="gh failed"):
            run_gh("pr", "view", "999")


# ---------------------------------------------------------------------------
# _run_git
# ---------------------------------------------------------------------------

class TestRunGit:
    @patch("app.claude_step.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n")
        result = _run_git(["git", "status"])
        assert result == "ok"

    @patch("app.claude_step.subprocess.run")
    def test_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        with pytest.raises(RuntimeError, match="git failed"):
            _run_git(["git", "checkout", "nope"])

    @patch("app.claude_step.subprocess.run")
    def test_cwd_passed(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        _run_git(["git", "status"], cwd="/tmp/test")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == "/tmp/test"


# ---------------------------------------------------------------------------
# _build_pr_comment
# ---------------------------------------------------------------------------

class TestBuildPrComment:
    def test_basic_comment(self):
        actions = ["Rebased onto main", "Tests passing"]
        ctx = {"title": "Fix bug", "body": "desc", "branch": "fix", "base": "main"}
        comment = _build_pr_comment("1", "fix", "main", actions, ctx)
        assert "## Fix bug" in comment
        assert "### Changes" in comment
        assert "Rebased onto main" in comment
        assert "Tests passing" in comment
        assert "Automated by Kōan" in comment
        assert "### Status & Next Steps" in comment
        assert "successfully" in comment

    def test_comment_with_failures(self):
        actions = ["Rebased onto main", "Tests still failing: 3 errors"]
        ctx = {"title": "Fix", "body": "", "branch": "b", "base": "main"}
        comment = _build_pr_comment("1", "b", "main", actions, ctx)
        assert "issues" in comment.lower() or "review" in comment.lower()

    def test_comment_with_skipped_step(self):
        actions = ["Rebased onto main", "Refactor step skipped: timeout"]
        ctx = {"title": "Add feature", "body": "", "branch": "b", "base": "main"}
        comment = _build_pr_comment("1", "b", "main", actions, ctx)
        assert "issues" in comment.lower() or "verify" in comment.lower()

    def test_empty_actions(self):
        ctx = {"title": "", "body": "", "branch": "b", "base": "main"}
        comment = _build_pr_comment("1", "b", "main", [], ctx)
        assert "No changes needed" in comment

    def test_summary_paragraph_present(self):
        actions = ["Rebased onto main", "Applied refactoring", "Force-pushed `b`"]
        ctx = {"title": "Improve auth", "body": "", "branch": "b", "base": "main"}
        comment = _build_pr_comment("1", "b", "main", actions, ctx)
        # Summary should mention the step count and pipeline
        assert "pipeline" in comment.lower() or "steps" in comment.lower()


# ---------------------------------------------------------------------------
# _run_claude_step
# ---------------------------------------------------------------------------

class TestRunClaudeStep:
    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.claude_step.get_model_config", return_value={"mission": "", "fallback": "sonnet"})
    def test_success_with_commit(self, mock_models, mock_flags, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "Done", "error": ""}
        actions = []
        result = _run_claude_step(
            prompt="test", project_path="/tmp",
            commit_msg="test commit", success_label="Step done",
            failure_label="Step failed", actions_log=actions,
        )
        assert result is True
        assert "Step done" in actions

    @patch("app.claude_step.commit_if_changes", return_value=False)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.claude_step.get_model_config", return_value={"mission": "", "fallback": "sonnet"})
    def test_success_no_changes(self, mock_models, mock_flags, mock_claude, mock_commit):
        mock_claude.return_value = {"success": True, "output": "Done", "error": ""}
        actions = []
        result = _run_claude_step(
            prompt="test", project_path="/tmp",
            commit_msg="msg", success_label="OK",
            failure_label="FAIL", actions_log=actions,
        )
        assert result is False
        assert len(actions) == 0

    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.claude_step.get_model_config", return_value={"mission": "", "fallback": "sonnet"})
    def test_failure_logs_error(self, mock_models, mock_flags, mock_claude):
        mock_claude.return_value = {"success": False, "output": "", "error": "timeout"}
        actions = []
        result = _run_claude_step(
            prompt="test", project_path="/tmp",
            commit_msg="msg", success_label="OK",
            failure_label="Step failed", actions_log=actions,
        )
        assert result is False
        assert any("Step failed" in a for a in actions)

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.claude_step.get_model_config", return_value={"mission": "", "fallback": "sonnet"})
    def test_use_skill_includes_skill_tool(self, mock_models, mock_cmd, mock_claude, mock_commit):
        """When use_skill=True, the Skill tool should be in allowedTools."""
        mock_claude.return_value = {"success": True, "output": "Done", "error": ""}
        _run_claude_step(
            prompt="test", project_path="/tmp",
            commit_msg="msg", success_label="OK",
            failure_label="FAIL", actions_log=[],
            use_skill=True,
        )
        call_kwargs = mock_cmd.call_args.kwargs
        allowed = call_kwargs.get("allowed_tools", [])
        assert "Skill" in allowed

    @patch("app.claude_step.commit_if_changes", return_value=True)
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.claude_step.get_model_config", return_value={"mission": "", "fallback": "sonnet"})
    def test_default_no_skill_tool(self, mock_models, mock_cmd, mock_claude, mock_commit):
        """By default, Skill tool should NOT be in allowedTools."""
        mock_claude.return_value = {"success": True, "output": "Done", "error": ""}
        _run_claude_step(
            prompt="test", project_path="/tmp",
            commit_msg="msg", success_label="OK",
            failure_label="FAIL", actions_log=[],
        )
        call_kwargs = mock_cmd.call_args.kwargs
        allowed = call_kwargs.get("allowed_tools", [])
        assert "Skill" not in allowed


# ---------------------------------------------------------------------------
# _commit_if_changes
# ---------------------------------------------------------------------------

class TestCommitIfChanges:
    @patch("app.claude_step._run_git")
    @patch("app.claude_step.subprocess.run")
    def test_commits_when_changes(self, mock_run, mock_git):
        mock_run.return_value = MagicMock(returncode=0, stdout="M file.py\n")
        result = _commit_if_changes("/tmp/p", "test commit")
        assert result is True
        assert mock_git.call_count == 2  # git add + git commit

    @patch("app.claude_step.subprocess.run")
    def test_no_commit_when_clean(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = _commit_if_changes("/tmp/p", "test commit")
        assert result is False


# ---------------------------------------------------------------------------
# _run_claude
# ---------------------------------------------------------------------------

class TestRunClaude:
    @patch("app.claude_step.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Done", stderr=""
        )
        result = _run_claude(["claude", "-p", "test"], "/tmp")
        assert result["success"] is True
        assert result["output"] == "Done"

    @patch("app.claude_step.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error"
        )
        result = _run_claude(["claude", "-p", "test"], "/tmp")
        assert result["success"] is False
        assert "Exit code 1" in result["error"]

    @patch("app.claude_step.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=10)
        result = _run_claude(["claude", "-p", "test"], "/tmp", timeout=10)
        assert result["success"] is False
        assert "Timeout" in result["error"]


# ---------------------------------------------------------------------------
# _rebase_onto_target
# ---------------------------------------------------------------------------

class TestRebaseOntoTarget:
    @patch("app.claude_step._run_git")
    @patch("app.claude_step.subprocess.run")
    def test_success_returns_remote_name(self, mock_subproc, mock_git):
        result = _rebase_onto_target("main", "/tmp/p")
        assert result == "origin"
        assert mock_git.call_count == 2  # fetch + rebase

    @patch("app.claude_step._run_git")
    @patch("app.claude_step.subprocess.run")
    def test_falls_back_to_upstream(self, mock_subproc, mock_git):
        """When origin rebase fails, tries upstream."""
        call_count = 0
        def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First two calls are origin fetch+rebase — fail the rebase
            if call_count == 2:
                raise RuntimeError("conflict on origin")
            return ""
        mock_git.side_effect = selective_fail
        result = _rebase_onto_target("main", "/tmp/p")
        assert result == "upstream"

    @patch("app.claude_step._run_git")
    @patch("app.claude_step.subprocess.run")
    def test_all_remotes_fail_returns_none(self, mock_subproc, mock_git):
        mock_git.side_effect = RuntimeError("conflict")
        result = _rebase_onto_target("main", "/tmp/p")
        assert result is None


# ---------------------------------------------------------------------------
# _run_tests
# ---------------------------------------------------------------------------

class TestRunTests:
    """Tests for run_project_tests (moved to claude_step.py)."""

    @patch("app.claude_step.subprocess.run")
    def test_passing(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="42 tests passed", stderr=""
        )
        result = run_project_tests("/tmp/p", test_cmd="make test")
        assert result["passed"] is True
        assert "42" in result["details"]

    @patch("app.claude_step.subprocess.run")
    def test_failing(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="3 failed", stderr=""
        )
        result = run_project_tests("/tmp/p", test_cmd="make test")
        assert result["passed"] is False

    @patch("app.claude_step.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="make", timeout=300)
        result = run_project_tests("/tmp/p", test_cmd="make test")
        assert result["passed"] is False
        assert "timeout" in result["details"]


# ---------------------------------------------------------------------------
# detect_test_command
# ---------------------------------------------------------------------------

class TestDetectTestCommand:
    def test_makefile_with_test_target(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        assert detect_test_command(str(tmp_path)) == "make test"

    def test_makefile_without_test_target(self, tmp_path):
        (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
        assert detect_test_command(str(tmp_path)) is None

    def test_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / "tests").mkdir()
        assert detect_test_command(str(tmp_path)) == "pytest"

    def test_python_with_makefile(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / "tests").mkdir()
        assert detect_test_command(str(tmp_path)) == "make test"

    def test_nodejs_project(self, tmp_path):
        pkg = {"scripts": {"test": "jest"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_command(str(tmp_path)) == "npm test"

    def test_empty_directory(self, tmp_path):
        assert detect_test_command(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# detect_skills
# ---------------------------------------------------------------------------

class TestDetectSkills:
    def test_with_soul_mentioning_skills(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "soul.md").write_text(
            "Skills: `team.refactor` and `team.review` are available."
        )
        refactor, review = detect_skills()
        assert refactor == "team.refactor"
        assert review == "team.review"

    def test_without_soul(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        refactor, review = detect_skills()
        assert refactor is None
        assert review is None

    def test_generic_skill_mentions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "soul.md").write_text(
            "You have access to refactor skill and review skill tools."
        )
        refactor, review = detect_skills()
        assert refactor == "refactor"
        assert review == "review"

    def test_unquoted_skill_names(self, tmp_path, monkeypatch):
        """Skills detected without backtick quoting."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "soul.md").write_text(
            "Use team.refactor and team.review for code quality."
        )
        refactor, review = detect_skills()
        assert refactor == "team.refactor"
        assert review == "team.review"

    def test_claude_md_at_project_path(self, tmp_path, monkeypatch):
        """Skills detected from CLAUDE.md at project path (highest priority)."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text(
            "Use `myapp.refactor` and `myapp.review` skills when appropriate."
        )
        refactor, review = detect_skills(str(project))
        assert refactor == "myapp.refactor"
        assert review == "myapp.review"

    def test_claude_md_takes_priority_over_soul(self, tmp_path, monkeypatch):
        """CLAUDE.md skills take priority over soul.md skills."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "soul.md").write_text(
            "Skills: `team.refactor` and `team.review` are available."
        )
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text(
            "Available: `custom.refactor` for code cleanup."
        )
        refactor, review = detect_skills(str(project))
        assert refactor == "custom.refactor"
        # review falls through to soul.md
        assert review == "team.review"

    def test_no_project_path(self, tmp_path, monkeypatch):
        """Works without project_path, falls back to soul.md only."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "soul.md").write_text(
            "Skills: `team.refactor` and `team.review`."
        )
        refactor, review = detect_skills("")
        assert refactor == "team.refactor"
        assert review == "team.review"


# ---------------------------------------------------------------------------
# fetch_pr_context
# ---------------------------------------------------------------------------

class TestFetchPrContext:
    @patch("app.rebase_pr.run_gh")
    def test_fetches_all_data(self, mock_gh):
        pr_meta = json.dumps({
            "title": "Add feature",
            "body": "Some description",
            "headRefName": "koan/feature",
            "baseRefName": "main",
            "state": "OPEN",
            "author": {"login": "dev"},
            "url": "https://github.com/o/r/pull/1",
        })
        mock_gh.side_effect = [
            pr_meta,           # PR metadata
            "1",               # review_comments count
            "diff content",    # diff
            "comment1",        # review comments
            "review1",         # reviews
            "discussion1",     # issue comments
        ]

        ctx = fetch_pr_context("owner", "repo", "1")
        assert ctx["title"] == "Add feature"
        assert ctx["branch"] == "koan/feature"
        assert ctx["base"] == "main"
        assert ctx["diff"] == "diff content"
        assert ctx["review_comments"] == "comment1"
        assert ctx["reviews"] == "review1"
        assert ctx["issue_comments"] == "discussion1"
        assert ctx["has_pending_reviews"] is False
        assert mock_gh.call_count == 6

    @patch("app.rebase_pr.run_gh")
    def test_handles_invalid_json(self, mock_gh):
        mock_gh.side_effect = ["not json", "0", "", "", "", ""]
        ctx = fetch_pr_context("o", "r", "1")
        assert ctx["title"] == ""
        assert ctx["branch"] == ""


# ---------------------------------------------------------------------------
# build_pr_prompt
# ---------------------------------------------------------------------------

PR_SKILL_DIR = Path(__file__).parent.parent / "skills" / "core" / "pr"


class TestBuildPrPrompt:
    def test_builds_prompt(self):
        ctx = {
            "title": "Test PR",
            "body": "Description",
            "branch": "fix-branch",
            "base": "main",
            "diff": "+some code",
            "review_comments": "fix this",
            "reviews": "needs work",
            "issue_comments": "thread",
        }
        prompt = build_pr_prompt(ctx, skill_dir=PR_SKILL_DIR)
        assert "Test PR" in prompt
        assert "fix-branch" in prompt
        assert "+some code" in prompt
        assert "fix this" in prompt
        assert "needs work" in prompt


# ---------------------------------------------------------------------------
# build_refactor_prompt / build_quality_review_prompt
# ---------------------------------------------------------------------------

class TestBuildRefactorPrompt:
    def test_includes_project_path(self):
        prompt = build_refactor_prompt("/tmp/project", skill_dir=PR_SKILL_DIR)
        assert "/tmp/project" in prompt

    def test_includes_skill_name(self):
        prompt = build_refactor_prompt("/tmp/project", "team.refactor", skill_dir=PR_SKILL_DIR)
        assert "team.refactor" in prompt

    def test_empty_skill_name(self):
        prompt = build_refactor_prompt("/tmp/project", "", skill_dir=PR_SKILL_DIR)
        assert "/tmp/project" in prompt


class TestBuildQualityReviewPrompt:
    def test_includes_project_path(self):
        prompt = build_quality_review_prompt("/tmp/project", skill_dir=PR_SKILL_DIR)
        assert "/tmp/project" in prompt

    def test_includes_skill_name(self):
        prompt = build_quality_review_prompt("/tmp/project", "team.review", skill_dir=PR_SKILL_DIR)
        assert "team.review" in prompt


# ---------------------------------------------------------------------------
# run_pr_review (integration-level with mocks)
# ---------------------------------------------------------------------------

class TestRunPrReview:
    def _mock_pr_context(self):
        return json.dumps({
            "title": "Fix bug",
            "body": "Fixes #10",
            "headRefName": "koan/fix-bug",
            "baseRefName": "main",
            "state": "OPEN",
            "author": {"login": "dev"},
            "url": "https://github.com/o/r/pull/1",
        })

    @patch("app.pr_review.detect_skills", return_value=(None, None))
    @patch("app.pr_review.detect_test_command", return_value=None)
    @patch("app.pr_review.run_gh")
    @patch("app.pr_review._run_git")
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.commit_if_changes")
    @patch("app.claude_step._run_git")
    @patch("app.claude_step.get_model_config")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.rebase_pr.run_gh")
    def test_basic_workflow(
        self, mock_rebase_gh, mock_flags, mock_models, mock_cs_git, mock_commit,
        mock_claude, mock_git, mock_gh, mock_test_cmd, mock_skills
    ):
        mock_models.return_value = {"mission": "", "fallback": "sonnet"}

        mock_rebase_gh.side_effect = [
            self._mock_pr_context(), "0", "diff", "reviewer comment", "reviews", "thread",
        ]
        mock_claude.return_value = {"success": True, "output": "Fixed", "error": ""}
        mock_commit.return_value = True

        notify = MagicMock()
        success, summary = run_pr_review("o", "r", "1", "/tmp/p", notify_fn=notify, skill_dir=PR_SKILL_DIR)
        assert success is True
        assert "updated" in summary.lower() or "PR #1" in summary

    @patch("app.pr_review.fetch_pr_context")
    def test_no_branch_fails(self, mock_fetch):
        mock_fetch.return_value = {
            "title": "X", "body": "", "branch": "", "base": "main",
            "state": "OPEN", "author": "", "url": "",
            "diff": "", "review_comments": "", "reviews": "", "issue_comments": "",
        }
        notify = MagicMock()
        success, summary = run_pr_review("o", "r", "1", "/tmp/p", notify_fn=notify)
        assert success is False
        assert "branch" in summary.lower()

    @patch("app.pr_review.fetch_pr_context")
    def test_fetch_error_fails(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("API error")
        notify = MagicMock()
        success, summary = run_pr_review("o", "r", "1", "/tmp/p", notify_fn=notify)
        assert success is False
        assert "Failed to fetch" in summary

    @patch("app.pr_review.detect_skills", return_value=("team.refactor", "team.review"))
    @patch("app.pr_review.detect_test_command", return_value="make test")
    @patch("app.pr_review.run_project_tests")
    @patch("app.pr_review.run_gh")
    @patch("app.pr_review._run_git")
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.commit_if_changes")
    @patch("app.claude_step._run_git")
    @patch("app.claude_step.get_model_config")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.rebase_pr.run_gh")
    def test_full_pipeline_with_skills_and_tests(
        self, mock_rebase_gh, mock_flags, mock_models, mock_cs_git, mock_commit,
        mock_claude, mock_git, mock_gh, mock_tests, mock_test_cmd, mock_skills
    ):
        mock_models.return_value = {"mission": "", "fallback": "sonnet"}

        mock_rebase_gh.side_effect = [
            self._mock_pr_context(), "0", "diff", "comment", "review", "thread",
        ]
        # 3 Claude calls: review feedback, refactor, quality review
        mock_claude.return_value = {"success": True, "output": "Done", "error": ""}
        mock_commit.return_value = True
        mock_tests.return_value = {"passed": True, "output": "", "details": "800 tests passed"}

        notify = MagicMock()
        success, summary = run_pr_review("o", "r", "1", "/tmp/p", notify_fn=notify, skill_dir=PR_SKILL_DIR)
        assert success is True
        # Should have called Claude 3 times (feedback + refactor + review)
        assert mock_claude.call_count == 3
        assert "refactoring" in summary.lower() or "quality" in summary.lower()

    @patch("app.pr_review.detect_skills", return_value=(None, None))
    @patch("app.pr_review.detect_test_command", return_value=None)
    @patch("app.pr_review.run_gh")
    @patch("app.pr_review._run_git")
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.commit_if_changes")
    @patch("app.claude_step._run_git")
    @patch("app.claude_step.get_model_config")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.rebase_pr.run_gh")
    def test_no_review_feedback_skips_claude(
        self, mock_rebase_gh, mock_flags, mock_models, mock_cs_git, mock_commit,
        mock_claude, mock_git, mock_gh, mock_test_cmd, mock_skills
    ):
        """When PR has no review comments, skip the Claude feedback step."""
        mock_models.return_value = {"mission": "", "fallback": "sonnet"}

        pr_meta = json.dumps({
            "title": "Add feature",
            "body": "Description",
            "headRefName": "koan/feature",
            "baseRefName": "main",
            "state": "OPEN",
            "author": {"login": "dev"},
            "url": "https://github.com/o/r/pull/1",
        })
        # No comments/reviews
        mock_rebase_gh.side_effect = [pr_meta, "0", "diff", "", "", ""]
        mock_commit.return_value = False

        notify = MagicMock()
        success, summary = run_pr_review("o", "r", "1", "/tmp/p", notify_fn=notify)
        assert success is True
        # Claude should NOT be called (no feedback to address, no skills)
        assert mock_claude.call_count == 0

    @patch("app.pr_review.detect_skills", return_value=(None, None))
    @patch("app.pr_review.detect_test_command", return_value="make test")
    @patch("app.pr_review.run_project_tests")
    @patch("app.pr_review.run_gh")
    @patch("app.pr_review._run_git")
    @patch("app.claude_step.run_claude")
    @patch("app.claude_step.commit_if_changes")
    @patch("app.claude_step._run_git")
    @patch("app.claude_step.get_model_config")
    @patch("app.claude_step.build_full_command", return_value=["claude", "-p", "test"])
    @patch("app.rebase_pr.run_gh")
    def test_failing_tests_trigger_fix_attempt(
        self, mock_rebase_gh, mock_flags, mock_models, mock_cs_git, mock_commit,
        mock_claude, mock_git, mock_gh, mock_tests, mock_test_cmd, mock_skills
    ):
        mock_models.return_value = {"mission": "", "fallback": "sonnet"}

        mock_rebase_gh.side_effect = [
            self._mock_pr_context(), "0", "diff", "", "", "",
        ]
        mock_claude.return_value = {"success": True, "output": "Fixed", "error": ""}
        mock_commit.return_value = True

        # First run fails, second (after fix) passes
        mock_tests.side_effect = [
            {"passed": False, "output": "AssertionError", "details": "1 failed"},
            {"passed": True, "output": "OK", "details": "800 passed"},
        ]

        notify = MagicMock()
        success, summary = run_pr_review("o", "r", "1", "/tmp/p", notify_fn=notify)
        assert success is True
        assert "fixed" in summary.lower() or "passing" in summary.lower()
