"""Tests for app/pr_submit.py — shared PR submission helpers."""

import json
from unittest.mock import patch, MagicMock, call

from app.pr_submit import (
    _is_minimal_body,
    guess_project_name,
    get_current_branch,
    get_commit_subjects,
    get_fork_owner,
    resolve_submit_target,
    submit_draft_pr,
)

_M = "app.pr_submit"


# ---------------------------------------------------------------------------
# guess_project_name
# ---------------------------------------------------------------------------

class TestGuessProjectName:
    def test_simple(self):
        assert guess_project_name("/home/user/koan") == "koan"

    def test_nested(self):
        assert guess_project_name("/a/b/c/my-project") == "my-project"

    def test_trailing_slash(self):
        # Path normalizes trailing slashes
        assert guess_project_name("/a/b/c/") == "c"


# ---------------------------------------------------------------------------
# get_current_branch
# ---------------------------------------------------------------------------

class TestGetCurrentBranch:
    @patch(f"{_M}._git_get_current_branch", return_value="feature/xyz")
    def test_returns_branch(self, mock):
        assert get_current_branch("/p") == "feature/xyz"
        mock.assert_called_once_with(cwd="/p")

    @patch(f"{_M}._git_get_current_branch", return_value="main")
    def test_fallback_to_main(self, mock):
        assert get_current_branch("/p") == "main"


# ---------------------------------------------------------------------------
# get_commit_subjects
# ---------------------------------------------------------------------------

class TestGetCommitSubjects:
    @patch(f"{_M}._git_get_commit_subjects", return_value=["fix: A", "feat: B"])
    def test_returns_list(self, mock):
        assert get_commit_subjects("/p") == ["fix: A", "feat: B"]
        mock.assert_called_once_with(cwd="/p", base_branch="main")

    @patch(f"{_M}._git_get_commit_subjects", return_value=["fix: A", "feat: B"])
    def test_custom_base_branch(self, mock):
        get_commit_subjects("/p", base_branch="develop")
        mock.assert_called_once_with(cwd="/p", base_branch="develop")

    @patch(f"{_M}._git_get_commit_subjects", return_value=[])
    def test_empty_output(self, mock):
        assert get_commit_subjects("/p") == []

    @patch(f"{_M}._git_get_commit_subjects", return_value=[])
    def test_error_returns_empty(self, mock):
        assert get_commit_subjects("/p") == []


# ---------------------------------------------------------------------------
# get_fork_owner
# ---------------------------------------------------------------------------

class TestGetForkOwner:
    @patch(f"{_M}.origin_repo", return_value="myuser/myrepo")
    def test_returns_origin_owner(self, mock):
        assert get_fork_owner("/p") == "myuser"

    @patch(f"{_M}.origin_repo", return_value=None)
    @patch(f"{_M}.run_gh", return_value="ghuser\n")
    def test_falls_back_to_gh_when_no_origin(self, mock_gh, mock_origin):
        assert get_fork_owner("/p") == "ghuser"

    @patch(f"{_M}.origin_repo", return_value=None)
    @patch(f"{_M}.run_gh", side_effect=RuntimeError("gh not found"))
    def test_error_returns_empty(self, mock_gh, mock_origin):
        assert get_fork_owner("/p") == ""

    @patch(f"{_M}.origin_repo", return_value="aiolibsbot/aiohappyeyeballs")
    @patch(f"{_M}.run_gh", return_value="aio-libs\n")
    def test_prefers_origin_over_gh_resolved_upstream(self, mock_gh, mock_origin):
        """Regression: when an `upstream` remote exists, `gh repo view`
        resolves to the upstream repo and reports the *upstream* owner
        (`aio-libs`). The PR head was pushed to origin (the fork), so the
        head owner must be the fork owner (`aiolibsbot`) — not the upstream.
        Returning the wrong owner made `--head aio-libs:branch` point at a
        non-existent branch and silently landed the PR on the fork.
        """
        assert get_fork_owner("/p") == "aiolibsbot"


# ---------------------------------------------------------------------------
# resolve_submit_target
# ---------------------------------------------------------------------------

class TestResolveSubmitTarget:
    @patch(f"{_M}.resolve_target_repo", return_value=None)
    @patch.dict("os.environ", {"KOAN_ROOT": ""})
    def test_fallback_to_owner_repo(self, mock):
        result = resolve_submit_target("/p", "proj", "owner", "repo")
        assert result == {"repo": "owner/repo", "is_fork": False}

    @patch(f"{_M}.resolve_target_repo", return_value="upstream/repo")
    @patch.dict("os.environ", {"KOAN_ROOT": ""})
    def test_fork_detected(self, mock):
        result = resolve_submit_target("/p", "proj", "o", "r")
        assert result == {"repo": "upstream/repo", "is_fork": True}

    @patch(f"{_M}.resolve_target_repo", return_value="aio-libs/aiohappyeyeballs")
    @patch.dict("os.environ", {"KOAN_ROOT": ""})
    def test_fork_detected_via_upstream_remote_when_gh_parent_null(self, mock):
        """Regression: gh repo view reports no parent (it resolved to the
        upstream repo, which is not itself a fork), but an `upstream` git
        remote exists. resolve_target_repo's remote fallback must still
        identify this as a fork so the PR targets upstream with --head, rather
        than landing on the fork via gh's ambiguous base resolution.
        """
        result = resolve_submit_target("/p", "proj", "aio-libs", "aiohappyeyeballs")
        assert result == {"repo": "aio-libs/aiohappyeyeballs", "is_fork": True}

    def test_config_override(self):
        config = {
            "defaults": {},
            "projects": {
                "proj": {
                    "path": "/p",
                    "submit_to_repository": {"repo": "org/repo"},
                }
            },
        }
        with patch("app.projects_config.load_projects_config", return_value=config), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            result = resolve_submit_target("/p", "proj", "o", "r")
            assert result == {"repo": "org/repo", "is_fork": True}


# ---------------------------------------------------------------------------
# _is_minimal_body
# ---------------------------------------------------------------------------

class TestIsMinimalBody:
    def test_empty_body(self):
        assert _is_minimal_body("") is True
        assert _is_minimal_body(None) is True

    def test_closes_reference_only(self):
        assert _is_minimal_body("Closes #42.") is True
        assert _is_minimal_body("Fixes #123") is True

    def test_short_without_headers(self):
        assert _is_minimal_body("Quick fix for the bug.") is True

    def test_body_with_headers(self):
        assert _is_minimal_body("## Summary\n\nGood body") is False

    def test_long_body_without_headers(self):
        assert _is_minimal_body("A" * 100) is False


# ---------------------------------------------------------------------------
# submit_draft_pr
# ---------------------------------------------------------------------------

class TestSubmitDraftPr:
    def test_skips_on_main(self):
        with patch(f"{_M}.get_current_branch", return_value="main"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_skips_on_master(self):
        with patch(f"{_M}.get_current_branch", return_value="master"), \
             patch(f"{_M}.resolve_base_branch", return_value="master"):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_skips_when_head_is_resolved_base_branch_staging(self):
        """Regression for the staging-commit bug: when the project's base
        branch resolves to `staging` and Claude landed there without
        creating a feature branch, submit_draft_pr must abort the push (no
        diff exists) and notify the caller — it must not just log+silent."""
        notify = MagicMock()
        with patch(f"{_M}.get_current_branch", return_value="staging"), \
             patch(f"{_M}.resolve_base_branch", return_value="staging"):
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T", "B", notify_fn=notify,
            )
        assert result is None
        notify.assert_called_once()
        msg = notify.call_args.args[0]
        assert "staging" in msg
        assert "feature branch" in msg.lower() or "base branch" in msg.lower()

    def test_explicit_base_branch_arg_used_in_guard(self):
        """If the caller passes an explicit base_branch, it overrides the
        resolved one and the guard fires against that. Confirms the guard
        is not bypassable through a misconfigured projects.yaml."""
        notify = MagicMock()
        with patch(f"{_M}.get_current_branch", return_value="release-11"), \
             patch(f"{_M}.resolve_base_branch", return_value="staging") as resolved:
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T", "B",
                base_branch="release-11", notify_fn=notify,
            )
        assert result is None
        # resolve_base_branch must NOT be consulted when an explicit value
        # is supplied — that's a fork in the resolution path.
        resolved.assert_not_called()
        notify.assert_called_once()

    def test_returns_existing_pr(self):
        existing = json.dumps({"url": "https://pr/1", "body": "## Summary\nGood body", "number": 42})
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=existing):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") == "https://pr/1"

    def test_enriches_existing_pr_with_minimal_body(self):
        """When an existing PR has a minimal body (e.g. 'Closes #42.'),
        update it with the richer body provided by the caller."""
        existing = json.dumps({"url": "https://pr/1", "body": "Closes #42.", "number": 99})
        gh = MagicMock(return_value=existing)
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", gh):
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T",
                "## Summary\n\nRich description\n\nCloses #42",
                footer_enabled=False,
            )
        assert result == "https://pr/1"
        edit_call = [c for c in gh.call_args_list if "edit" in c.args]
        assert len(edit_call) == 1
        assert "99" in edit_call[0].args
        assert "Rich description" in edit_call[0].args[edit_call[0].args.index("--body") + 1]

    def test_does_not_enrich_existing_pr_with_good_body(self):
        """When an existing PR already has a structured body, don't overwrite it."""
        existing = json.dumps({
            "url": "https://pr/1",
            "body": "## What\n\nFull description with details.\n\n## Why\n\nReason here.",
            "number": 99,
        })
        gh = MagicMock(return_value=existing)
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", gh):
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T",
                "## Summary\n\nDifferent body",
                footer_enabled=False,
            )
        assert result == "https://pr/1"
        edit_calls = [c for c in gh.call_args_list if "edit" in c.args]
        assert len(edit_calls) == 0

    def test_null_existing_pr_falls_through_to_creation(self):
        """gh pr list --jq '.[0]' returns 'null' when no PR exists.
        Must not crash — should fall through to PR creation path."""
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value="null"), \
             patch(f"{_M}.get_commit_subjects", return_value=[]):
            result = submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B")
        assert result is None

    def test_no_commits_returns_none_and_notifies(self):
        notify = MagicMock()
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=[]):
            assert submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T", "B", notify_fn=notify,
            ) is None
        notify.assert_called_once()
        assert "No commits" in notify.call_args.args[0]

    def test_push_failure_returns_none_and_notifies(self):
        notify = MagicMock()
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict", side_effect=RuntimeError("auth denied")):
            assert submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T", "B", notify_fn=notify,
            ) is None
        notify.assert_called_once()
        msg = notify.call_args.args[0]
        assert "git push failed" in msg
        assert "auth denied" in msg

    def test_creates_pr_with_correct_kwargs(self):
        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch("app.pr_footer.build_pr_footer",
                   return_value="_Generated by [Kōan](https://koan.anantys.com)_ _(Claude)_"), \
             patch(f"{_M}.pr_create", return_value="https://pr/99") as mock_pr:
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "42",
                pr_title="fix: bug",
                pr_body="## Summary\nFixed.",
                issue_url="https://issue/42",
            )
            assert result == "https://pr/99"
            kw = mock_pr.call_args[1]
            assert kw["title"] == "fix: bug"
            assert kw["body"] == (
                "## Summary\nFixed.\n\n---\n"
                "_Generated by [Kōan](https://koan.anantys.com)_ _(Claude)_"
            )
            assert kw["draft"] is True

    def test_posts_running_indicator_on_push(self, monkeypatch):
        """After a successful push, the pending koan/mission commit status is
        posted on the resolved head SHA via mission_status.on_branch_pushed."""
        import app.mission_status as ms

        monkeypatch.setenv("KOAN_ROOT", "/tmp/test-koan")
        seen = {}
        monkeypatch.setattr(
            ms, "on_branch_pushed",
            lambda instance, project, repo, branch, sha: seen.update(
                instance=instance, project=project, repo=repo,
                branch=branch, sha=sha),
        )

        def fake_git(*args, **kwargs):
            if args[:1] == ("rev-parse",):
                return "deadbeef\n"
            return ""

        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict", side_effect=fake_git), \
             patch(f"{_M}.resolve_submit_target",
                   return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/99"):
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "42",
                pr_title="fix: bug", pr_body="## Summary\nFixed.",
                footer_enabled=False,
            )
        assert result == "https://pr/99"
        assert seen == {
            "instance": "/tmp/test-koan/instance", "project": "proj",
            "repo": "o/r", "branch": "koan/feat", "sha": "deadbeef",
        }

    def test_replaces_existing_koan_footer(self):
        old_body = (
            "## Summary\nFixed.\n\n---\n"
            "_Generated by [Kōan](https://koan.anantys.com)_ _(Claude · model old)_"
        )
        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch("app.pr_footer.build_pr_footer",
                   return_value="_Generated by [Kōan](https://koan.anantys.com)_ _(Codex · model new)_"), \
             patch(f"{_M}.pr_create", return_value="https://pr/99") as mock_pr:
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "42",
                pr_title="fix: bug",
                pr_body=old_body,
            )
        assert result == "https://pr/99"
        body = mock_pr.call_args.kwargs["body"]
        assert body.count("Generated by") == 1
        assert "model old" not in body
        assert "model new" in body

    def test_footer_can_be_disabled(self):
        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/99") as mock_pr:
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "42",
                pr_title="fix: bug",
                pr_body="Body",
                footer_enabled=False,
            )
        assert result == "https://pr/99"
        assert mock_pr.call_args.kwargs["body"] == "Body"

    def test_fork_workflow_sets_repo_and_head(self):
        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "upstream/r", "is_fork": True}), \
             patch(f"{_M}.get_fork_owner", return_value="myfork"), \
             patch(f"{_M}.pr_create", return_value="https://pr/5") as mock_pr:
            result = submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B")
            assert result == "https://pr/5"
            kw = mock_pr.call_args[1]
            assert kw["repo"] == "upstream/r"
            assert kw["head"] == "myfork:koan/feat"

    def test_pr_create_failure_returns_none_and_notifies(self):
        notify = MagicMock()
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", side_effect=RuntimeError("403 forbidden")):
            assert submit_draft_pr(
                "/p", "proj", "o", "r", "1", "T", "B", notify_fn=notify,
            ) is None
        notify.assert_called_once()
        msg = notify.call_args.args[0]
        assert "gh pr create failed" in msg
        assert "403 forbidden" in msg

    def test_issue_comment_posted_when_url_given(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/1"), \
             patch("app.issue_tracker.add_comment") as mock_comment:
            submit_draft_pr(
                "/p", "proj", "o", "r", "42",
                pr_title="T", pr_body="B",
                issue_url="https://github.com/o/r/issues/42",
            )
            mock_comment.assert_called_once()
            assert mock_comment.call_args.args[0] == "https://github.com/o/r/issues/42"
            assert "https://pr/1" in mock_comment.call_args.args[1]

    def test_no_issue_comment_when_no_url(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value="") as mock_gh, \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/1"), \
             patch("app.issue_tracker.add_comment") as mock_comment:
            submit_draft_pr("/p", "proj", "o", "r", "42", "T", "B")
            # Only 1 gh call (the PR check), no issue comment
            assert mock_gh.call_count == 1
            mock_comment.assert_not_called()

    def test_jira_comment_uses_shared_upsert_with_issue_key(self):
        # Jira comments route through the marker-based upsert (keyed by issue
        # key + skill), not the generic tracker add_comment path.
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value="") as mock_gh, \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/1"), \
             patch("app.issue_tracker.add_comment") as mock_tracker, \
             patch("app.jira_outcome_publish.upsert_jira_comment",
                   return_value=(True, "created")) as mock_upsert:
            submit_draft_pr(
                "/p", "proj", "o", "r", "PROJ-42",
                pr_title="T", pr_body="B",
                issue_url="https://org.atlassian.net/browse/PROJ-42",
                skill_name="fix",
            )
            mock_upsert.assert_called_once()
            assert mock_upsert.call_args.args[0] == "PROJ-42"
            assert mock_upsert.call_args.args[1] == "fix"
            # Jira issues are not commented through the generic tracker path
            # nor through `gh issue comment`.
            mock_tracker.assert_not_called()
            assert mock_gh.call_count == 1

    def test_jira_success_comment_includes_mission_and_pr_link(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/1"), \
             patch("app.jira_outcome_publish.upsert_jira_comment",
                   return_value=(True, "created")) as mock_upsert:
            submit_draft_pr(
                "/p", "proj", "o", "r", "PROJ-42",
                pr_title="fix: bug",
                pr_body=(
                    "## Summary\n\n- Updated parser\n\n"
                    "## Why\n\nNeeded for Jira flow"
                ),
                issue_url="https://org.atlassian.net/browse/PROJ-42",
                base_branch="main",
                skill_name="fix",
            )

        comment_text = mock_upsert.call_args.args[2]
        assert "- **Mission**: `/fix`" in comment_text
        assert "- **Pull request**: [PR #1 — fix: bug](https://pr/1)" in comment_text
        assert "- **Target branch**: `main`" in comment_text
        assert "**What changed**" in comment_text
        assert "**Why**\nNeeded for Jira flow" in comment_text

    def test_jira_push_failure_posts_failure_comment(self):
        notify = MagicMock()
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.resolve_base_branch", return_value="main"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict", side_effect=RuntimeError("auth denied")), \
             patch("app.jira_outcome_publish.upsert_jira_comment",
                   return_value=(True, "created")) as mock_upsert:
            result = submit_draft_pr(
                "/p", "proj", "o", "r", "PROJ-42", "T", "B",
                issue_url="https://org.atlassian.net/browse/PROJ-42",
                notify_fn=notify,
                skill_name="implement",
            )

        assert result is None
        notify.assert_called_once()
        comment_text = mock_upsert.call_args.args[2]
        assert comment_text.startswith("### Kōan · pull request creation failed")
        assert "- **Mission**: `/implement`" in comment_text
        assert "auth denied" in comment_text
