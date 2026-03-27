"""Tests for the recreate_pr pipeline module."""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from app.recreate_pr import (
    _build_recreate_comment,
    _build_recreate_prompt,
    _fetch_upstream_target,
    _has_commits_on_branch,
    _push_recreated,
    run_recreate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pr_context():
    """Sample PR context dict as returned by fetch_pr_context."""
    return {
        "title": "feat: add outbox scanner",
        "body": "Scans outbox for secrets before sending to Telegram.",
        "branch": "koan/agent-threat-model",
        "base": "main",
        "state": "OPEN",
        "author": "atoomic",
        "url": "https://github.com/sukria/koan/pull/71",
        "diff": "+import re\n+def scan():\n+    pass",
        "review_comments": "@reviewer: looks good",
        "reviews": "@reviewer (APPROVED): nice work",
        "issue_comments": "@human: can you add tests?",
    }


@pytest.fixture
def skill_dir():
    return Path(__file__).parent.parent / "skills" / "core" / "recreate"


# ---------------------------------------------------------------------------
# _fetch_upstream_target
# ---------------------------------------------------------------------------

class TestFetchUpstreamTarget:
    def test_upstream_preferred(self):
        """upstream is tried first (source-of-truth in fork setups)."""
        with patch("app.recreate_pr._run_git") as mock_git:
            result = _fetch_upstream_target("main", "/project")
            assert result == "upstream"
            mock_git.assert_called_once_with(
                ["git", "fetch", "upstream", "main"], cwd="/project"
            )

    def test_falls_back_to_origin(self):
        """When upstream fails, falls back to origin."""
        with patch("app.recreate_pr._run_git") as mock_git:
            mock_git.side_effect = [RuntimeError("no upstream"), None]
            result = _fetch_upstream_target("main", "/project")
            assert result == "origin"
            assert mock_git.call_count == 2

    def test_both_fail_returns_none(self):
        with patch("app.recreate_pr._run_git") as mock_git:
            mock_git.side_effect = RuntimeError("fail")
            result = _fetch_upstream_target("main", "/project")
            assert result is None


# ---------------------------------------------------------------------------
# _has_commits_on_branch
# ---------------------------------------------------------------------------

class TestHasCommitsOnBranch:
    def test_has_commits(self):
        with patch("app.recreate_pr._run_git", return_value="abc123 first commit\ndef456 second"):
            assert _has_commits_on_branch("koan/feat", "main", "origin", "/proj") is True

    def test_no_commits(self):
        with patch("app.recreate_pr._run_git", return_value=""):
            assert _has_commits_on_branch("koan/feat", "main", "origin", "/proj") is False

    def test_git_error_falls_back_to_head(self):
        """When the branch ref fails, falls back to HEAD."""
        with patch("app.recreate_pr._run_git") as mock_git:
            mock_git.side_effect = [
                RuntimeError("ambiguous ref"),  # branch ref fails
                "abc123 commit via HEAD",        # HEAD ref succeeds
            ]
            assert _has_commits_on_branch("koan.atoomic/feat", "main", "origin", "/proj") is True
            assert mock_git.call_count == 2

    def test_both_refs_fail_returns_false(self):
        with patch("app.recreate_pr._run_git", side_effect=RuntimeError("oops")):
            assert _has_commits_on_branch("koan/feat", "main", "origin", "/proj") is False

    def test_branch_empty_but_head_has_commits(self):
        """Branch ref returns empty but HEAD has commits (e.g. Claude switched branches)."""
        with patch("app.recreate_pr._run_git") as mock_git:
            mock_git.side_effect = ["", "abc123 commit on HEAD"]
            assert _has_commits_on_branch("koan/feat", "main", "origin", "/proj") is True


# ---------------------------------------------------------------------------
# _build_recreate_prompt
# ---------------------------------------------------------------------------

class TestBuildRecreatePrompt:
    def test_with_skill_dir(self, pr_context, skill_dir):
        prompt = _build_recreate_prompt(pr_context, skill_dir=skill_dir)
        assert "outbox scanner" in prompt
        assert "main" in prompt
        assert "koan/agent-threat-model" in prompt

    def test_without_skill_dir_uses_system_prompts(self, pr_context):
        """Without skill_dir, falls back to system-prompts/recreate.md which
        may not exist. That's fine -- the test just verifies the code path."""
        with patch("app.claude_step.load_prompt_or_skill", return_value="fallback prompt") as mock:
            prompt = _build_recreate_prompt(pr_context, skill_dir=None)
            mock.assert_called_once()
            assert prompt == "fallback prompt"

    def test_prompt_contains_diff(self, pr_context, skill_dir):
        prompt = _build_recreate_prompt(pr_context, skill_dir=skill_dir)
        assert "scan()" in prompt

    def test_prompt_contains_review_comments(self, pr_context, skill_dir):
        prompt = _build_recreate_prompt(pr_context, skill_dir=skill_dir)
        assert "looks good" in prompt


# ---------------------------------------------------------------------------
# _build_recreate_comment
# ---------------------------------------------------------------------------

class TestBuildRecreateComment:
    def test_basic_comment(self, pr_context):
        comment = _build_recreate_comment(
            "71", "koan/feat", "main",
            ["Read PR #71", "Reimplemented feature"],
            pr_context,
        )
        assert "Recreated:" in comment
        assert "outbox scanner" in comment
        assert "diverged" in comment
        assert "scratch" in comment.lower()
        assert "Reimplemented feature" in comment

    def test_comment_with_new_pr_url(self, pr_context):
        comment = _build_recreate_comment(
            "71", "koan/feat", "main",
            ["Created new branch"],
            pr_context,
            new_pr_url="https://github.com/sukria/koan/pull/116",
        )
        assert "https://github.com/sukria/koan/pull/116" in comment

    def test_comment_without_new_pr(self, pr_context):
        comment = _build_recreate_comment(
            "71", "koan/feat", "main",
            ["Force-pushed"],
            pr_context,
        )
        assert "force-pushed" in comment.lower()

    def test_empty_actions(self, pr_context):
        comment = _build_recreate_comment("71", "br", "main", [], pr_context)
        assert "Reimplemented from scratch" in comment

    def test_diffstat_included(self, pr_context):
        comment = _build_recreate_comment(
            "71", "koan/feat", "main",
            ["Created fresh branch", "Reimplemented feature"],
            pr_context,
            diffstat="4 files changed, 80 insertions(+), 12 deletions(-)",
        )
        assert "4 files changed" in comment
        assert "**Diff**" in comment

    def test_test_result_highlighted(self, pr_context):
        comment = _build_recreate_comment(
            "71", "koan/feat", "main",
            ["Created fresh branch", "Reimplemented feature", "Tests pass (47 passed)"],
            pr_context,
        )
        assert "**Tests pass (47 passed)**" in comment
        # Test result should not be duplicated in actions
        actions_section = comment.split("### Actions")[1]
        assert "Tests pass" not in actions_section

    def test_mechanical_actions_filtered(self, pr_context):
        comment = _build_recreate_comment(
            "71", "koan/feat", "main",
            [
                "Read PR #71: \"feat: add outbox scanner\"",
                "Read PR comments and review feedback",
                "Reimplemented feature",
                "Commented on original PR",
            ],
            pr_context,
        )
        assert "Read PR #71" not in comment
        assert "Read PR comments" not in comment
        assert "Commented on" not in comment
        assert "Reimplemented feature" in comment


# ---------------------------------------------------------------------------
# _push_recreated
# ---------------------------------------------------------------------------

class TestPushRecreated:
    def test_force_push_succeeds(self, pr_context):
        with patch("app.claude_step._run_git") as mock_git:
            result = _push_recreated(
                "koan/feat", "main", "sukria/koan", "71",
                pr_context, "/project",
            )
            assert result["success"] is True
            assert any("Force-pushed" in a for a in result["actions"])
            mock_git.assert_called_once_with(
                ["git", "push", "origin", "koan/feat", "--force-with-lease"],
                cwd="/project",
            )

    def test_permission_denied_creates_new_pr(self, pr_context):
        with patch("app.claude_step._run_git") as mock_git, \
             patch("app.claude_step.pr_create", return_value="https://github.com/sukria/koan/pull/120"), \
             patch("app.claude_step.run_gh"), \
             patch("app.utils.get_branch_prefix", return_value="koan/"):
            mock_git.side_effect = [
                RuntimeError("permission denied"),  # force-push fails
                None,  # checkout -b
                None,  # push -u
            ]
            result = _push_recreated(
                "koan/feat", "main", "sukria/koan", "71",
                pr_context, "/project",
            )
            assert result["success"] is True
            assert any("new branch" in a.lower() for a in result["actions"])
            assert any("draft PR" in a for a in result["actions"])

    def test_non_permission_error_fails(self, pr_context):
        with patch("app.claude_step._run_git") as mock_git:
            mock_git.side_effect = RuntimeError("network error")
            result = _push_recreated(
                "koan/feat", "main", "sukria/koan", "71",
                pr_context, "/project",
            )
            assert result["success"] is False
            assert "network error" in result["error"]


# ---------------------------------------------------------------------------
# run_recreate -- full pipeline
# ---------------------------------------------------------------------------

class TestRunRecreate:
    def _mock_context(self):
        return {
            "title": "feat: add scanner",
            "body": "Adds outbox scanning.",
            "branch": "koan/scanner",
            "base": "main",
            "state": "OPEN",
            "author": "user",
            "url": "https://github.com/sukria/koan/pull/71",
            "diff": "+code here",
            "review_comments": "",
            "reviews": "",
            "issue_comments": "",
        }

    def test_fetch_context_failure(self):
        notify = MagicMock()
        with patch("app.recreate_pr.fetch_pr_context", side_effect=RuntimeError("404")):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "Failed to fetch" in msg

    def test_no_branch_name(self):
        notify = MagicMock()
        ctx = self._mock_context()
        ctx["branch"] = ""
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "branch name" in msg.lower()

    def test_fetch_upstream_failure(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value=None):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "Could not fetch" in msg

    def test_branch_creation_failure(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git") as mock_git, \
             patch("app.recreate_pr._safe_checkout"):
            # First call: branch -D (may fail, that's ok)
            # Second call: checkout -b (must fail for this test)
            mock_git.side_effect = [None, RuntimeError("checkout failed")]
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "Failed to create fresh branch" in msg

    def test_no_changes_produced(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature", return_value=True), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=False), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "no changes" in msg.lower()

    def test_no_changes_includes_actions_log(self):
        """When reimpl fails, the error message should include actions_log."""
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature", return_value=False), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=False), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "reimplementation step failed" in msg.lower()
            assert "Read PR #1" in msg

    def test_merged_pr_rejected(self):
        """Merged PRs should be rejected early with a clear message."""
        notify = MagicMock()
        ctx = self._mock_context()
        ctx["state"] = "MERGED"
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx):
            ok, msg = run_recreate("o", "r", "42", "/p", notify_fn=notify)
            assert ok is False
            assert "already merged" in msg.lower()
            assert "#42" in msg

    def test_closed_pr_rejected(self):
        """Closed PRs should be rejected early with a clear message."""
        notify = MagicMock()
        ctx = self._mock_context()
        ctx["state"] = "CLOSED"
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx):
            ok, msg = run_recreate("o", "r", "42", "/p", notify_fn=notify)
            assert ok is False
            assert "closed" in msg.lower()
            assert "#42" in msg

    def test_open_pr_not_rejected(self):
        """Open PRs should proceed past the state guard."""
        notify = MagicMock()
        ctx = self._mock_context()
        ctx["state"] = "OPEN"
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature", return_value=True), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": [], "error": "",
             }), \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is True

    def test_successful_pipeline(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature"), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": True, "output": "50 passed", "details": "50 passed"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": ["Force-pushed `koan/scanner`"], "error": "",
             }), \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("sukria", "koan", "71", "/p", notify_fn=notify)
            assert ok is True
            assert "recreated" in msg.lower()
            assert "#71" in msg

    def test_push_failure(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature"), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": False, "actions": [], "error": "network error",
             }), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is False
            assert "Push failed" in msg

    def test_comment_failure_is_non_fatal(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature"), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": ["Force-pushed"], "error": "",
             }), \
             patch("app.recreate_pr.run_gh", side_effect=RuntimeError("comment failed")), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is True
            assert "non-fatal" in msg.lower()

    def test_notify_fn_called_for_progress(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature"), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": [], "error": "",
             }), \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout"):
            run_recreate("o", "r", "1", "/p", notify_fn=notify)
            # Should have been called at least for: reading PR, creating branch,
            # reimplementing, running tests, pushing
            assert notify.call_count >= 4

    def test_actions_log_tracks_pr_read(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="main"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature"), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": [], "error": "",
             }), \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert 'Read PR #1' in msg

    def test_restores_original_branch_on_success(self):
        notify = MagicMock()
        ctx = self._mock_context()
        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", return_value="develop"), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature"), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": [], "error": "",
             }), \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout") as mock_checkout:
            run_recreate("o", "r", "1", "/p", notify_fn=notify)
            mock_checkout.assert_called_with("develop", "/p")

    def test_claude_switches_branch_detected(self):
        """When Claude creates a different branch, run_recreate should detect it."""
        notify = MagicMock()
        ctx = self._mock_context()  # branch = "koan/scanner"

        # _get_current_branch returns different values:
        # 1st call: before work (returns "main" - original)
        # 2nd call: after _reimpl_feature (returns "koan/scanner-v2" - Claude switched)
        get_branch_calls = iter(["main", "koan/scanner-v2"])

        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", side_effect=get_branch_calls), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature", return_value=True), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True) as mock_has, \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": [], "error": "",
             }) as mock_push, \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is True
            # The branch switch should be logged in the summary
            assert "switched" in msg.lower()
            assert "koan/scanner-v2" in msg
            # _has_commits_on_branch should be called with the NEW branch name
            mock_has.assert_called_once_with(
                "koan/scanner-v2", "main", "origin", "/p"
            )
            # Push should use the new branch name
            mock_push.assert_called_once()
            assert mock_push.call_args[0][0] == "koan/scanner-v2"

    def test_claude_stays_on_branch(self):
        """When Claude stays on the expected branch, no switch is logged."""
        notify = MagicMock()
        ctx = self._mock_context()  # branch = "koan/scanner"

        # Both calls return expected branches
        get_branch_calls = iter(["main", "koan/scanner"])

        with patch("app.recreate_pr.fetch_pr_context", return_value=ctx), \
             patch("app.recreate_pr._get_current_branch", side_effect=get_branch_calls), \
             patch("app.recreate_pr._fetch_upstream_target", return_value="origin"), \
             patch("app.recreate_pr._run_git"), \
             patch("app.recreate_pr._reimpl_feature", return_value=True), \
             patch("app.recreate_pr._has_commits_on_branch", return_value=True), \
             patch("app.recreate_pr.run_project_tests", return_value={"passed": False, "output": "", "details": "command not found"}), \
             patch("app.recreate_pr._push_recreated", return_value={
                 "success": True, "actions": [], "error": "",
             }), \
             patch("app.recreate_pr.run_gh"), \
             patch("app.recreate_pr._safe_checkout"):
            ok, msg = run_recreate("o", "r", "1", "/p", notify_fn=notify)
            assert ok is True
            assert "switched" not in msg.lower()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_success(self):
        from app.recreate_pr import main
        with patch("app.recreate_pr.run_recreate", return_value=(True, "Done")):
            code = main(["https://github.com/sukria/koan/pull/71", "--project-path", "/p"])
            assert code == 0

    def test_main_failure(self):
        from app.recreate_pr import main
        with patch("app.recreate_pr.run_recreate", return_value=(False, "Fail")):
            code = main(["https://github.com/sukria/koan/pull/71", "--project-path", "/p"])
            assert code == 1

    def test_main_invalid_url(self, capsys):
        from app.recreate_pr import main
        code = main(["https://not-github.com/foo", "--project-path", "/p"])
        assert code == 1

    def test_main_skill_dir_path(self):
        """Verify the CLI passes the correct skill_dir."""
        from app.recreate_pr import main
        with patch("app.recreate_pr.run_recreate") as mock_run:
            mock_run.return_value = (True, "ok")
            main(["https://github.com/sukria/koan/pull/71", "--project-path", "/p"])
            kwargs = mock_run.call_args
            skill_dir = kwargs[1].get("skill_dir") if kwargs[1] else None
            # skill_dir should end with skills/core/recreate
            assert skill_dir is not None
            assert str(skill_dir).endswith("skills/core/recreate")


# ---------------------------------------------------------------------------
# _run_tests
# ---------------------------------------------------------------------------

class TestRunTests:
    """Tests for run_project_tests (now in claude_step.py, used by recreate_pr)."""

    def test_tests_pass(self):
        from app.claude_step import run_project_tests
        with patch("app.claude_step.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="50 passed in 3.2s",
                stderr="",
            )
            result = run_project_tests("/project")
            assert result["passed"] is True
            assert "50 passed" in result["details"]

    def test_tests_fail(self):
        from app.claude_step import run_project_tests
        with patch("app.claude_step.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="2 failed, 48 passed",
                stderr="",
            )
            result = run_project_tests("/project")
            assert result["passed"] is False
            assert "2 failed" in result["details"]

    def test_tests_timeout(self):
        from app.claude_step import run_project_tests
        import subprocess
        with patch("app.claude_step.subprocess.run", side_effect=subprocess.TimeoutExpired("make", 300)):
            result = run_project_tests("/project")
            assert result["passed"] is False
            assert "timeout" in result["details"]

    def test_no_makefile(self):
        from app.claude_step import run_project_tests
        with patch("app.claude_step.subprocess.run", side_effect=FileNotFoundError):
            result = run_project_tests("/project")
            assert result["passed"] is False
            assert result["details"] == "command not found"
