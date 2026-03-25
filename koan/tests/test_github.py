"""Tests for app.github — shared gh CLI wrapper."""

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from app.github import (
    SSOAuthRequired, _is_sso_error,
    run_gh, pr_create, issue_create, api,
    get_gh_username, count_open_prs, cached_count_open_prs,
    batch_count_open_prs, fetch_issue_with_comments, detect_parent_repo,
    resolve_target_repo, _upstream_remote_repo, _parse_remote_url,
)
import app.github as github_module


# ---------------------------------------------------------------------------
# run_gh
# ---------------------------------------------------------------------------

class TestRunGh:
    @patch("app.github.subprocess.run")
    def test_returns_stripped_stdout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  output\n")
        assert run_gh("pr", "view", "1") == "output"

    @patch("app.github.subprocess.run")
    def test_passes_cwd_and_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_gh("repo", "view", cwd="/project", timeout=10)
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == "/project"
        assert mock_run.call_args.kwargs["timeout"] == 10

    @patch("app.github.subprocess.run")
    def test_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        run_gh("pr", "view", "42", "--repo", "owner/repo")
        cmd = mock_run.call_args[0][0]
        assert cmd == ["gh", "pr", "view", "42", "--repo", "owner/repo"]

    @patch("app.github.subprocess.run")
    def test_raises_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        with pytest.raises(RuntimeError, match="gh failed"):
            run_gh("pr", "view", "999")

    @patch("app.github.subprocess.run")
    def test_error_message_includes_stderr(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="auth required")
        with pytest.raises(RuntimeError, match="auth required"):
            run_gh("api", "repos/o/r")

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_timeout_propagates(self, mock_run, mock_sleep):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=5)
        with pytest.raises(subprocess.TimeoutExpired):
            run_gh("pr", "view", "1", timeout=5)

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_retries_on_transient_network_error(self, mock_run, mock_sleep):
        """Transient network errors trigger retry with backoff."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="connection reset by peer"),
            MagicMock(returncode=0, stdout="ok\n"),
        ]
        assert run_gh("pr", "view", "1") == "ok"
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_no_retry_on_permanent_error(self, mock_run, mock_sleep):
        """Permanent errors (not found, auth) propagate immediately."""
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        with pytest.raises(RuntimeError, match="not found"):
            run_gh("pr", "view", "999")
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_retries_exhausted_raises(self, mock_run, mock_sleep):
        """After all retry attempts, the last error is raised."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="connection timed out"),
            MagicMock(returncode=1, stderr="connection timed out"),
            MagicMock(returncode=1, stderr="connection timed out"),
        ]
        with pytest.raises(RuntimeError, match="connection timed out"):
            run_gh("api", "repos/o/r")
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("app.github.subprocess.run")
    def test_raises_sso_auth_required_on_sso_error(self, mock_run):
        """SSO/SAML errors raise SSOAuthRequired instead of RuntimeError."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="Resource protected by organization SAML enforcement. "
                   "You must grant your OAuth token access to this organization.",
        )
        with pytest.raises(SSOAuthRequired, match="SSO/SAML authorization required"):
            run_gh("api", "repos/enterprise-org/repo")

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_sso_error_not_retried(self, mock_run, mock_sleep):
        """SSO errors are not transient — should not be retried."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="SSO authorization required for this resource",
        )
        with pytest.raises(SSOAuthRequired):
            run_gh("api", "repos/org/repo")
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_secondary_rate_limit_never_retried_even_when_idempotent(self, mock_run, mock_sleep):
        """Secondary rate limits are never retried — retrying escalates GitHub's response."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="You have exceeded a secondary rate limit"
        )
        with pytest.raises(RuntimeError, match="secondary rate limit"):
            run_gh("api", "repos/o/r", idempotent=True)
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_secondary_rate_limit_never_retried_when_not_idempotent(self, mock_run, mock_sleep):
        """Secondary rate limits are NOT retried when idempotent=False."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="You have exceeded a secondary rate limit"
        )
        with pytest.raises(RuntimeError, match="secondary rate limit"):
            run_gh("pr", "create", "--title", "T", "--body", "B", idempotent=False)
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.retry.time.sleep")
    @patch("app.github.subprocess.run")
    def test_retry_after_header_respected(self, mock_run, mock_sleep):
        """When gh reports a Retry-After value, that delay is used instead of backoff."""
        # 429 matches transient keywords; Retry-After: 30 provides the delay
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="429 too many requests — Retry-After: 30"),
            MagicMock(returncode=0, stdout="ok\n"),
        ]
        assert run_gh("api", "repos/o/r") == "ok"
        mock_sleep.assert_called_once_with(30.0)


# ---------------------------------------------------------------------------
# _is_sso_error
# ---------------------------------------------------------------------------

class TestIsSsoError:
    def test_detects_sso_keyword(self):
        assert _is_sso_error("You must grant your SSO token access") is True

    def test_detects_saml_keyword(self):
        assert _is_sso_error("Resource protected by organization SAML enforcement") is True

    def test_case_insensitive(self):
        assert _is_sso_error("sso authorization required") is True
        assert _is_sso_error("saml enforcement") is True

    def test_non_sso_error(self):
        assert _is_sso_error("not found") is False
        assert _is_sso_error("auth required") is False


# ---------------------------------------------------------------------------
# SSOAuthRequired
# ---------------------------------------------------------------------------

class TestSSOAuthRequired:
    def test_is_runtime_error_subclass(self):
        exc = SSOAuthRequired("test stderr")
        assert isinstance(exc, RuntimeError)

    def test_includes_remediation(self):
        exc = SSOAuthRequired("SAML enforcement error")
        assert "gh auth refresh" in str(exc)

    def test_stores_stderr(self):
        exc = SSOAuthRequired("original stderr")
        assert exc.stderr_text == "original stderr"


# ---------------------------------------------------------------------------
# pr_create
# ---------------------------------------------------------------------------

class TestPrCreate:
    @patch("app.github.subprocess.run")
    def test_defaults_to_draft(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/o/r/pull/1"
        )
        url = pr_create("My PR", "Description")
        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd
        assert "pull/1" in url

    @patch("app.github.subprocess.run")
    def test_draft_false_omits_flag(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/o/r/pull/2"
        )
        pr_create("My PR", "Description", draft=False)
        cmd = mock_run.call_args[0][0]
        assert "--draft" not in cmd

    @patch("app.github.subprocess.run")
    def test_includes_base_when_provided(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("Title", "Body", base="develop")
        cmd = mock_run.call_args[0][0]
        assert "--base" in cmd
        idx = cmd.index("--base")
        assert cmd[idx + 1] == "develop"

    @patch("app.github.subprocess.run")
    def test_no_base_omits_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("Title", "Body")
        cmd = mock_run.call_args[0][0]
        assert "--base" not in cmd

    @patch("app.github.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("Title", "Body", cwd="/my/project")
        assert mock_run.call_args.kwargs["cwd"] == "/my/project"

    @patch("app.github.subprocess.run")
    def test_passes_title_and_body(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("My Title", "My Body")
        cmd = mock_run.call_args[0][0]
        assert "--title" in cmd
        assert "My Title" in cmd
        assert "--body" in cmd
        assert "My Body" in cmd


# ---------------------------------------------------------------------------
# issue_create
# ---------------------------------------------------------------------------

class TestIssueCreate:
    @patch("app.github.subprocess.run")
    def test_creates_issue(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/o/r/issues/42"
        )
        url = issue_create("Bug Title", "Bug description")
        assert "issues/42" in url
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["gh", "issue"]
        assert "--title" in cmd
        assert "Bug Title" in cmd

    @patch("app.github.subprocess.run")
    def test_with_labels(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        issue_create("Title", "Body", labels=["bug", "priority"])
        cmd = mock_run.call_args[0][0]
        assert "--label" in cmd
        idx = cmd.index("--label")
        assert cmd[idx + 1] == "bug,priority"

    @patch("app.github.subprocess.run")
    def test_no_labels_omits_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        issue_create("Title", "Body")
        cmd = mock_run.call_args[0][0]
        assert "--label" not in cmd

    @patch("app.github.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        issue_create("Title", "Body", cwd="/project")
        assert mock_run.call_args.kwargs["cwd"] == "/project"


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------

class TestApi:
    @patch("app.github.subprocess.run")
    def test_get_request(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"id": 1}')
        result = api("repos/owner/repo/issues/1")
        assert '"id": 1' in result
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["gh", "api", "repos/owner/repo/issues/1"]
        # GET should not add -X flag
        assert "-X" not in cmd

    @patch("app.github.subprocess.run")
    def test_with_jq_filter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="filtered")
        api("repos/o/r/issues", jq=".[] | .title")
        cmd = mock_run.call_args[0][0]
        assert "--jq" in cmd
        idx = cmd.index("--jq")
        assert cmd[idx + 1] == ".[] | .title"

    @patch("app.github.subprocess.run")
    def test_post_with_input_data(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        api("repos/o/r/issues/1/comments", input_data="My comment body")
        assert mock_run.call_args.kwargs.get("input") == "My comment body"
        cmd = mock_run.call_args[0][0]
        assert "-F" in cmd
        assert "body=@-" in cmd

    @patch("app.github.subprocess.run")
    def test_explicit_method(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        api("repos/o/r/issues/1", method="PATCH")
        cmd = mock_run.call_args[0][0]
        assert "-X" in cmd
        idx = cmd.index("-X")
        assert cmd[idx + 1] == "PATCH"

    @patch("app.github.subprocess.run")
    def test_extra_args(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        api("repos/o/r/pulls/1/comments", extra_args=["--paginate"])
        cmd = mock_run.call_args[0][0]
        assert "--paginate" in cmd

    @patch("app.github.subprocess.run")
    def test_raises_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        with pytest.raises(RuntimeError, match="gh failed"):
            api("repos/o/r/nonexistent")

    @patch("app.github.subprocess.run")
    def test_input_data_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="forbidden")
        with pytest.raises(RuntimeError, match="gh failed"):
            api("repos/o/r/issues/1/comments", input_data="body")


# ---------------------------------------------------------------------------
# get_gh_username
# ---------------------------------------------------------------------------

class TestGetGhUsername:

    def setup_method(self):
        """Reset cached username between tests."""
        github_module._cached_gh_username = None

    @patch("app.github_auth.get_github_user", return_value="koan-bot")
    def test_returns_github_user_env(self, mock_get_user):
        assert get_gh_username() == "koan-bot"

    @patch("app.github_auth.get_github_user", return_value="")
    @patch("app.github.run_gh", return_value="fallback-user")
    def test_falls_back_to_gh_api(self, mock_gh, mock_get_user):
        assert get_gh_username() == "fallback-user"
        mock_gh.assert_called_once_with("api", "user", "--jq", ".login", timeout=15)

    @patch("app.github_auth.get_github_user", return_value="")
    @patch("app.github.run_gh", side_effect=RuntimeError("not logged in"))
    def test_returns_empty_on_failure(self, mock_gh, mock_get_user):
        assert get_gh_username() == ""

    @patch("app.github_auth.get_github_user", return_value="")
    @patch("app.github.run_gh", return_value="cached-user")
    def test_caches_gh_api_result(self, mock_gh, mock_get_user):
        assert get_gh_username() == "cached-user"
        assert get_gh_username() == "cached-user"
        # Only one call to run_gh despite two invocations
        mock_gh.assert_called_once()

    @patch("app.github_auth.get_github_user", return_value="")
    @patch("app.github.run_gh", side_effect=RuntimeError("fail"))
    def test_caches_failure_as_empty(self, mock_gh, mock_get_user):
        assert get_gh_username() == ""
        assert get_gh_username() == ""
        # Only one call — failure is cached too
        mock_gh.assert_called_once()

    @patch("app.github_auth.get_github_user", return_value="env-user")
    def test_env_var_takes_priority_over_cache(self, mock_get_user):
        # Pre-populate cache
        github_module._cached_gh_username = "cached-user"
        assert get_gh_username() == "env-user"


# ---------------------------------------------------------------------------
# count_open_prs
# ---------------------------------------------------------------------------

class TestCountOpenPrs:

    @patch("app.github.run_gh", return_value="5")
    def test_returns_count(self, mock_gh):
        assert count_open_prs("owner/repo", "koan-bot") == 5
        mock_gh.assert_called_once_with(
            "pr", "list",
            "--repo", "owner/repo",
            "--state", "open",
            "--author", "koan-bot",
            "--json", "number",
            "--jq", "length",
            cwd=None, timeout=15,
        )

    @patch("app.github.run_gh", return_value="0")
    def test_returns_zero_when_no_prs(self, mock_gh):
        assert count_open_prs("owner/repo", "koan-bot") == 0

    @patch("app.github.run_gh", side_effect=RuntimeError("auth error"))
    def test_returns_negative_one_on_error(self, mock_gh):
        assert count_open_prs("owner/repo", "koan-bot") == -1

    @patch("app.github.run_gh", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15))
    def test_returns_negative_one_on_timeout(self, mock_gh):
        assert count_open_prs("owner/repo", "koan-bot") == -1

    def test_returns_negative_one_for_empty_author(self):
        assert count_open_prs("owner/repo", "") == -1

    @patch("app.github.run_gh", return_value="not-a-number")
    def test_returns_negative_one_on_non_numeric_output(self, mock_gh):
        assert count_open_prs("owner/repo", "koan-bot") == -1

    @patch("app.github.run_gh", return_value="3")
    def test_passes_cwd(self, mock_gh):
        count_open_prs("owner/repo", "koan-bot", cwd="/my/project")
        assert mock_gh.call_args.kwargs["cwd"] == "/my/project"

    @patch("app.github.run_gh", return_value="")
    def test_empty_output_returns_negative_one(self, mock_gh):
        assert count_open_prs("owner/repo", "koan-bot") == -1


# ---------------------------------------------------------------------------
# batch_count_open_prs
# ---------------------------------------------------------------------------


class TestBatchCountOpenPrs:

    def setup_method(self):
        from app.github import _pr_count_cache
        _pr_count_cache.clear()

    @patch("app.github.run_gh")
    def test_single_repo(self, mock_gh):
        mock_gh.return_value = json.dumps({
            "data": {"r0": {"issueCount": 3}},
        })
        result = batch_count_open_prs(["owner/repo"], "koan-bot")
        assert result == {"owner/repo": 3}
        mock_gh.assert_called_once()
        # Verify GraphQL query structure
        call_args = mock_gh.call_args
        assert call_args[0][:2] == ("api", "graphql")

    @patch("app.github.run_gh")
    def test_multiple_repos(self, mock_gh):
        mock_gh.return_value = json.dumps({
            "data": {
                "r0": {"issueCount": 2},
                "r1": {"issueCount": 5},
                "r2": {"issueCount": 0},
            },
        })
        repos = ["owner/alpha", "owner/beta", "other/gamma"]
        result = batch_count_open_prs(repos, "koan-bot")
        assert result == {
            "owner/alpha": 2,
            "owner/beta": 5,
            "other/gamma": 0,
        }
        # Single GraphQL call for all repos
        assert mock_gh.call_count == 1

    @patch("app.github.run_gh")
    def test_populates_cache(self, mock_gh):
        from app.github import _pr_count_cache
        mock_gh.return_value = json.dumps({
            "data": {"r0": {"issueCount": 7}},
        })
        batch_count_open_prs(["owner/repo"], "koan-bot")
        assert "owner/repo:koan-bot" in _pr_count_cache
        assert _pr_count_cache["owner/repo:koan-bot"][0] == 7

    @patch("app.github.run_gh", side_effect=RuntimeError("auth error"))
    def test_returns_empty_on_error(self, mock_gh):
        result = batch_count_open_prs(["owner/repo"], "koan-bot")
        assert result == {}

    def test_empty_repos_returns_empty(self):
        assert batch_count_open_prs([], "koan-bot") == {}

    def test_empty_author_returns_empty(self):
        assert batch_count_open_prs(["owner/repo"], "") == {}

    @patch("app.github.run_gh")
    def test_deduplicates_repos(self, mock_gh):
        mock_gh.return_value = json.dumps({
            "data": {"r0": {"issueCount": 4}},
        })
        result = batch_count_open_prs(
            ["owner/repo", "owner/repo", "owner/repo"], "koan-bot",
        )
        assert result == {"owner/repo": 4}
        # Query should only have one alias (r0)
        query_arg = mock_gh.call_args[0][3]  # -f arg value
        assert "r1:" not in query_arg

    @patch("app.github.run_gh")
    def test_partial_response_returns_available(self, mock_gh):
        """If GraphQL returns data for some repos but not all."""
        mock_gh.return_value = json.dumps({
            "data": {
                "r0": {"issueCount": 3},
                "r1": None,
            },
        })
        result = batch_count_open_prs(["owner/a", "owner/b"], "koan-bot")
        assert result == {"owner/a": 3}


# ---------------------------------------------------------------------------
# run_gh — stdin_data
# ---------------------------------------------------------------------------


class TestRunGhStdinData:

    @patch("app.github.subprocess.run")
    def test_stdin_data_passes_input(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_gh("api", "endpoint", stdin_data="my input")
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["input"] == "my input"
        # stdin should NOT be set when using input
        assert "stdin" not in call_kwargs

    @patch("app.github.subprocess.run")
    def test_no_stdin_data_uses_devnull(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_gh("pr", "view", "1")
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["stdin"] == subprocess.DEVNULL
        assert "input" not in call_kwargs


# ---------------------------------------------------------------------------
# fetch_issue_with_comments
# ---------------------------------------------------------------------------


class TestFetchIssueWithComments:

    @patch("app.github.api")
    def test_returns_title_body_comments(self, mock_api):
        issue_json = json.dumps({"title": "Bug report", "body": "It's broken"})
        comments_json = json.dumps([
            {"author": "user1", "date": "2026-01-01", "body": "I agree"},
            {"author": "user2", "date": "2026-01-02", "body": "Fixed"},
        ])
        mock_api.side_effect = [issue_json, comments_json]

        title, body, comments = fetch_issue_with_comments("owner", "repo", 42)

        assert title == "Bug report"
        assert body == "It's broken"
        assert len(comments) == 2
        assert comments[0]["author"] == "user1"
        assert comments[1]["body"] == "Fixed"

    @patch("app.github.api")
    def test_calls_correct_endpoints(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B"}),
            json.dumps([]),
        ]
        fetch_issue_with_comments("sukria", "koan", 99)

        calls = mock_api.call_args_list
        assert calls[0][0][0] == "repos/sukria/koan/issues/99"
        assert calls[1][0][0] == "repos/sukria/koan/issues/99/comments"

    @patch("app.github.api")
    def test_handles_malformed_issue_json(self, mock_api):
        mock_api.side_effect = ["not json", json.dumps([])]

        title, body, comments = fetch_issue_with_comments("o", "r", 1)

        # Falls back to raw text as body
        assert title == ""
        assert body == "not json"
        assert comments == []

    @patch("app.github.api")
    def test_handles_malformed_comments_json(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B"}),
            "not json",
        ]

        title, body, comments = fetch_issue_with_comments("o", "r", 1)

        assert title == "T"
        assert body == "B"
        assert comments == []

    @patch("app.github.api")
    def test_handles_comments_not_a_list(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B"}),
            json.dumps({"unexpected": "object"}),
        ]

        title, body, comments = fetch_issue_with_comments("o", "r", 1)
        assert comments == []

    @patch("app.github.api")
    def test_empty_comments(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "T", "body": "B"}),
            json.dumps([]),
        ]

        title, body, comments = fetch_issue_with_comments("o", "r", 1)
        assert comments == []

    @patch("app.github.api")
    def test_missing_title_defaults_empty(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"body": "only body"}),
            json.dumps([]),
        ]

        title, body, comments = fetch_issue_with_comments("o", "r", 1)
        assert title == ""
        assert body == "only body"

    @patch("app.github.api")
    def test_missing_body_defaults_empty(self, mock_api):
        mock_api.side_effect = [
            json.dumps({"title": "only title"}),
            json.dumps([]),
        ]

        title, body, comments = fetch_issue_with_comments("o", "r", 1)
        assert title == "only title"
        assert body == ""

    @patch("app.github.api", side_effect=RuntimeError("gh failed"))
    def test_propagates_api_error(self, mock_api):
        with pytest.raises(RuntimeError, match="gh failed"):
            fetch_issue_with_comments("o", "r", 1)


# ---------------------------------------------------------------------------
# detect_parent_repo
# ---------------------------------------------------------------------------


class TestDetectParentRepo:

    @patch("app.github.run_gh", return_value="upstream-owner/upstream-repo")
    def test_returns_parent_repo(self, mock_gh):
        result = detect_parent_repo("/my/fork")
        assert result == "upstream-owner/upstream-repo"
        mock_gh.assert_called_once_with(
            "repo", "view", "--json", "parent",
            "--jq", '.parent.owner.login + "/" + .parent.name',
            cwd="/my/fork", timeout=15,
        )

    @patch("app.github.run_gh", return_value="")
    def test_returns_none_for_empty_output(self, mock_gh):
        assert detect_parent_repo("/not/a/fork") is None

    @patch("app.github.run_gh", return_value="/")
    def test_returns_none_for_slash_only(self, mock_gh):
        assert detect_parent_repo("/not/a/fork") is None

    @patch("app.github.run_gh", return_value="null/null")
    def test_returns_none_for_null_parent(self, mock_gh):
        assert detect_parent_repo("/not/a/fork") is None

    @patch("app.github.run_gh", return_value="just-one-part")
    def test_returns_none_for_invalid_format(self, mock_gh):
        assert detect_parent_repo("/some/path") is None

    @patch("app.github.run_gh", return_value="/repo-only")
    def test_returns_none_for_empty_owner(self, mock_gh):
        assert detect_parent_repo("/some/path") is None

    @patch("app.github.run_gh", return_value="owner-only/")
    def test_returns_none_for_empty_repo(self, mock_gh):
        assert detect_parent_repo("/some/path") is None

    @patch("app.github.run_gh", side_effect=RuntimeError("not found"))
    def test_returns_none_on_error(self, mock_gh):
        assert detect_parent_repo("/nonexistent") is None

    @patch("app.github.run_gh", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15))
    def test_returns_none_on_timeout(self, mock_gh):
        assert detect_parent_repo("/slow/repo") is None

    @patch("app.github.run_gh", return_value="  owner/repo  ")
    def test_strips_whitespace(self, mock_gh):
        assert detect_parent_repo("/my/fork") == "owner/repo"


# ---------------------------------------------------------------------------
# resolve_target_repo — fork detection with upstream remote fallback
# ---------------------------------------------------------------------------


class TestResolveTargetRepo:

    @patch("app.github.detect_parent_repo", return_value="upstream/repo")
    def test_prefers_github_parent(self, mock_detect):
        assert resolve_target_repo("/proj") == "upstream/repo"

    @patch("app.github.detect_parent_repo", return_value=None)
    @patch("app.github._upstream_remote_repo", return_value="org/repo")
    def test_falls_back_to_upstream_remote(self, mock_remote, mock_detect):
        assert resolve_target_repo("/proj") == "org/repo"

    @patch("app.github.detect_parent_repo", return_value=None)
    @patch("app.github._upstream_remote_repo", return_value=None)
    def test_returns_none_when_no_upstream(self, mock_remote, mock_detect):
        assert resolve_target_repo("/proj") is None


class TestUpstreamRemoteRepo:

    @patch("app.github._get_remote_url")
    def test_returns_upstream_when_different_from_origin(self, mock_url):
        mock_url.side_effect = lambda path, remote: {
            "upstream": "git@github.com:Anantys-oss/koan.git",
            "origin": "https://github.com/Koan-Bot/koan.git",
        }.get(remote)
        assert _upstream_remote_repo("/proj") == "Anantys-oss/koan"

    @patch("app.github._get_remote_url")
    def test_returns_none_when_same_as_origin(self, mock_url):
        mock_url.side_effect = lambda path, remote: {
            "upstream": "git@github.com:owner/repo.git",
            "origin": "https://github.com/owner/repo.git",
        }.get(remote)
        assert _upstream_remote_repo("/proj") is None

    @patch("app.github._get_remote_url")
    def test_returns_none_when_no_upstream(self, mock_url):
        mock_url.side_effect = lambda path, remote: {
            "origin": "https://github.com/owner/repo.git",
        }.get(remote)
        assert _upstream_remote_repo("/proj") is None

    @patch("app.github._get_remote_url")
    def test_returns_upstream_when_no_origin(self, mock_url):
        mock_url.side_effect = lambda path, remote: {
            "upstream": "git@github.com:org/repo.git",
        }.get(remote)
        assert _upstream_remote_repo("/proj") == "org/repo"


class TestParseRemoteUrl:

    def test_https_url(self):
        assert _parse_remote_url("https://github.com/owner/repo.git") == "owner/repo"

    def test_ssh_url(self):
        assert _parse_remote_url("git@github.com:owner/repo.git") == "owner/repo"

    def test_https_without_git_suffix(self):
        assert _parse_remote_url("https://github.com/owner/repo") == "owner/repo"

    def test_non_github_url(self):
        assert _parse_remote_url("https://gitlab.com/owner/repo.git") is None


# ---------------------------------------------------------------------------
# issue_create — repo parameter
# ---------------------------------------------------------------------------


class TestIssueCreateRepo:

    @patch("app.github.run_gh", return_value="https://github.com/org/repo/issues/1")
    @patch("app.leak_detector.scan_and_redact", side_effect=lambda x, **kw: x)
    def test_passes_repo_flag(self, mock_redact, mock_gh):
        issue_create("title", "body", repo="upstream/repo")
        args = mock_gh.call_args[0]
        assert "--repo" in args
        idx = args.index("--repo")
        assert args[idx + 1] == "upstream/repo"

    @patch("app.github.run_gh", return_value="https://github.com/org/repo/issues/1")
    @patch("app.leak_detector.scan_and_redact", side_effect=lambda x, **kw: x)
    def test_omits_repo_when_none(self, mock_redact, mock_gh):
        issue_create("title", "body")
        args = mock_gh.call_args[0]
        assert "--repo" not in args


# ---------------------------------------------------------------------------
# pr_create — repo and head parameters
# ---------------------------------------------------------------------------


class TestPrCreateExtended:

    @patch("app.github.subprocess.run")
    def test_passes_repo_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("Title", "Body", repo="upstream/repo")
        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        idx = cmd.index("--repo")
        assert cmd[idx + 1] == "upstream/repo"

    @patch("app.github.subprocess.run")
    def test_passes_head_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("Title", "Body", head="user:branch")
        cmd = mock_run.call_args[0][0]
        assert "--head" in cmd
        idx = cmd.index("--head")
        assert cmd[idx + 1] == "user:branch"

    @patch("app.github.subprocess.run")
    def test_no_repo_no_head_omits_flags(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url")
        pr_create("Title", "Body")
        cmd = mock_run.call_args[0][0]
        assert "--repo" not in cmd
        assert "--head" not in cmd


# ---------------------------------------------------------------------------
# cached_count_open_prs
# ---------------------------------------------------------------------------


class TestCachedCountOpenPrs:

    def setup_method(self):
        """Clear the PR count cache between tests."""
        github_module._pr_count_cache.clear()

    @patch("app.github.count_open_prs", return_value=5)
    def test_returns_count(self, mock_count):
        assert cached_count_open_prs("owner/repo", "koan-bot") == 5
        mock_count.assert_called_once_with("owner/repo", "koan-bot")

    @patch("app.github.count_open_prs", return_value=3)
    def test_caches_result(self, mock_count):
        assert cached_count_open_prs("owner/repo", "koan-bot") == 3
        assert cached_count_open_prs("owner/repo", "koan-bot") == 3
        # Only one call despite two invocations
        mock_count.assert_called_once()

    @patch("app.github.count_open_prs", return_value=-1)
    def test_caches_errors(self, mock_count):
        """Errors (-1) are cached too to avoid hammering gh."""
        assert cached_count_open_prs("owner/repo", "koan-bot") == -1
        assert cached_count_open_prs("owner/repo", "koan-bot") == -1
        mock_count.assert_called_once()

    @patch("app.github.count_open_prs", return_value=2)
    def test_different_repos_cached_independently(self, mock_count):
        cached_count_open_prs("owner/repo-a", "koan-bot")
        cached_count_open_prs("owner/repo-b", "koan-bot")
        assert mock_count.call_count == 2

    @patch("app.github.count_open_prs", return_value=7)
    @patch("app.github.time.monotonic")
    def test_ttl_expiry_refreshes(self, mock_time, mock_count):
        """After TTL expires, the cache is refreshed."""
        mock_time.return_value = 1000.0
        assert cached_count_open_prs("owner/repo", "koan-bot") == 7

        # Advance time past TTL (300s)
        mock_time.return_value = 1301.0
        assert cached_count_open_prs("owner/repo", "koan-bot") == 7
        assert mock_count.call_count == 2

    @patch("app.github.count_open_prs", return_value=4)
    @patch("app.github.time.monotonic")
    def test_within_ttl_uses_cache(self, mock_time, mock_count):
        """Within TTL, cached value is returned without gh call."""
        mock_time.return_value = 1000.0
        cached_count_open_prs("owner/repo", "koan-bot")

        # Still within TTL
        mock_time.return_value = 1299.0
        cached_count_open_prs("owner/repo", "koan-bot")
        mock_count.assert_called_once()
