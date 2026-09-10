"""Tests for app.github — shared gh CLI wrapper."""

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from app.github import (
    SSOAuthRequired, _is_sso_error,
    run_gh, pr_create, issue_create, api,
    get_gh_username, count_open_prs, cached_count_open_prs,
    batch_count_open_prs, fetch_issue_state, fetch_issue_with_comments,
    detect_parent_repo, resolve_target_repo, _config_target_repo,
    _upstream_remote_repo,
    origin_repo, _parse_remote_url,
    sanitize_github_comment,
    escape_issue_refs,
    find_bot_comment,
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
    def test_max_attempts_one_skips_timeout_retry(self, mock_run, mock_sleep):
        """Hot-path callers can disable retry amplification on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=15)
        with pytest.raises(subprocess.TimeoutExpired):
            run_gh("api", "repos/o/r/pulls/1/comments", timeout=15, max_attempts=1)
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

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
    @patch("app.github.run_gh", side_effect=RuntimeError("network error"))
    def test_logs_warning_on_failure(self, mock_gh, mock_get_user, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="app.github"):
            assert get_gh_username() == ""
        assert "get_gh_username failed" in caplog.text
        assert "network error" in caplog.text

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


class TestFetchIssueState:

    @patch("app.github.api", return_value='"closed"')
    def test_returns_closed(self, mock_api):
        assert fetch_issue_state("o", "r", 42) == "closed"
        mock_api.assert_called_once()

    @patch("app.github.api", return_value='"open"')
    def test_returns_open(self, mock_api):
        assert fetch_issue_state("o", "r", 42) == "open"

    @patch("app.github.api", return_value="unexpected")
    def test_unknown_state_defaults_open(self, mock_api):
        assert fetch_issue_state("o", "r", 42) == "open"

    @patch("app.github.api", side_effect=RuntimeError("gh failed"))
    def test_api_error_defaults_open(self, mock_api):
        assert fetch_issue_state("o", "r", 42) == "open"

    @patch("app.github.api", side_effect=RuntimeError("connection refused"))
    def test_logs_warning_on_api_error(self, mock_api, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="app.github"):
            assert fetch_issue_state("o", "r", 42) == "open"
        assert "fetch_issue_state failed" in caplog.text
        assert "connection refused" in caplog.text


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

    @patch("app.github._config_target_repo")
    @patch("app.github.detect_parent_repo")
    def test_config_override_skips_fork_detection(self, mock_detect, mock_cfg):
        from app.github import _UNSET
        mock_cfg.return_value = None  # origin IS canonical
        result = resolve_target_repo("/proj", project_name="XML-Parser")
        assert result is None
        mock_detect.assert_not_called()

    @patch("app.github._config_target_repo")
    @patch("app.github.detect_parent_repo")
    def test_config_override_returns_different_repo(self, mock_detect, mock_cfg):
        mock_cfg.return_value = "other-owner/repo"
        result = resolve_target_repo("/proj", project_name="my-fork")
        assert result == "other-owner/repo"
        mock_detect.assert_not_called()

    @patch("app.github._config_target_repo")
    @patch("app.github.detect_parent_repo", return_value="parent/repo")
    def test_no_config_falls_through_to_fork_detection(self, mock_detect, mock_cfg):
        from app.github import _UNSET
        mock_cfg.return_value = _UNSET
        result = resolve_target_repo("/proj", project_name="unconfigured")
        assert result == "parent/repo"


class TestConfigTargetRepo:

    @patch("app.github._get_remote_url", return_value="https://github.com/cpan-authors/XML-Parser.git")
    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    @patch("app.projects_config.get_project_submit_to_repository",
           return_value={"repo": "cpan-authors/XML-Parser"})
    def test_origin_matches_config_returns_none(self, mock_submit, mock_cfg, mock_url):
        with patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"}):
            result = _config_target_repo("/proj", "XML-Parser")
            assert result is None

    @patch("app.github._get_remote_url", return_value="https://github.com/my-fork/repo.git")
    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    @patch("app.projects_config.get_project_submit_to_repository",
           return_value={"repo": "upstream/repo"})
    def test_origin_differs_from_config_returns_config(self, mock_submit, mock_cfg, mock_url):
        with patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"}):
            result = _config_target_repo("/proj", "my-fork")
            assert result == "upstream/repo"

    def test_no_koan_root_returns_unset(self):
        from app.github import _UNSET
        with patch.dict("os.environ", {}, clear=True):
            result = _config_target_repo("/proj", "test")
            assert result is _UNSET

    @patch("app.projects_config.load_projects_config", return_value=None)
    def test_no_config_returns_unset(self, mock_cfg):
        from app.github import _UNSET
        with patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"}):
            result = _config_target_repo("/proj", "test")
            assert result is _UNSET

    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    @patch("app.projects_config.get_project_submit_to_repository", return_value={})
    def test_no_submit_repo_returns_unset(self, mock_submit, mock_cfg):
        from app.github import _UNSET
        with patch.dict("os.environ", {"KOAN_ROOT": "/tmp/koan"}):
            result = _config_target_repo("/proj", "test")
            assert result is _UNSET


class TestUpstreamRemoteRepo:

    @patch("app.github._get_remote_url")
    def test_returns_upstream_when_different_from_origin(self, mock_url):
        mock_url.side_effect = lambda path, remote: {
            "upstream": "git@github.com:upstream-org/my-toolkit.git",
            "origin": "https://github.com/fork-owner/my-toolkit.git",
        }.get(remote)
        assert _upstream_remote_repo("/proj") == "upstream-org/my-toolkit"

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


class TestOriginRepo:

    @patch("app.github._get_remote_url",
            return_value="https://github.com/fork-owner/my-toolkit.git")
    def test_returns_origin_slug(self, mock_url):
        assert origin_repo("/proj") == "fork-owner/my-toolkit"

    @patch("app.github._get_remote_url", return_value=None)
    def test_returns_none_when_no_origin(self, mock_url):
        assert origin_repo("/proj") is None

    @patch("app.github._get_remote_url", return_value="git@gitlab.com:o/r.git")
    def test_returns_none_for_non_github(self, mock_url):
        assert origin_repo("/proj") is None


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


# ---------------------------------------------------------------------------
# sanitize_github_comment
# ---------------------------------------------------------------------------

class TestSanitizeGithubComment:
    def test_bare_lowercase(self):
        assert sanitize_github_comment("Hi @copilot please review") == "Hi `@copilot` please review"

    def test_bare_capitalized(self):
        assert sanitize_github_comment("Hi @Copilot please review") == "Hi `@Copilot` please review"

    def test_bare_uppercase(self):
        assert sanitize_github_comment("@COPILOT look at this") == "`@COPILOT` look at this"

    def test_already_escaped_not_double_escaped(self):
        assert sanitize_github_comment("`@copilot` is already escaped") == "`@copilot` is already escaped"

    def test_no_partial_match(self):
        assert sanitize_github_comment("@copilotx is not copilot") == "@copilotx is not copilot"

    def test_no_mention(self):
        assert sanitize_github_comment("no mention here") == "no mention here"

    def test_empty_string(self):
        assert sanitize_github_comment("") == ""

    def test_none_passthrough(self):
        assert sanitize_github_comment(None) is None

    def test_multiple_occurrences(self):
        result = sanitize_github_comment("@copilot and @Copilot and @COPILOT")
        assert result == "`@copilot` and `@Copilot` and `@COPILOT`"

    def test_in_quote_header(self):
        text = "> @copilot: can you fix this?\n\nSure, here's how."
        result = sanitize_github_comment(text)
        assert "`@copilot`" in result
        assert "@copilot:" not in result.split("`@copilot`")[0]

    def test_bare_dependabot(self):
        assert sanitize_github_comment("Thanks @dependabot") == "Thanks `@dependabot`"

    def test_bare_dependabot_capitalized(self):
        assert sanitize_github_comment("@Dependabot updated") == "`@Dependabot` updated"

    def test_dependabot_already_escaped(self):
        assert sanitize_github_comment("`@dependabot` is fine") == "`@dependabot` is fine"

    def test_dependabot_no_partial_match(self):
        assert sanitize_github_comment("@dependabot-preview is different") == "@dependabot-preview is different"

    def test_bare_github_actions(self):
        assert sanitize_github_comment("See @github-actions run") == "See `@github-actions` run"

    def test_github_actions_capitalized(self):
        assert sanitize_github_comment("@GitHub-Actions failed") == "`@GitHub-Actions` failed"

    def test_github_actions_already_escaped(self):
        assert sanitize_github_comment("`@github-actions` ok") == "`@github-actions` ok"

    def test_mixed_bots(self):
        text = "@copilot and @dependabot and @github-actions"
        result = sanitize_github_comment(text)
        assert result == "`@copilot` and `@dependabot` and `@github-actions`"


# ---------------------------------------------------------------------------
# find_bot_comment
# ---------------------------------------------------------------------------

class TestFindBotComment:
    """Tests for find_bot_comment — searches PR issue comments for a marker."""

    MARKER = "<!-- koan-summary -->"

    def _make_comment(self, comment_id, body, user="koan-bot"):
        return json.dumps({"id": comment_id, "body": body, "user": user})

    @patch("app.github.run_gh")
    def test_returns_comment_when_marker_found(self, mock_gh):
        raw = self._make_comment(101, f"{self.MARKER}\n## Review\n\nLooks good.")
        mock_gh.return_value = raw
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result is not None
        assert result["id"] == 101
        assert self.MARKER in result["body"]

    @patch("app.github.run_gh")
    def test_returns_none_when_marker_absent(self, mock_gh):
        raw = self._make_comment(101, "This comment has no koan marker.")
        mock_gh.return_value = raw
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result is None

    @patch("app.github.run_gh")
    def test_returns_first_match_when_multiple_comments_match(self, mock_gh):
        """When multiple comments have the marker, first one wins."""
        line1 = self._make_comment(101, f"{self.MARKER} first match")
        line2 = self._make_comment(202, f"{self.MARKER} second match")
        mock_gh.return_value = f"{line1}\n{line2}"
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result["id"] == 101

    @patch("app.github.run_gh")
    def test_returns_none_on_empty_response(self, mock_gh):
        mock_gh.return_value = ""
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result is None

    @patch("app.github.run_gh", side_effect=RuntimeError("API error"))
    def test_returns_none_on_api_error(self, mock_gh):
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result is None

    @patch("app.github.run_gh")
    def test_calls_correct_endpoint(self, mock_gh):
        mock_gh.return_value = ""
        find_bot_comment("myowner", "myrepo", 99, self.MARKER)
        mock_gh.assert_called_once_with(
            "api",
            "repos/myowner/myrepo/issues/99/comments",
            "--paginate",
            "--jq", r'.[] | {id: .id, body: .body, user: .user.login}',
            timeout=30,
        )

    @patch("app.github.run_gh")
    def test_skips_malformed_json_lines(self, mock_gh):
        """Malformed JSON lines are skipped; valid ones are still searched."""
        good = self._make_comment(55, "no marker here")
        marked = self._make_comment(66, f"{self.MARKER} found it")
        mock_gh.return_value = f"{{not valid json}}\n{good}\n{marked}"
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result["id"] == 66

    @patch("app.github.run_gh")
    def test_bot_username_skips_other_bots_comment(self, mock_gh):
        """When bot_username is given, a marked comment by a different account
        is not returned.

        Reproduces the bot-switch bug: a first bot (other-bot) posts the
        review summary, then a second bot (koan-bot) runs the review. Matching
        by marker alone would return other-bot's comment, and koan-bot would
        then PATCH it — which GitHub rejects with a 403. The current bot must
        only treat its OWN comment as the existing one.
        """
        raw = self._make_comment(
            101, f"{self.MARKER}\n## Review", user="other-bot",
        )
        mock_gh.return_value = raw
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER, bot_username="koan-bot",
        )
        assert result is None

    @patch("app.github.run_gh")
    def test_bot_username_returns_own_comment(self, mock_gh):
        """When bot_username matches the comment author, it is returned."""
        raw = self._make_comment(
            101, f"{self.MARKER}\n## Review", user="koan-bot",
        )
        mock_gh.return_value = raw
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER, bot_username="koan-bot",
        )
        assert result is not None
        assert result["id"] == 101

    @patch("app.github.run_gh")
    def test_bot_username_match_is_case_insensitive(self, mock_gh):
        """Author matching ignores case (GitHub logins are case-insensitive)."""
        raw = self._make_comment(
            101, f"{self.MARKER}\n## Review", user="Koan-Bot",
        )
        mock_gh.return_value = raw
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER, bot_username="koan-bot",
        )
        assert result is not None
        assert result["id"] == 101

    @patch("app.github.run_gh")
    def test_bot_username_picks_own_over_other_bot(self, mock_gh):
        """With multiple marked comments, returns the one by the current bot,
        not merely the first one."""
        other = self._make_comment(
            101, f"{self.MARKER} by other", user="other-bot",
        )
        mine = self._make_comment(
            202, f"{self.MARKER} by me", user="koan-bot",
        )
        mock_gh.return_value = f"{other}\n{mine}"
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER, bot_username="koan-bot",
        )
        assert result["id"] == 202

    @patch("app.github.run_gh")
    def test_no_bot_username_keeps_first_match_behavior(self, mock_gh):
        """Without bot_username (unconfigured), falls back to first marker match
        regardless of author — preserves backward compatibility."""
        other = self._make_comment(
            101, f"{self.MARKER} by other", user="other-bot",
        )
        mock_gh.return_value = other
        result = find_bot_comment("owner", "repo", 42, self.MARKER)
        assert result is not None
        assert result["id"] == 101

    @patch("app.github.run_gh")
    def test_prefer_newest_returns_highest_id_match(self, mock_gh):
        """prefer_newest returns the highest-id match, not the first.

        Reproduces the preserve_previous case: two same-bot comments carry the
        marker at once (the preserved prior review + the freshly-posted one).
        The newest — highest comment id — is the current review."""
        old = self._make_comment(101, f"{self.MARKER} preserved prior review")
        new = self._make_comment(303, f"{self.MARKER} fresh review")
        mock_gh.return_value = f"{old}\n{new}"
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER, prefer_newest=True,
        )
        assert result["id"] == 303

    @patch("app.github.run_gh")
    def test_prefer_newest_ignores_input_ordering(self, mock_gh):
        """The highest id wins even when it appears first in the response."""
        new = self._make_comment(303, f"{self.MARKER} fresh review")
        old = self._make_comment(101, f"{self.MARKER} preserved prior review")
        mock_gh.return_value = f"{new}\n{old}"
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER, prefer_newest=True,
        )
        assert result["id"] == 303

    @patch("app.github.run_gh")
    def test_prefer_newest_respects_bot_username_filter(self, mock_gh):
        """prefer_newest still honours the author filter: a newer comment by a
        different bot is not selected over the current bot's own comment."""
        mine = self._make_comment(
            202, f"{self.MARKER} by me", user="koan-bot",
        )
        other_newer = self._make_comment(
            303, f"{self.MARKER} by other", user="other-bot",
        )
        mock_gh.return_value = f"{mine}\n{other_newer}"
        result = find_bot_comment(
            "owner", "repo", 42, self.MARKER,
            bot_username="koan-bot", prefer_newest=True,
        )
        assert result["id"] == 202


class TestIssueEdit:
    def test_passes_repo_flag_when_provided(self):
        with patch("app.github.run_gh") as mock_gh, \
             patch("app.leak_detector.scan_and_redact", side_effect=lambda b, **kw: b):
            from app.github import issue_edit

            issue_edit(42, "new body", cwd="/fake", repo="upstream/app")
            args = list(mock_gh.call_args.args)
            assert "--repo" in args
            assert args[args.index("--repo") + 1] == "upstream/app"

    def test_omits_repo_flag_when_none(self):
        with patch("app.github.run_gh") as mock_gh, \
             patch("app.leak_detector.scan_and_redact", side_effect=lambda b, **kw: b):
            from app.github import issue_edit

            issue_edit(42, "new body", cwd="/fake")
            args = list(mock_gh.call_args.args)
            assert "--repo" not in args


# ---------------------------------------------------------------------------
# api — raw_body branch
# ---------------------------------------------------------------------------
class TestApiRawBody:
    @patch("app.github.subprocess.run")
    def test_raw_body_uses_input_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        api("repos/o/r/x", method="POST", input_data='{"a": 1}', raw_body=True)
        cmd = mock_run.call_args[0][0]
        assert "--input" in cmd
        assert cmd[cmd.index("--input") + 1] == "-"
        assert "-F" not in cmd
        assert mock_run.call_args.kwargs.get("input") == '{"a": 1}'


# ---------------------------------------------------------------------------
# list_open_issues / list_open_audit_issues
# ---------------------------------------------------------------------------
class TestListOpenIssues:
    @patch("app.github.run_gh")
    def test_returns_parsed_issues(self, mock_gh):
        from app.github import list_open_issues

        mock_gh.return_value = json.dumps([
            {"number": 1, "title": "A", "url": "u1", "body": "first"},
            {"number": 2, "title": "B", "url": "u2", "body": "second"},
        ])
        result = list_open_issues(repo="o/r")
        assert len(result) == 2
        assert result[0] == {"number": 1, "title": "A", "url": "u1", "body": "first"}

    @patch("app.github.run_gh")
    def test_repo_flag_added(self, mock_gh):
        from app.github import list_open_issues

        mock_gh.return_value = "[]"
        list_open_issues(repo="o/r")
        args = list(mock_gh.call_args.args)
        assert "--repo" in args
        assert args[args.index("--repo") + 1] == "o/r"

    @patch("app.github.run_gh")
    def test_no_repo_omits_flag(self, mock_gh):
        from app.github import list_open_issues

        mock_gh.return_value = "[]"
        list_open_issues()
        assert "--repo" not in list(mock_gh.call_args.args)

    @patch("app.github.run_gh")
    def test_body_contains_filters(self, mock_gh):
        from app.github import list_open_issues

        mock_gh.return_value = json.dumps([
            {"number": 1, "title": "A", "url": "u1", "body": "has MARKER here"},
            {"number": 2, "title": "B", "url": "u2", "body": "no match"},
        ])
        result = list_open_issues(body_contains="MARKER")
        assert len(result) == 1
        assert result[0]["number"] == 1

    @patch("app.github.run_gh")
    def test_limit_passed(self, mock_gh):
        from app.github import list_open_issues

        mock_gh.return_value = "[]"
        list_open_issues(limit=50)
        args = list(mock_gh.call_args.args)
        assert args[args.index("--limit") + 1] == "50"

    @patch("app.github.run_gh", side_effect=RuntimeError("boom"))
    def test_returns_empty_on_error(self, mock_gh):
        from app.github import list_open_issues

        assert list_open_issues(repo="o/r") == []

    @patch("app.github.run_gh", side_effect=RuntimeError("timeout reached"))
    def test_logs_warning_on_error(self, mock_gh, caplog):
        import logging as _logging

        from app.github import list_open_issues

        with caplog.at_level(_logging.WARNING, logger="app.github"):
            assert list_open_issues(repo="o/r") == []
        assert "list_open_issues failed" in caplog.text
        assert "timeout reached" in caplog.text

    @patch("app.github.run_gh", return_value="")
    def test_returns_empty_on_blank_output(self, mock_gh):
        from app.github import list_open_issues

        assert list_open_issues() == []

    @patch("app.github.run_gh", return_value="not json")
    def test_returns_empty_on_bad_json(self, mock_gh):
        from app.github import list_open_issues

        assert list_open_issues() == []

    @patch("app.github.run_gh", return_value='{"not": "a list"}')
    def test_returns_empty_when_not_a_list(self, mock_gh):
        from app.github import list_open_issues

        assert list_open_issues() == []

    @patch("app.github.run_gh")
    def test_skips_non_dict_items_and_defaults_fields(self, mock_gh):
        from app.github import list_open_issues

        mock_gh.return_value = json.dumps([
            "not a dict",
            {"number": 3},  # missing title/url/body
        ])
        result = list_open_issues()
        assert result == [{"number": 3, "title": "", "url": "", "body": ""}]


class TestListOpenAuditIssues:
    @patch("app.github.list_open_issues")
    def test_filters_by_audit_marker(self, mock_list):
        from app.github import list_open_audit_issues, AUDIT_ISSUE_MARKER

        mock_list.return_value = [{"number": 1}]
        result = list_open_audit_issues(repo="o/r", limit=10)
        assert result == [{"number": 1}]
        assert mock_list.call_args.kwargs["body_contains"] == AUDIT_ISSUE_MARKER
        assert mock_list.call_args.kwargs["limit"] == 10


# ---------------------------------------------------------------------------
# _get_remote_url
# ---------------------------------------------------------------------------
class TestGetRemoteUrl:
    @patch("app.github.subprocess.run")
    def test_returns_url_on_success(self, mock_run):
        from app.github import _get_remote_url

        mock_run.return_value = MagicMock(
            returncode=0, stdout="git@github.com:o/r.git\n")
        assert _get_remote_url("/p", "origin") == "git@github.com:o/r.git"

    @patch("app.github.subprocess.run")
    def test_returns_none_on_nonzero(self, mock_run):
        from app.github import _get_remote_url

        mock_run.return_value = MagicMock(returncode=2, stdout="")
        assert _get_remote_url("/p", "upstream") is None

    @patch("app.github.subprocess.run", side_effect=FileNotFoundError())
    def test_returns_none_when_git_missing(self, mock_run):
        from app.github import _get_remote_url

        assert _get_remote_url("/p", "origin") is None

    @patch("app.github.subprocess.run",
           side_effect=subprocess.TimeoutExpired("git", 5))
    def test_returns_none_on_timeout(self, mock_run):
        from app.github import _get_remote_url

        assert _get_remote_url("/p", "origin") is None


class TestUpstreamRemoteRepoUnparseable:
    @patch("app.github._get_remote_url", return_value="not-a-github-url")
    def test_returns_none_when_upstream_url_unparseable(self, mock_url):
        assert _upstream_remote_repo("/p") is None


# ---------------------------------------------------------------------------
# _config_target_repo — exception path
# ---------------------------------------------------------------------------
class TestConfigTargetRepoException:
    @patch("app.projects_config.load_projects_config",
           side_effect=RuntimeError("config blew up"))
    def test_returns_unset_on_exception(self, mock_load, monkeypatch):
        from app.github import _config_target_repo, _UNSET

        monkeypatch.setenv("KOAN_ROOT", "/koan")
        assert _config_target_repo("/p", "proj") is _UNSET


# ---------------------------------------------------------------------------
# list_open_pr_branches
# ---------------------------------------------------------------------------
class TestListOpenPrBranches:
    @patch("app.github.run_gh")
    def test_returns_sorted_unique_branches(self, mock_gh):
        from app.github import list_open_pr_branches

        mock_gh.return_value = json.dumps([
            {"headRefName": "feat/b"},
            {"headRefName": "feat/a"},
            {"headRefName": "feat/a"},
        ])
        assert list_open_pr_branches("o/r", "bot") == ["feat/a", "feat/b"]

    def test_empty_author_returns_empty(self):
        from app.github import list_open_pr_branches

        assert list_open_pr_branches("o/r", "") == []

    @patch("app.github.run_gh", return_value="")
    def test_empty_output_returns_empty(self, mock_gh):
        from app.github import list_open_pr_branches

        assert list_open_pr_branches("o/r", "bot") == []

    @patch("app.github.run_gh", return_value='{"not": "list"}')
    def test_non_list_returns_empty(self, mock_gh):
        from app.github import list_open_pr_branches

        assert list_open_pr_branches("o/r", "bot") == []

    @patch("app.github.run_gh")
    def test_skips_entries_without_headrefname(self, mock_gh):
        from app.github import list_open_pr_branches

        mock_gh.return_value = json.dumps([
            {"headRefName": "feat/a"},
            {"other": "x"},
            "not a dict",
        ])
        assert list_open_pr_branches("o/r", "bot") == ["feat/a"]

    @patch("app.github.run_gh", side_effect=RuntimeError("boom"))
    def test_error_returns_empty(self, mock_gh):
        from app.github import list_open_pr_branches

        assert list_open_pr_branches("o/r", "bot") == []


# ---------------------------------------------------------------------------
# check_pvrs_enabled
# ---------------------------------------------------------------------------
class TestCheckPvrsEnabled:
    @patch("app.github.api", return_value='{"enabled": true}')
    def test_true_when_enabled(self, mock_api):
        from app.github import check_pvrs_enabled

        assert check_pvrs_enabled("o/r") is True

    @patch("app.github.api", return_value='{"enabled": false}')
    def test_false_when_disabled(self, mock_api):
        from app.github import check_pvrs_enabled

        assert check_pvrs_enabled("o/r") is False

    @patch("app.github.api", return_value='{}')
    def test_false_when_field_absent(self, mock_api):
        from app.github import check_pvrs_enabled

        assert check_pvrs_enabled("o/r") is False

    @patch("app.github.api", side_effect=RuntimeError("404"))
    def test_false_on_error(self, mock_api):
        from app.github import check_pvrs_enabled

        assert check_pvrs_enabled("o/r") is False

    @patch("app.github.api", return_value="not json")
    def test_false_on_bad_json(self, mock_api):
        from app.github import check_pvrs_enabled

        assert check_pvrs_enabled("o/r") is False


# ---------------------------------------------------------------------------
# security_advisory_report
# ---------------------------------------------------------------------------
class TestSecurityAdvisoryReport:
    def test_returns_html_url(self):
        with patch("app.github.api",
                   return_value='{"html_url": "https://x/advisory/1"}') as mock_api, \
             patch("app.leak_detector.scan_and_redact",
                   side_effect=lambda b, **kw: b):
            from app.github import security_advisory_report

            url = security_advisory_report(
                "Sum", "Desc", "high", repo="o/r", package_name="pkg")
            assert url == "https://x/advisory/1"
            # Payload sent as raw body via POST
            assert mock_api.call_args.kwargs["method"] == "POST"
            assert mock_api.call_args.kwargs["raw_body"] is True
            payload = json.loads(mock_api.call_args.kwargs["input_data"])
            assert payload["severity"] == "high"
            assert payload["vulnerabilities"][0]["package"]["name"] == "pkg"

    def test_falls_back_to_ghsa_id(self):
        with patch("app.github.api", return_value='{"ghsa_id": "GHSA-xxxx"}'), \
             patch("app.leak_detector.scan_and_redact",
                   side_effect=lambda b, **kw: b):
            from app.github import security_advisory_report

            assert security_advisory_report(
                "S", "D", "low", repo="o/r") == "GHSA: GHSA-xxxx"

    def test_returns_raw_output_on_bad_json(self):
        with patch("app.github.api", return_value="weird output"), \
             patch("app.leak_detector.scan_and_redact",
                   side_effect=lambda b, **kw: b):
            from app.github import security_advisory_report

            assert security_advisory_report(
                "S", "D", "low", repo="o/r") == "weird output"

    def test_empty_when_api_returns_empty(self):
        with patch("app.github.api", return_value=""), \
             patch("app.leak_detector.scan_and_redact",
                   side_effect=lambda b, **kw: b):
            from app.github import security_advisory_report

            assert security_advisory_report("S", "D", "low", repo="o/r") == ""

    def test_default_package_name_is_unknown(self):
        with patch("app.github.api", return_value='{"html_url": "u"}') as mock_api, \
             patch("app.leak_detector.scan_and_redact",
                   side_effect=lambda b, **kw: b):
            from app.github import security_advisory_report

            security_advisory_report("S", "D", "critical", repo="o/r")
            payload = json.loads(mock_api.call_args.kwargs["input_data"])
            assert payload["vulnerabilities"][0]["package"]["name"] == "unknown"


# ---------------------------------------------------------------------------
# detect_ecosystem
# ---------------------------------------------------------------------------
class TestDetectEcosystem:
    def _make(self, tmp_path, *files):
        for f in files:
            (tmp_path / f).write_text("x")
        return str(tmp_path)

    def test_pip_from_pyproject(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(self._make(tmp_path, "pyproject.toml")) == "pip"

    def test_npm_from_package_json(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(self._make(tmp_path, "package.json")) == "npm"

    def test_go_from_go_mod(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(self._make(tmp_path, "go.mod")) == "go"

    def test_cargo(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(self._make(tmp_path, "Cargo.toml")) == "cargo"

    def test_rubygems(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(self._make(tmp_path, "Gemfile")) == "rubygems"

    def test_nuget_glob_match(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(self._make(tmp_path, "app.csproj")) == "nuget"

    def test_pip_wins_over_npm_when_both_present(self, tmp_path):
        from app.github import detect_ecosystem

        path = self._make(tmp_path, "pyproject.toml", "package.json")
        assert detect_ecosystem(path) == "pip"

    def test_other_when_no_indicator(self, tmp_path):
        from app.github import detect_ecosystem

        assert detect_ecosystem(str(tmp_path)) == "other"


# ---------------------------------------------------------------------------
# Mission status indicators (koan/mission)
# ---------------------------------------------------------------------------


class TestSetCommitStatus:
    def test_posts_raw_json(self, monkeypatch):
        import app.github as github

        calls = {}

        def fake_api(endpoint, method="GET", input_data=None,
                     raw_body=False, cwd=None, **kw):
            calls.update(endpoint=endpoint, method=method,
                         body=json.loads(input_data), raw_body=raw_body)
            return "{}"

        monkeypatch.setattr(github, "api", fake_api)
        github.set_commit_status("o/r", "abc123", "pending",
                                 description="Kōan is working on this mission")
        assert calls["endpoint"] == "repos/o/r/statuses/abc123"
        assert calls["method"] == "POST"
        assert calls["raw_body"] is True
        assert calls["body"]["state"] == "pending"
        assert calls["body"]["context"] == "koan/mission"

    def test_description_truncated_to_140(self, monkeypatch):
        import app.github as github

        captured = {}
        monkeypatch.setattr(
            github, "api",
            lambda endpoint, **kw: captured.update(
                body=json.loads(kw["input_data"])) or "{}",
        )
        github.set_commit_status("o/r", "sha", "success", description="x" * 300)
        assert len(captured["body"]["description"]) == 140

    def test_target_url_included(self, monkeypatch):
        import app.github as github

        captured = {}
        monkeypatch.setattr(
            github, "api",
            lambda endpoint, **kw: captured.update(
                body=json.loads(kw["input_data"])) or "{}",
        )
        github.set_commit_status("o/r", "sha", "success",
                                 target_url="https://pr/1")
        assert captured["body"]["target_url"] == "https://pr/1"


class TestIssueLabelToggle:
    def test_add_and_remove_label(self, monkeypatch):
        import app.github as github

        seen = []
        monkeypatch.setattr(github, "run_gh",
                            lambda *a, **k: seen.append(a) or "")
        github.add_issue_label("o/r", 42, "koan:working")
        github.remove_issue_label("o/r", 42, "koan:working")
        assert seen[0] == ("issue", "edit", "42", "--repo", "o/r",
                           "--add-label", "koan:working")
        assert seen[1][5] == "--remove-label"

    def test_ensure_label_uses_force(self, monkeypatch):
        import app.github as github

        seen = []
        monkeypatch.setattr(github, "run_gh",
                            lambda *a, **k: seen.append(a) or "")
        github.ensure_label("o/r", "koan:working")
        assert seen[0][:5] == ("label", "create", "koan:working",
                               "--repo", "o/r")
        assert "--force" in seen[0]


# ---------------------------------------------------------------------------
# escape_issue_refs
# ---------------------------------------------------------------------------

class TestEscapeIssueRefs:
    """Bare ``#N`` refs must stop auto-linking to unrelated issues/PRs."""

    def test_escapes_bare_ref(self):
        assert escape_issue_refs("reviewer #5 asked for this") == (
            "reviewer \uFF035 asked for this"
        )

    def test_escapes_every_ref_in_a_line(self):
        result = escape_issue_refs("Points #5, #6 and #7 remain.")
        assert result == "Points \uFF035, \uFF036 and \uFF037 remain."
        assert "#" not in result

    def test_escapes_ref_in_parentheses(self):
        assert escape_issue_refs("(reviewer #12)") == "(reviewer \uFF0312)"

    def test_escapes_cross_repo_ref(self):
        assert escape_issue_refs("owner/repo#5") == "owner/repo\uFF035"

    def test_leaves_inline_code_span_alone(self):
        assert escape_issue_refs("see `#7` here") == "see `#7` here"

    def test_leaves_fenced_code_block_alone(self):
        text = "before #1\n```\nfix #2\n```\nafter #3"
        assert escape_issue_refs(text) == "before \uFF031\n```\nfix #2\n```\nafter \uFF033"

    def test_leaves_tilde_fence_alone(self):
        assert escape_issue_refs("~~~\nfix #2\n~~~") == "~~~\nfix #2\n~~~"

    def test_leaves_url_fragment_alone(self):
        url = "https://github.com/o/r/pull/12#issuecomment-999"
        assert escape_issue_refs(f"see {url} now") == f"see {url} now"

    def test_leaves_autolink_alone(self):
        text = "<https://example.com/p#1> ok"
        assert escape_issue_refs(text) == text

    def test_leaves_markdown_headings_alone(self):
        assert escape_issue_refs("## Stats\n### CI status") == "## Stats\n### CI status"

    def test_leaves_hex_color_alone(self):
        assert escape_issue_refs("color #1f2937 stays") == "color #1f2937 stays"

    def test_leaves_html_entity_alone(self):
        assert escape_issue_refs("it&#39;s fine") == "it&#39;s fine"

    def test_no_refs_is_unchanged(self):
        assert escape_issue_refs("nothing to escape") == "nothing to escape"

    def test_empty_string(self):
        assert escape_issue_refs("") == ""

    def test_none(self):
        assert escape_issue_refs(None) is None
