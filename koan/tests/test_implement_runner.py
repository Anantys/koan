"""Tests for implement_runner.py — the implement execution pipeline."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.github import fetch_issue_with_comments, detect_parent_repo
from app.projects_config import get_project_submit_to_repository
from skills.core.implement.implement_runner import (
    run_implement,
    _is_plan_content,
    _extract_latest_plan,
    _build_prompt,
    _execute_implementation,
    _generate_pr_summary,
    _submit_implement_pr,
    main,
)

# Shared helpers imported via app.pr_submit
from app.pr_submit import (
    get_current_branch,
    get_commit_subjects,
    get_fork_owner,
    guess_project_name,
    resolve_submit_target,
    submit_draft_pr,
)


_IMPL_MODULE = "skills.core.implement.implement_runner"
_PR_MODULE = "app.pr_submit"


# ---------------------------------------------------------------------------
# _is_plan_content
# ---------------------------------------------------------------------------

class TestIsPlanContent:
    def test_empty_text(self):
        assert not _is_plan_content("")
        assert not _is_plan_content(None)

    def test_no_markers(self):
        assert not _is_plan_content("Just a random comment about the issue.")

    def test_phase_marker(self):
        assert _is_plan_content("Some text\n#### Phase 1: Setup\nDo things")

    def test_implementation_phases_marker(self):
        assert _is_plan_content("## Implementation Phases\n#### Phase 1")

    def test_summary_marker(self):
        assert _is_plan_content("### Summary\nThis plan does X")

    def test_changes_iteration_marker(self):
        assert _is_plan_content("### Changes in this iteration\n- Updated phase 2")

    def test_case_insensitive(self):
        assert _is_plan_content("## implementation phases\n#### Phase 1")
        assert _is_plan_content("### SUMMARY\nText here")


# ---------------------------------------------------------------------------
# _extract_latest_plan
# ---------------------------------------------------------------------------

class TestExtractLatestPlan:
    def test_plan_in_body_only(self):
        body = "## Summary\nDo the thing\n### Implementation Phases\n#### Phase 1: Start"
        result = _extract_latest_plan(body, [])
        assert result == body

    def test_plan_in_latest_comment(self):
        body = "Original issue description"
        comments = [
            {"body": "Nice idea!", "author": "user1", "date": "2026-01-01"},
            {"body": "### Summary\nOld plan v1\n#### Phase 1: Old", "author": "bot", "date": "2026-01-02"},
            {"body": "### Summary\nNew plan v2\n#### Phase 1: New", "author": "bot", "date": "2026-01-03"},
        ]
        result = _extract_latest_plan(body, comments)
        assert "New plan v2" in result
        assert "Old plan v1" not in result

    def test_ignores_non_plan_comments(self):
        body = "### Summary\nThe original plan"
        comments = [
            {"body": "Looks good!", "author": "reviewer", "date": "2026-01-01"},
            {"body": "Ship it", "author": "reviewer2", "date": "2026-01-02"},
        ]
        result = _extract_latest_plan(body, comments)
        assert "The original plan" in result

    def test_fallback_to_long_body_without_markers(self):
        body = "A" * 200  # Long body without plan markers
        result = _extract_latest_plan(body, [])
        assert result == body

    def test_empty_body_no_comments(self):
        result = _extract_latest_plan("", [])
        assert result == ""

    def test_short_body_without_markers(self):
        """Short bodies without markers are now returned as fallback."""
        result = _extract_latest_plan("Short text", [])
        assert result == "Short text"

    def test_plan_in_middle_comment_not_last(self):
        """The latest plan comment wins, even if non-plan comments follow."""
        body = "Issue body"
        comments = [
            {"body": "### Summary\nPlan v1\n#### Phase 1: Do it", "author": "bot", "date": "2026-01-01"},
            {"body": "Thanks for the plan!", "author": "human", "date": "2026-01-02"},
        ]
        result = _extract_latest_plan(body, comments)
        assert "Plan v1" in result

    def test_empty_comments_list(self):
        body = "### Implementation Phases\n#### Phase 1: Go"
        result = _extract_latest_plan(body, [])
        assert "Phase 1" in result


# ---------------------------------------------------------------------------
# fetch_issue_with_comments (now in github.py)
# ---------------------------------------------------------------------------

class TestFetchIssueWithComments:
    def test_successful_fetch(self):
        issue_data = json.dumps({"title": "My Plan", "body": "The plan body"})
        comments_data = json.dumps([
            {"author": "user1", "date": "2026-01-01", "body": "Nice!"}
        ])
        with patch("app.github.api", side_effect=[issue_data, comments_data]):
            title, body, comments = fetch_issue_with_comments("owner", "repo", "42")
            assert title == "My Plan"
            assert body == "The plan body"
            assert len(comments) == 1
            assert comments[0]["author"] == "user1"

    def test_malformed_issue_json(self):
        with patch("app.github.api", side_effect=["not json", "[]"]):
            title, body, comments = fetch_issue_with_comments("o", "r", "1")
            assert title == ""
            assert body == "not json"
            assert comments == []

    def test_malformed_comments_json(self):
        issue_data = json.dumps({"title": "T", "body": "B"})
        with patch("app.github.api", side_effect=[issue_data, "bad"]):
            title, body, comments = fetch_issue_with_comments("o", "r", "1")
            assert title == "T"
            assert comments == []

    def test_empty_comments(self):
        issue_data = json.dumps({"title": "T", "body": "B"})
        with patch("app.github.api", side_effect=[issue_data, "[]"]):
            _, _, comments = fetch_issue_with_comments("o", "r", "1")
            assert comments == []


# ---------------------------------------------------------------------------
# _build_prompt + _execute_implementation
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_uses_skill_prompt_when_skill_dir_given(self):
        skill_dir = Path("/fake/skill/dir")
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill", return_value="prompt") as mock_load:
            result = _build_prompt(
                "http://url", "Title", "Plan", "Context",
                skill_dir=skill_dir,
            )
            mock_load.assert_called_once_with(
                skill_dir, "implement",
                ISSUE_URL="http://url",
                ISSUE_TITLE="Title",
                PLAN="Plan",
                CONTEXT="Context",
                BRANCH_PREFIX="koan/",
                ISSUE_NUMBER="",
            )
            assert result == "prompt"

    def test_uses_global_prompt_when_no_skill_dir(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill", return_value="prompt") as mock_load:
            result = _build_prompt(
                "http://url", "Title", "Plan", "Context",
            )
            mock_load.assert_called_once_with(
                None, "implement",
                ISSUE_URL="http://url",
                ISSUE_TITLE="Title",
                PLAN="Plan",
                CONTEXT="Context",
                BRANCH_PREFIX="koan/",
                ISSUE_NUMBER="",
            )
            assert result == "prompt"

    def test_prompt_includes_pr_creation_step(self):
        """implement.md must instruct Claude to push and create a draft PR."""
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "implement"
        from app.prompts import load_skill_prompt

        prompt = load_skill_prompt(
            skill_dir, "implement",
            ISSUE_URL="https://github.com/o/r/issues/42",
            ISSUE_TITLE="Test",
            PLAN="Plan text",
            CONTEXT="ctx",
            BRANCH_PREFIX="koan/",
            ISSUE_NUMBER="42",
        )
        assert "gh pr create --draft" in prompt
        assert "git push" in prompt
        assert "Closes https://github.com/o/r/issues/42" in prompt


class TestExecuteImplementation:
    def test_passes_correct_run_command_params(self):
        with patch(f"{_IMPL_MODULE}._build_prompt", return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value="ok") as mock_run:
            result = _execute_implementation(
                "/project", "url", "t", "p", "c",
                skill_dir=Path("/skill"),
            )
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[0][0] == "prompt"
            assert call_kwargs[0][1] == "/project"
            assert call_kwargs[1]["max_turns"] == 50
            assert call_kwargs[1]["timeout"] == 3600
            assert result == "ok"

    def test_passes_allowed_tools(self):
        """run_command must receive allowed_tools covering full CLAUDE_TOOLS set."""
        with patch(f"{_IMPL_MODULE}._build_prompt", return_value="p"), \
             patch("app.cli_provider.run_command", return_value="ok") as mock_run:
            _execute_implementation("/project", "url", "t", "p", "c")
            call_args = mock_run.call_args
            tools = call_args[1].get("allowed_tools") or call_args[0][2]
            for tool in ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]:
                assert tool in tools, f"{tool} missing from allowed_tools"


# ---------------------------------------------------------------------------
# run_implement — top-level
# ---------------------------------------------------------------------------

class TestRunImplement:
    def test_invalid_url(self):
        ok, msg = run_implement("/project", "not-a-url", notify_fn=MagicMock())
        assert not ok
        assert "Invalid" in msg

    def test_no_plan_found(self):
        notify = MagicMock()
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", "", [])):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "No plan found" in msg

    def test_successful_implementation(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "#42" in msg

    def test_with_context(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done") as mock_run:
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                context="Phase 1 to 3",
                notify_fn=notify,
            )
            assert ok
            assert "Phase 1 to 3" in msg
            _, kwargs = mock_run.call_args
            assert kwargs.get("context") == "Phase 1 to 3" or \
                   mock_run.call_args[0][3] == "Phase 1 to 3"

    def test_fetch_failure(self):
        notify = MagicMock()
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    side_effect=RuntimeError("API error")):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "Failed to fetch" in msg

    def test_claude_failure(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    side_effect=RuntimeError("Timeout")):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "Implementation failed" in msg

    def test_empty_claude_output(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value=""):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/1",
                notify_fn=notify,
            )
            assert not ok
            assert "empty output" in msg

    def test_default_context_when_none(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done") as mock_run:
            run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            args = mock_run.call_args
            assert "Implement the full plan." in str(args)

    def test_notify_messages(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation",
                    return_value="Done"):
            run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                context="Phase 1",
                notify_fn=notify,
            )
            first_msg = notify.call_args_list[0][0][0]
            assert "#42" in first_msg
            assert "Phase 1" in first_msg
            second_msg = notify.call_args_list[1][0][0]
            assert "#42" in second_msg


# ---------------------------------------------------------------------------
# guess_project_name (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGuessProjectName:
    def test_extracts_dir_name(self):
        assert guess_project_name("/Users/me/workspace/koan") == "koan"

    def test_simple_path(self):
        assert guess_project_name("/tmp/myproject") == "myproject"


# ---------------------------------------------------------------------------
# get_current_branch (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGetCurrentBranch:
    def test_returns_branch_name(self):
        with patch(f"{_PR_MODULE}.run_git_strict",
                    return_value="koan/implement-42\n"):
            assert get_current_branch("/project") == "koan/implement-42"

    def test_returns_main_on_error(self):
        with patch(f"{_PR_MODULE}.run_git_strict",
                    side_effect=RuntimeError("not a repo")):
            assert get_current_branch("/project") == "main"


# ---------------------------------------------------------------------------
# get_commit_subjects (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGetCommitSubjects:
    def test_returns_subjects(self):
        with patch(f"{_PR_MODULE}.run_git_strict",
                    return_value="feat: add X\nfix: broken Y\n"):
            result = get_commit_subjects("/project")
            assert result == ["feat: add X", "fix: broken Y"]

    def test_returns_empty_on_error(self):
        with patch(f"{_PR_MODULE}.run_git_strict",
                    side_effect=RuntimeError("no commits")):
            assert get_commit_subjects("/project") == []

    def test_returns_empty_for_no_output(self):
        with patch(f"{_PR_MODULE}.run_git_strict",
                    return_value=""):
            assert get_commit_subjects("/project") == []


# ---------------------------------------------------------------------------
# get_fork_owner (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestGetForkOwner:
    def test_returns_owner(self):
        with patch(f"{_PR_MODULE}.run_gh",
                    return_value="atoomic"):
            assert get_fork_owner("/project") == "atoomic"

    def test_returns_empty_on_error(self):
        with patch(f"{_PR_MODULE}.run_gh",
                    side_effect=RuntimeError("gh failed")):
            assert get_fork_owner("/project") == ""


# ---------------------------------------------------------------------------
# resolve_submit_target (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestResolveSubmitTarget:
    def test_config_based_target(self):
        config = {
            "defaults": {},
            "projects": {
                "myapp": {
                    "path": "/project",
                    "submit_to_repository": {"repo": "upstream/myapp", "remote": "upstream"},
                }
            },
        }
        with patch("app.projects_config.load_projects_config",
                    return_value=config), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            target = resolve_submit_target("/project", "myapp", "fork-owner", "myapp")
            assert target == {"repo": "upstream/myapp", "is_fork": True}

    def test_auto_detect_fork(self):
        with patch("app.projects_config.load_projects_config",
                    return_value=None), \
             patch(f"{_PR_MODULE}.detect_parent_repo",
                    return_value="parent-owner/repo"), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            target = resolve_submit_target("/project", "myapp", "o", "r")
            assert target == {"repo": "parent-owner/repo", "is_fork": True}

    def test_fallback_to_issue_repo(self):
        with patch("app.projects_config.load_projects_config",
                    return_value=None), \
             patch(f"{_PR_MODULE}.detect_parent_repo",
                    return_value=None), \
             patch.dict("os.environ", {"KOAN_ROOT": "/koan"}):
            target = resolve_submit_target("/project", "myapp", "owner", "repo")
            assert target == {"repo": "owner/repo", "is_fork": False}

    def test_no_koan_root(self):
        with patch(f"{_PR_MODULE}.detect_parent_repo",
                    return_value=None), \
             patch.dict("os.environ", {}, clear=True):
            target = resolve_submit_target("/project", "myapp", "o", "r")
            assert target == {"repo": "o/r", "is_fork": False}


# ---------------------------------------------------------------------------
# _generate_pr_summary
# ---------------------------------------------------------------------------

class TestGeneratePRSummary:
    def test_happy_path(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command",
                    return_value="A great summary"):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add X", "fix: broken Y"],
                skill_dir=Path("/skill"),
            )
            assert result == "A great summary"

    def test_fallback_on_model_failure(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command",
                    side_effect=RuntimeError("model unavailable")):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add X"],
                skill_dir=Path("/skill"),
            )
            assert "http://issue/1" in result
            assert "feat: add X" in result

    def test_fallback_on_empty_output(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value=""):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add X"],
                skill_dir=Path("/skill"),
            )
            assert "http://issue/1" in result

    def test_no_skill_dir_uses_load_prompt(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt") as mock_load, \
             patch("app.cli_provider.run_command", return_value="summary"):
            _generate_pr_summary(
                "/project", "Title", "http://issue/1", ["c1"],
            )
            mock_load.assert_called_once()
            assert mock_load.call_args[0][0] is None
            assert mock_load.call_args[0][1] == "pr_summary"

    def test_empty_commits(self):
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value="summary"):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1", [],
                skill_dir=Path("/skill"),
            )
            assert result == "summary"

    def test_fallback_when_max_turns_noise_stripped(self):
        """When run_command returns empty after strip_cli_noise, fallback is used."""
        with patch(f"{_IMPL_MODULE}.load_prompt_or_skill",
                    return_value="prompt"), \
             patch("app.cli_provider.run_command", return_value=""):
            result = _generate_pr_summary(
                "/project", "Title", "http://issue/1",
                ["feat: add dashboard"],
                skill_dir=Path("/skill"),
            )
            assert "http://issue/1" in result
            assert "feat: add dashboard" in result


# ---------------------------------------------------------------------------
# submit_draft_pr (shared via app.pr_submit)
# ---------------------------------------------------------------------------

class TestSubmitDraftPR:
    def test_skips_on_main_branch(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="main"):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None

    def test_returns_existing_pr_url(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", return_value="https://github.com/o/r/pull/99"):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result == "https://github.com/o/r/pull/99"

    def test_skips_when_no_commits(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", return_value=""), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=[]):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None

    def test_returns_none_on_push_failure(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", return_value=""), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_PR_MODULE}.run_git_strict", side_effect=RuntimeError("push failed")):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None

    def test_happy_path_creates_pr(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/impl-42"), \
             patch(f"{_PR_MODULE}.run_gh", side_effect=["", ""]), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["feat: add X"]), \
             patch(f"{_PR_MODULE}.run_git_strict"), \
             patch(f"{_PR_MODULE}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_PR_MODULE}.pr_create",
                    return_value="https://github.com/o/r/pull/100") as mock_pr:
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="Implement: The Title",
                pr_body="## Summary\n\nBody",
                issue_url="http://issue/42",
            )
            assert result == "https://github.com/o/r/pull/100"
            mock_pr.assert_called_once()
            call_kwargs = mock_pr.call_args[1]
            assert call_kwargs["draft"] is True
            assert "The Title" in call_kwargs["title"]

    def test_fork_workflow_uses_repo_and_head(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/impl-42"), \
             patch(f"{_PR_MODULE}.run_gh", side_effect=["", ""]), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_PR_MODULE}.run_git_strict"), \
             patch(f"{_PR_MODULE}.resolve_submit_target",
                    return_value={"repo": "upstream/repo", "is_fork": True}), \
             patch(f"{_PR_MODULE}.get_fork_owner", return_value="myfork"), \
             patch(f"{_PR_MODULE}.pr_create",
                    return_value="https://github.com/upstream/repo/pull/5") as mock_pr:
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result == "https://github.com/upstream/repo/pull/5"
            call_kwargs = mock_pr.call_args[1]
            assert call_kwargs["repo"] == "upstream/repo"
            assert call_kwargs["head"] == "myfork:koan/impl-42"

    def test_returns_none_on_pr_create_failure(self):
        with patch(f"{_PR_MODULE}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_PR_MODULE}.run_gh", side_effect=["", RuntimeError("fail")]), \
             patch(f"{_PR_MODULE}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_PR_MODULE}.run_git_strict"), \
             patch(f"{_PR_MODULE}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_PR_MODULE}.pr_create", side_effect=RuntimeError("auth fail")):
            result = submit_draft_pr(
                "/project", "myapp", "o", "r", "42",
                pr_title="T", pr_body="B",
            )
            assert result is None


# ---------------------------------------------------------------------------
# detect_parent_repo (in github.py)
# ---------------------------------------------------------------------------

class TestDetectParentRepo:
    def test_fork_detected(self):
        with patch("app.github.run_gh", return_value="upstream-owner/repo-name"):
            result = detect_parent_repo("/project")
            assert result == "upstream-owner/repo-name"

    def test_not_a_fork(self):
        with patch("app.github.run_gh", return_value=""):
            assert detect_parent_repo("/project") is None

    def test_null_parent(self):
        with patch("app.github.run_gh", return_value="null/null"):
            assert detect_parent_repo("/project") is None

    def test_gh_error(self):
        with patch("app.github.run_gh", side_effect=RuntimeError("gh failed")):
            assert detect_parent_repo("/project") is None

    def test_slash_only(self):
        with patch("app.github.run_gh", return_value="/"):
            assert detect_parent_repo("/project") is None


# ---------------------------------------------------------------------------
# get_project_submit_to_repository (in projects_config.py)
# ---------------------------------------------------------------------------

class TestGetProjectSubmitToRepository:
    def test_empty_config(self):
        config = {"defaults": {}, "projects": {"app": {"path": "/app"}}}
        assert get_project_submit_to_repository(config, "app") == {}

    def test_defaults_only(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "up/stream", "remote": "upstream"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "up/stream", "remote": "upstream"}

    def test_project_override(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "default/repo"}},
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {"repo": "custom/repo", "remote": "origin"},
                }
            },
        }
        result = get_project_submit_to_repository(config, "app")
        assert result["repo"] == "custom/repo"
        assert result["remote"] == "origin"

    def test_non_dict_value(self):
        config = {
            "defaults": {"submit_to_repository": "invalid"},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_submit_to_repository(config, "app") == {}

    def test_partial_config(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "up/stream"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "up/stream"}
        assert "remote" not in result


# ---------------------------------------------------------------------------
# run_implement — updated integration tests
# ---------------------------------------------------------------------------

class TestRunImplementWithPR:
    """Tests verifying PR submission is called after successful implementation."""

    def test_pr_url_in_summary_on_success(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    return_value="https://github.com/o/r/pull/99"), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/feat"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "https://github.com/o/r/pull/99" in msg

    def test_branch_in_summary_when_pr_fails(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/impl-42"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "koan/impl-42" in msg

    def test_warning_when_on_main(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr", return_value=None), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="main"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok
            assert "no PR" in msg

    def test_pr_submission_exception_does_not_fail_mission(self):
        notify = MagicMock()
        body = "### Summary\nPlan\n#### Phase 1: Do it"
        with patch(f"{_IMPL_MODULE}.fetch_issue_with_comments",
                    return_value=("Title", body, [])), \
             patch(f"{_IMPL_MODULE}._execute_implementation", return_value="Done"), \
             patch(f"{_IMPL_MODULE}._submit_implement_pr",
                    side_effect=RuntimeError("unexpected")), \
             patch(f"{_IMPL_MODULE}.get_current_branch", return_value="koan/feat"):
            ok, msg = run_implement(
                "/project",
                "https://github.com/o/r/issues/42",
                notify_fn=notify,
            )
            assert ok


# ---------------------------------------------------------------------------
# main — CLI entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_success_exit_code(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")):
            code = main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
            ])
            assert code == 0

    def test_failure_exit_code(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(False, "failed")):
            code = main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
            ])
            assert code == 1

    def test_context_arg_passed(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
                "--context", "Phase 1 to 3",
            ])
            _, kwargs = mock.call_args
            assert kwargs["context"] == "Phase 1 to 3"

    def test_context_defaults_to_none(self):
        with patch(f"{_IMPL_MODULE}.run_implement",
                    return_value=(True, "ok")) as mock:
            main([
                "--project-path", "/project",
                "--issue-url", "https://github.com/o/r/issues/1",
            ])
            _, kwargs = mock.call_args
            assert kwargs["context"] is None
