"""Tests for issue_tracker.py — unified issue tracker client."""

import json
import subprocess
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from app.issue_tracker import (
    fetch_github_issues,
    fetch_issue_context,
    fetch_jira_issues,
    parse_github_issue_refs,
    parse_jira_ticket_ids,
)


# ---------------------------------------------------------------------------
# parse_jira_ticket_ids
# ---------------------------------------------------------------------------

class TestParseJiraTicketIds:
    def test_single_ticket(self):
        assert parse_jira_ticket_ids("Fixes PROJ-42") == ["PROJ-42"]

    def test_multiple_tickets(self):
        result = parse_jira_ticket_ids("Fixes PROJ-42 and ABC-7")
        assert result == ["PROJ-42", "ABC-7"]

    def test_empty_string(self):
        assert parse_jira_ticket_ids("") == []

    def test_none_returns_empty(self):
        assert parse_jira_ticket_ids(None) == []

    def test_deduplicates(self):
        result = parse_jira_ticket_ids("PROJ-1 fixes PROJ-1 again")
        assert result == ["PROJ-1"]

    def test_preserves_order(self):
        result = parse_jira_ticket_ids("See ABC-1 and XYZ-99 for context")
        assert result == ["ABC-1", "XYZ-99"]

    def test_requires_two_uppercase_letters(self):
        # Single-letter prefix should not match
        result = parse_jira_ticket_ids("A-123 should not match")
        assert result == []

    def test_max_ten_uppercase_letters(self):
        # 11 uppercase letters should not match
        result = parse_jira_ticket_ids("ABCDEFGHIJK-1 should not match")
        assert result == []

    def test_exactly_ten_uppercase_letters(self):
        result = parse_jira_ticket_ids("ABCDEFGHIJ-1 matches")
        assert result == ["ABCDEFGHIJ-1"]

    def test_no_tickets_in_plain_text(self):
        assert parse_jira_ticket_ids("No ticket IDs here") == []

    def test_ticket_in_pr_body(self):
        body = "This PR implements FEAT-100.\n\nSee also INFRA-55."
        result = parse_jira_ticket_ids(body)
        assert result == ["FEAT-100", "INFRA-55"]


# ---------------------------------------------------------------------------
# parse_github_issue_refs
# ---------------------------------------------------------------------------

class TestParseGithubIssueRefs:
    def test_single_ref(self):
        result = parse_github_issue_refs("see myorg/myrepo#99")
        assert result == [("myorg", "myrepo", 99)]

    def test_multiple_refs(self):
        result = parse_github_issue_refs("fixes myorg/repo#1, see other/proj#200")
        assert result == [("myorg", "repo", 1), ("other", "proj", 200)]

    def test_empty_string(self):
        assert parse_github_issue_refs("") == []

    def test_none_returns_empty(self):
        assert parse_github_issue_refs(None) == []

    def test_in_repo_ref_excluded(self):
        """In-repo #123 refs are not parsed — only owner/repo#N cross-repo refs."""
        result = parse_github_issue_refs("Fixes #42 in this repo")
        assert result == []

    def test_deduplicates(self):
        result = parse_github_issue_refs("myorg/repo#1 and myorg/repo#1 again")
        assert result == [("myorg", "repo", 1)]

    def test_preserves_order(self):
        result = parse_github_issue_refs("org/a#1 then org/b#2")
        assert result == [("org", "a", 1), ("org", "b", 2)]

    def test_dedup_is_case_insensitive(self):
        """Duplicate detection ignores owner/repo casing."""
        result = parse_github_issue_refs("MyOrg/MyRepo#1 and myorg/myrepo#1")
        assert len(result) == 1

    def test_returns_int_for_number(self):
        refs = parse_github_issue_refs("org/repo#42")
        assert refs[0][2] == 42
        assert isinstance(refs[0][2], int)


# ---------------------------------------------------------------------------
# fetch_jira_issues
# ---------------------------------------------------------------------------

class TestFetchJiraIssues:
    """Tests for fetch_jira_issues() with mocked urllib.request."""

    JIRA_CONFIG = {
        "type": "jira",
        "base_url": "https://my-org.atlassian.net",
        "email": "bot@my-org.com",
        "api_token": "secret-token",
    }

    def _mock_response(self, data: dict, status: int = 200):
        """Build a mock urllib response object."""
        body = json.dumps(data).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = body
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    def test_returns_formatted_block(self):
        issue_data = {
            "fields": {
                "summary": "Fix login timeout",
                "description": "Users reported login timeouts after 5 minutes.",
            }
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_response(issue_data)
            result = fetch_jira_issues(["PROJ-42"], self.JIRA_CONFIG)
        assert "## Issue Tracker Context" in result
        assert "PROJ-42" in result
        assert "Fix login timeout" in result

    def test_returns_empty_on_404(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
            )
            result = fetch_jira_issues(["PROJ-99"], self.JIRA_CONFIG)
        assert result == ""

    def test_returns_empty_on_403(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="http://x", code=403, msg="Forbidden", hdrs=None, fp=None
            )
            result = fetch_jira_issues(["PROJ-99"], self.JIRA_CONFIG)
        assert result == ""

    def test_returns_empty_on_timeout(self):
        with patch("urllib.request.urlopen") as mock_open:
            import socket
            mock_open.side_effect = socket.timeout("timed out")
            result = fetch_jira_issues(["PROJ-42"], self.JIRA_CONFIG)
        assert result == ""

    def test_returns_empty_when_no_ticket_ids(self):
        result = fetch_jira_issues([], self.JIRA_CONFIG)
        assert result == ""

    def test_description_truncated_at_500_chars(self):
        long_desc = "x" * 600
        issue_data = {
            "fields": {
                "summary": "A summary",
                "description": long_desc,
            }
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_response(issue_data)
            result = fetch_jira_issues(["PROJ-1"], self.JIRA_CONFIG)
        assert "..." in result
        # The full 600-char description should not appear
        assert "x" * 501 not in result

    def test_multiple_tickets_fetched(self):
        issue_data = {
            "fields": {
                "summary": "Short summary",
                "description": "Short desc.",
            }
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_response(issue_data)
            result = fetch_jira_issues(["PROJ-1", "PROJ-2"], self.JIRA_CONFIG)
        assert "PROJ-1" in result
        assert "PROJ-2" in result

    def test_total_output_capped_at_1000_chars(self):
        issue_data = {
            "fields": {
                "summary": "A" * 200,
                "description": "B" * 450,
            }
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_response(issue_data)
            result = fetch_jira_issues(
                [f"PROJ-{i}" for i in range(20)], self.JIRA_CONFIG
            )
        assert len(result) <= 1000

    def test_missing_config_fields_returns_empty(self):
        """Returns empty when config is missing required JIRA fields."""
        result = fetch_jira_issues(["PROJ-1"], {"type": "jira"})
        assert result == ""

    def test_adf_description_extracted(self):
        """Handles JIRA ADF-format description (dict instead of string)."""
        issue_data = {
            "fields": {
                "summary": "ADF desc test",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Hello from ADF"}
                            ],
                        }
                    ],
                },
            }
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._mock_response(issue_data)
            result = fetch_jira_issues(["PROJ-1"], self.JIRA_CONFIG)
        assert "Hello from ADF" in result


# ---------------------------------------------------------------------------
# fetch_github_issues
# ---------------------------------------------------------------------------

class TestFetchGithubIssues:
    """Tests for fetch_github_issues() with mocked subprocess.run."""

    GITHUB_CONFIG = {"type": "github"}

    def _make_run_result(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        return result

    def test_returns_formatted_block(self):
        payload = json.dumps({"title": "Fix login timeout", "body": "Description here."})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_run_result(payload)
            result = fetch_github_issues([("myorg", "myrepo", 99)], self.GITHUB_CONFIG)
        assert "## Issue Tracker Context" in result
        assert "myorg/myrepo#99" in result
        assert "Fix login timeout" in result

    def test_returns_empty_when_no_refs(self):
        result = fetch_github_issues([], self.GITHUB_CONFIG)
        assert result == ""

    def test_returns_empty_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_run_result("", returncode=1)
            result = fetch_github_issues([("org", "repo", 1)], self.GITHUB_CONFIG)
        assert result == ""

    def test_returns_empty_when_gh_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = fetch_github_issues([("org", "repo", 1)], self.GITHUB_CONFIG)
        assert result == ""

    def test_returns_empty_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)):
            result = fetch_github_issues([("org", "repo", 1)], self.GITHUB_CONFIG)
        assert result == ""

    def test_returns_empty_on_invalid_json(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_run_result("not-json")
            result = fetch_github_issues([("org", "repo", 1)], self.GITHUB_CONFIG)
        assert result == ""

    def test_body_truncated_at_500_chars(self):
        payload = json.dumps({"title": "Long body", "body": "x" * 600})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_run_result(payload)
            result = fetch_github_issues([("org", "repo", 1)], self.GITHUB_CONFIG)
        assert "..." in result
        assert "x" * 501 not in result

    def test_total_output_capped_at_1000_chars(self):
        payload = json.dumps({"title": "A" * 200, "body": "B" * 450})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_run_result(payload)
            refs = [(f"org{i}", "repo", i) for i in range(20)]
            result = fetch_github_issues(refs, self.GITHUB_CONFIG)
        assert len(result) <= 1000


# ---------------------------------------------------------------------------
# fetch_issue_context (dispatcher)
# ---------------------------------------------------------------------------

class TestFetchIssueContext:
    """Tests for the top-level fetch_issue_context() dispatcher."""

    def test_dispatches_to_jira(self):
        config = {
            "type": "jira",
            "base_url": "https://x.atlassian.net",
            "email": "bot@x.com",
            "api_token": "token",
        }
        with patch("app.issue_tracker.fetch_jira_issues", return_value="JIRA block") as mock_jira:
            result = fetch_issue_context("Fixes PROJ-1", config)
        mock_jira.assert_called_once()
        assert result == "JIRA block"

    def test_dispatches_to_github(self):
        config = {"type": "github"}
        with patch("app.issue_tracker.fetch_github_issues", return_value="GH block") as mock_gh:
            result = fetch_issue_context("fixes org/repo#1", config)
        mock_gh.assert_called_once()
        assert result == "GH block"

    def test_returns_empty_for_unknown_type(self):
        result = fetch_issue_context("some text", {"type": "gitlab"})
        assert result == ""

    def test_returns_empty_when_config_is_none(self):
        result = fetch_issue_context("some text", None)
        assert result == ""

    def test_returns_empty_when_no_jira_refs_found(self):
        config = {"type": "jira", "base_url": "x", "email": "e", "api_token": "t"}
        with patch("app.issue_tracker.fetch_jira_issues") as mock_jira:
            result = fetch_issue_context("no tickets here", config)
        mock_jira.assert_not_called()
        assert result == ""

    def test_returns_empty_when_no_github_refs_found(self):
        config = {"type": "github"}
        with patch("app.issue_tracker.fetch_github_issues") as mock_gh:
            result = fetch_issue_context("Fixes #42 (in-repo, not cross-repo)", config)
        mock_gh.assert_not_called()
        assert result == ""

    def test_empty_pr_body_returns_empty(self):
        config = {"type": "jira", "base_url": "x", "email": "e", "api_token": "t"}
        result = fetch_issue_context("", config)
        assert result == ""

    def test_none_pr_body_returns_empty(self):
        config = {"type": "jira", "base_url": "x", "email": "e", "api_token": "t"}
        result = fetch_issue_context(None, config)
        assert result == ""
