"""Tests for app/pr_submit.py — shared PR submission helpers."""

from unittest.mock import patch, MagicMock

from app.pr_submit import (
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
    @patch(f"{_M}.run_gh", return_value="myuser\n")
    def test_returns_stripped(self, mock):
        assert get_fork_owner("/p") == "myuser"

    @patch(f"{_M}.run_gh", side_effect=RuntimeError("gh not found"))
    def test_error_returns_empty(self, mock):
        assert get_fork_owner("/p") == ""


# ---------------------------------------------------------------------------
# resolve_submit_target
# ---------------------------------------------------------------------------

class TestResolveSubmitTarget:
    @patch(f"{_M}.detect_parent_repo", return_value=None)
    @patch.dict("os.environ", {"KOAN_ROOT": ""})
    def test_fallback_to_owner_repo(self, mock):
        result = resolve_submit_target("/p", "proj", "owner", "repo")
        assert result == {"repo": "owner/repo", "is_fork": False}

    @patch(f"{_M}.detect_parent_repo", return_value="upstream/repo")
    @patch.dict("os.environ", {"KOAN_ROOT": ""})
    def test_fork_detected(self, mock):
        result = resolve_submit_target("/p", "proj", "o", "r")
        assert result == {"repo": "upstream/repo", "is_fork": True}

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
# submit_draft_pr
# ---------------------------------------------------------------------------

class TestSubmitDraftPr:
    def test_skips_on_main(self):
        with patch(f"{_M}.get_current_branch", return_value="main"):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_skips_on_master(self):
        with patch(f"{_M}.get_current_branch", return_value="master"):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_returns_existing_pr(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.run_gh", return_value="https://pr/1"):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") == "https://pr/1"

    def test_no_commits_returns_none(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=[]):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_push_failure_returns_none(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict", side_effect=RuntimeError("push")):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_creates_pr_with_correct_kwargs(self):
        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
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
            assert kw["body"] == "## Summary\nFixed."
            assert kw["draft"] is True

    def test_fork_workflow_sets_repo_and_head(self):
        with patch(f"{_M}.get_current_branch", return_value="koan/feat"), \
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

    def test_pr_create_failure_returns_none(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.run_gh", return_value=""), \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", side_effect=RuntimeError("auth")):
            assert submit_draft_pr("/p", "proj", "o", "r", "1", "T", "B") is None

    def test_issue_comment_posted_when_url_given(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.run_gh", side_effect=["", ""]) as mock_gh, \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/1"):
            submit_draft_pr(
                "/p", "proj", "o", "r", "42",
                pr_title="T", pr_body="B",
                issue_url="https://issue/42",
            )
            # Second run_gh call should be the issue comment
            calls = mock_gh.call_args_list
            assert len(calls) >= 2
            comment_call = calls[1]
            assert "issue" in comment_call[0]
            assert "comment" in comment_call[0]

    def test_no_issue_comment_when_no_url(self):
        with patch(f"{_M}.get_current_branch", return_value="feat"), \
             patch(f"{_M}.run_gh", return_value="") as mock_gh, \
             patch(f"{_M}.get_commit_subjects", return_value=["c1"]), \
             patch(f"{_M}.run_git_strict"), \
             patch(f"{_M}.resolve_submit_target",
                    return_value={"repo": "o/r", "is_fork": False}), \
             patch(f"{_M}.pr_create", return_value="https://pr/1"):
            submit_draft_pr("/p", "proj", "o", "r", "42", "T", "B")
            # Only 1 gh call (the PR check), no issue comment
            assert mock_gh.call_count == 1
