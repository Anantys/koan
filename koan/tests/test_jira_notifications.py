"""Tests for jira_notifications.py — Jira API client and mention parsing."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.jira_notifications import (
    JiraFetchResult,
    _adf_to_text,
    _extract_comment_text,
    _get_comment_age_hours,
    _load_processed_tracker,
    _save_processed_tracker,
    check_jira_already_processed,
    fetch_jira_mentions,
    mark_jira_comment_processed,
    parse_jira_mention_command,
    resolve_project_from_jira_key,
)


class TestAdfToText:
    def test_plain_text_node(self):
        node = {"type": "text", "text": "hello world"}
        assert _adf_to_text(node) == "hello world"

    def test_doc_with_paragraph(self):
        node = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "@koan-bot plan"}
                    ]
                }
            ]
        }
        assert "@koan-bot plan" in _adf_to_text(node)

    def test_skips_code_blocks(self):
        node = {
            "type": "doc",
            "content": [
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": "@koan-bot plan"}]
                }
            ]
        }
        assert "@koan-bot" not in _adf_to_text(node)

    def test_mention_node(self):
        node = {
            "type": "mention",
            "attrs": {"text": "@koan-bot", "id": "123"}
        }
        assert "@koan-bot" in _adf_to_text(node)

    def test_hard_break(self):
        node = {"type": "hardBreak"}
        assert _adf_to_text(node) == " "

    def test_empty_node(self):
        assert _adf_to_text({}) == ""
        assert _adf_to_text(None) == ""
        assert _adf_to_text([]) == ""

    def test_nested_structure(self):
        node = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Please "},
                        {"type": "mention", "attrs": {"text": "@koan-bot"}},
                        {"type": "text", "text": " plan"},
                    ]
                }
            ]
        }
        text = _adf_to_text(node)
        assert "@koan-bot" in text
        assert "plan" in text


class TestExtractCommentText:
    def test_string_passthrough(self):
        assert _extract_comment_text("hello @koan-bot plan") == "hello @koan-bot plan"

    def test_adf_dict(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "@koan-bot plan"}]
                }
            ]
        }
        result = _extract_comment_text(adf)
        assert "@koan-bot plan" in result

    def test_none_returns_empty(self):
        assert _extract_comment_text(None) == ""


class TestParseJiraMentionCommand:
    def test_basic_command(self):
        result = parse_jira_mention_command("@koan-bot plan", "koan-bot")
        assert result == ("plan", "")

    def test_command_with_context(self):
        result = parse_jira_mention_command("@koan-bot rebase please fix conflicts", "koan-bot")
        assert result is not None
        cmd, ctx = result
        assert cmd == "rebase"
        assert "please fix conflicts" in ctx

    def test_command_with_slash_prefix(self):
        result = parse_jira_mention_command("@koan-bot /plan", "koan-bot")
        assert result is not None
        assert result[0] == "plan"

    def test_case_insensitive_mention(self):
        result = parse_jira_mention_command("@KOAN-BOT plan", "koan-bot")
        assert result is not None
        assert result[0] == "plan"

    def test_no_mention_returns_none(self):
        assert parse_jira_mention_command("just a comment", "koan-bot") is None

    def test_empty_text(self):
        assert parse_jira_mention_command("", "koan-bot") is None

    def test_empty_nickname(self):
        assert parse_jira_mention_command("@koan-bot plan", "") is None

    def test_command_lowercased(self):
        result = parse_jira_mention_command("@koan-bot PLAN", "koan-bot")
        assert result is not None
        assert result[0] == "plan"

    def test_strips_jira_code_block(self):
        text = "{{@koan-bot plan}}\n@koan-bot rebase"
        result = parse_jira_mention_command(text, "koan-bot")
        assert result is not None
        assert result[0] == "rebase"

    @pytest.mark.parametrize("text,nick,expected", [
        # Jira renders multi-word display names with their literal space
        # in the ADF mention.attrs.text field.
        ("@My Bot plan", "My Bot", ("plan", "")),
        ("@My Bot plan FOO-123", "My Bot", ("plan", "FOO-123")),
        # Case-insensitive — clients render mentions inconsistently.
        ("@my bot plan", "My Bot", ("plan", "")),
    ])
    def test_spaced_nickname(self, text, nick, expected):
        """Nicknames containing spaces must match.

        Regression guard: re.escape() correctly handles the space; a future
        refactor that drops re.escape or uses plain f-string interpolation
        would silently break any nickname that contains a space.
        """
        assert parse_jira_mention_command(text, nick) == expected


class TestResolveProjectFromJiraKey:
    def test_basic_mapping(self):
        project_map = {"FOO": "myproject", "BAR": "another"}
        assert resolve_project_from_jira_key("FOO-123", project_map) == "myproject"

    def test_unknown_key_returns_none(self):
        project_map = {"FOO": "myproject"}
        assert resolve_project_from_jira_key("BAR-456", project_map) is None

    def test_case_insensitive_key(self):
        project_map = {"FOO": "myproject"}
        assert resolve_project_from_jira_key("foo-123", project_map) == "myproject"

    def test_invalid_key_no_dash(self):
        project_map = {"FOO": "myproject"}
        assert resolve_project_from_jira_key("FOOBAD", project_map) is None

    def test_empty_key(self):
        project_map = {"FOO": "myproject"}
        assert resolve_project_from_jira_key("", project_map) is None


class TestProcessedTracker:
    def test_load_nonexistent_file(self, tmp_path):
        tracker = tmp_path / ".jira-processed.json"
        result = _load_processed_tracker(tracker)
        assert result == set()

    def test_load_and_save_roundtrip(self, tmp_path):
        tracker = tmp_path / ".jira-processed.json"
        ids = {"comment-1", "comment-2", "comment-3"}
        _save_processed_tracker(tracker, ids)
        loaded = _load_processed_tracker(tracker)
        assert loaded == ids

    def test_load_invalid_json(self, tmp_path):
        tracker = tmp_path / ".jira-processed.json"
        tracker.write_text("not-json")
        result = _load_processed_tracker(tracker)
        assert result == set()

    def test_save_trims_to_5000(self, tmp_path):
        tracker = tmp_path / ".jira-processed.json"
        ids = {str(i) for i in range(6000)}
        _save_processed_tracker(tracker, ids)
        loaded = _load_processed_tracker(tracker)
        assert len(loaded) == 5000


class TestCheckAlreadyProcessed:
    def test_not_processed(self):
        assert check_jira_already_processed("new-id", set()) is False

    def test_in_persistent_set(self):
        assert check_jira_already_processed("known-id", {"known-id"}) is True

    def test_marks_in_memory_after_persistent_hit(self):
        processed_set = {"cached-id"}
        # First call hits persistent set
        assert check_jira_already_processed("cached-id", processed_set) is True
        # Second call hits in-memory set (bounded set)
        assert check_jira_already_processed("cached-id", set()) is True


class TestMarkJiraCommentProcessed:
    def test_adds_to_both_sets(self):
        processed_set = set()
        mark_jira_comment_processed("new-id", processed_set)
        assert "new-id" in processed_set
        assert check_jira_already_processed("new-id", set()) is True


class TestGetCommentAgeHours:
    def test_recent_comment(self):
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        age = _get_comment_age_hours(now_iso)
        assert age is not None
        assert age < 0.1  # Less than 6 minutes

    def test_invalid_timestamp(self):
        assert _get_comment_age_hours("not-a-timestamp") is None

    def test_empty_string(self):
        assert _get_comment_age_hours("") is None


class TestFetchJiraMentions:
    """Tests for the main fetch function using mocked HTTP."""

    def _make_config(self, nickname="koan-bot"):
        return {
            "jira": {
                "enabled": True,
                "base_url": "https://test.atlassian.net",
                "email": "bot@example.com",
                "api_token": "secret",
                "nickname": nickname,
                "max_age_hours": 24,
            }
        }

    def _make_search_response(self, issue_key="FOO-123"):
        return {
            "issues": [{"key": issue_key, "fields": {"summary": "Test issue"}}],
            "total": 1,
        }

    def _make_comments_response(self, comment_id="456", body="@koan-bot plan"):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        return {
            "comments": [
                {
                    "id": comment_id,
                    "body": body,
                    "author": {
                        "emailAddress": "user@example.com",
                        "displayName": "Test User",
                    },
                    "updated": now,
                }
            ],
            "total": 1,
        }

    def test_no_project_map_returns_empty(self):
        config = self._make_config()
        result = fetch_jira_mentions(config, {})
        assert isinstance(result, JiraFetchResult)
        assert result.mentions == []

    def test_missing_config_returns_empty(self):
        result = fetch_jira_mentions({}, {"FOO": "myproject"})
        assert result.mentions == []

    @patch("app.jira_notifications._jira_get")
    @patch("app.jira_notifications._jira_post")
    def test_finds_mention_in_comment(self, mock_post, mock_get):
        """Single @mention comment is returned as a mention dict."""
        # POST for JQL search; GET for issue comments
        mock_post.return_value = self._make_search_response("FOO-123")
        mock_get.return_value = self._make_comments_response("456", "@koan-bot plan")

        config = self._make_config()
        project_map = {"FOO": "myproject"}
        result = fetch_jira_mentions(config, project_map)

        assert len(result.mentions) == 1
        mention = result.mentions[0]
        assert mention["issue_key"] == "FOO-123"
        assert mention["project_name"] == "myproject"
        assert mention["comment_id"] == "456"
        assert mention["author_email"] == "user@example.com"

    @patch("app.jira_notifications._jira_get")
    @patch("app.jira_notifications._jira_post")
    def test_skips_comment_without_mention(self, mock_post, mock_get):
        """Comments without @bot are not returned."""
        mock_post.return_value = self._make_search_response("FOO-123")
        mock_get.return_value = self._make_comments_response("456", "just a regular comment")

        config = self._make_config()
        result = fetch_jira_mentions(config, {"FOO": "myproject"})
        assert result.mentions == []

    @patch("app.jira_notifications._jira_post")
    def test_skips_unknown_project(self, mock_post):
        """Issues with no project mapping are skipped."""
        mock_post.return_value = self._make_search_response("BAR-456")

        config = self._make_config()
        # BAR not in project_map
        result = fetch_jira_mentions(config, {"FOO": "myproject"})
        assert result.mentions == []

    @patch("app.jira_notifications._get_issue_comments")
    @patch("app.jira_notifications._search_issues_with_comments")
    def test_searches_only_registered_project_keys(self, mock_search, mock_comments):
        """Polling scope is limited to projects registered to this instance."""
        mock_search.return_value = []
        config = self._make_config()

        result = fetch_jira_mentions(
            config,
            {"FOO": "alpha", "BAR": "beta"},
        )

        assert result.mentions == []
        project_keys = mock_search.call_args.args[2]
        assert project_keys == ["BAR", "FOO"]
        mock_comments.assert_not_called()

    @patch("app.jira_notifications._get_issue_comments")
    @patch("app.jira_notifications._search_issues_with_comments")
    def test_unmapped_search_result_is_not_acknowledged_or_returned(
        self, mock_search, mock_comments,
    ):
        """If Jira returns an issue outside the ownership map, leave it untouched."""
        mock_search.return_value = [{"key": "BAR-456", "fields": {}}]
        config = self._make_config()

        result = fetch_jira_mentions(config, {"FOO": "myproject"})

        assert result.mentions == []
        mock_comments.assert_not_called()

    def test_pagination_across_three_pages(self):
        """Pagination: 3 pages of issues are all fetched via nextPageToken."""
        call_count = [0]

        def post_side_effect(base_url, auth_header, path, body=None):
            if "/search" in path:
                call_count[0] += 1
                all_issues = [{"key": f"FOO-{i}", "fields": {}} for i in range(6)]
                # Page 1: items 0-1, page 2: items 2-3, page 3: items 4-5
                page = call_count[0]
                start = (page - 1) * 2
                batch = all_issues[start:start + 2]
                is_last = page >= 3
                result = {"issues": batch, "isLast": is_last}
                if not is_last:
                    result["nextPageToken"] = f"token-page-{page + 1}"
                return result
            return None

        def get_side_effect(base_url, auth_header, path, params=None):
            if "/comment" in path:
                return {"comments": [], "total": 0}
            return None

        config = self._make_config()
        with patch("app.jira_notifications._jira_post", side_effect=post_side_effect), \
             patch("app.jira_notifications._jira_get", side_effect=get_side_effect):
            result = fetch_jira_mentions(config, {"FOO": "myproject"})

        assert isinstance(result, JiraFetchResult)
        assert call_count[0] == 3

    def test_pagination_halts_at_cap(self):
        """_search_issues_with_comments stops paginating once the cap is reached.

        Regression: previously the search paginated through *all* matching
        issues without bound, even though the caller only inspected the first
        N. With max_issues plumbed through, an unbounded result set must not
        cause unbounded API calls.
        """
        # Simulate 1000 available issues across 20 pages of 50. With a cap of
        # 200, pagination should stop after the 4th page (200 issues).
        page_size = 50
        cap = 200
        call_count = [0]

        def post_side_effect(base_url, auth_header, path, body=None):
            if "/search" in path:
                call_count[0] += 1
                page = call_count[0]
                start = (page - 1) * page_size
                batch = [
                    {"key": f"FOO-{i:04}", "fields": {}}
                    for i in range(start, start + page_size)
                ]
                # Server always says "more available"
                return {
                    "issues": batch,
                    "isLast": False,
                    "nextPageToken": f"token-page-{page + 1}",
                }
            return None

        def get_side_effect(base_url, auth_header, path, params=None):
            if "/comment" in path:
                return {"comments": [], "total": 0}
            return None

        config = self._make_config()
        with patch("app.jira_notifications._jira_post", side_effect=post_side_effect), \
             patch("app.jira_notifications._jira_get", side_effect=get_side_effect):
            result = fetch_jira_mentions(config, {"FOO": "myproject"})

        # 4 pages of 50 = 200 issues; pagination must stop there.
        assert call_count[0] == cap // page_size
        assert isinstance(result, JiraFetchResult)

    @patch("app.jira_notifications._jira_get")
    @patch("app.jira_notifications._jira_post")
    def test_api_failure_returns_empty(self, mock_post, mock_get):
        """API failure returns empty result, doesn't raise."""
        mock_post.return_value = None
        mock_get.return_value = None

        config = self._make_config()
        result = fetch_jira_mentions(config, {"FOO": "myproject"})
        assert result.mentions == []

    @patch("app.jira_notifications._get_issue_comments")
    @patch("app.jira_notifications._search_issues_with_comments")
    def test_mention_deep_in_results_is_found(self, mock_search, mock_comments):
        """Regression: a mention on an issue ranked deep in the result set
        (observed at rank 46 of 100 in production) must still be picked up.
        Previously _MAX_ISSUES_PER_CYCLE = 20 silently dropped it.
        """
        # 100 issues; the only one whose comments mention the bot is at index 46
        issues = [{"key": f"FOO-{i:03}", "fields": {"summary": f"i{i}"}} for i in range(100)]
        issues[46] = {"key": "FOO-046", "fields": {"summary": "deep target"}}
        mock_search.return_value = issues

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

        def comments_side_effect(base_url, auth_header, issue_key, since):
            # Only the deep-ranked issue has a body that triggers the mention
            if issue_key == "FOO-046":
                return [{
                    "id": "999",
                    "body": "@koan-bot plan",
                    "author": {"emailAddress": "u@example.com", "displayName": "U"},
                    "updated": now_iso,
                }]
            return []

        mock_comments.side_effect = comments_side_effect

        config = self._make_config()
        result = fetch_jira_mentions(config, {"FOO": "myproject"})

        assert len(result.mentions) == 1
        assert result.mentions[0]["issue_key"] == "FOO-046"

    @patch("app.jira_notifications._get_issue_comments")
    @patch("app.jira_notifications._search_issues_with_comments")
    def test_max_issues_per_cycle_override_narrows_inspection(
        self, mock_search, mock_comments,
    ):
        """jira.max_issues_per_cycle overrides the default cap. With a 5-cap
        and a mention at rank 10, the deeper mention is silently dropped —
        and only the first 5 issues should trigger comment fetches.
        """
        issues = [{"key": f"FOO-{i:03}", "fields": {"summary": f"i{i}"}} for i in range(20)]

        def search_side_effect(base_url, auth_header, project_keys, since, max_issues=None):
            return issues[:max_issues] if max_issues else issues

        mock_search.side_effect = search_side_effect

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

        def comments_side_effect(base_url, auth_header, issue_key, since):
            if issue_key == "FOO-010":  # past the 5-cap
                return [{
                    "id": "999",
                    "body": "@koan-bot plan",
                    "author": {"emailAddress": "u@example.com", "displayName": "U"},
                    "updated": now_iso,
                }]
            return []

        mock_comments.side_effect = comments_side_effect

        config = self._make_config()
        config["jira"]["max_issues_per_cycle"] = 5
        result = fetch_jira_mentions(config, {"FOO": "myproject"})

        # Cap takes effect: deeper mention dropped, only first 5 inspected.
        assert result.mentions == []
        inspected_keys = [call.args[2] for call in mock_comments.call_args_list]
        assert inspected_keys == [f"FOO-{i:03}" for i in range(5)]


class TestJiraHttpHelpers:
    """Low-level HTTP helpers stay pure with mocked urllib boundaries."""

    class _Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.body

    def test_make_auth_header_uses_basic_auth_encoding(self):
        from app.jira_notifications import _make_auth_header

        assert _make_auth_header("bot@example.com", "secret") == (
            "Basic Ym90QGV4YW1wbGUuY29tOnNlY3JldA=="
        )

    def test_jira_get_success_encodes_params(self):
        from app.jira_notifications import _jira_get

        response = self._Response(b'{"ok": true}')
        with patch("urllib.request.urlopen", return_value=response) as mock_open:
            result = _jira_get(
                "https://test.atlassian.net",
                "Basic token",
                "/rest/api/3/search",
                {"jql": "project = FOO"},
            )

        assert result == {"ok": True}
        request = mock_open.call_args.args[0]
        assert request.full_url.endswith("jql=project+%3D+FOO")

    def test_jira_get_failure_returns_none(self):
        from app.jira_notifications import _jira_get

        with patch("urllib.request.urlopen", side_effect=OSError("network")):
            assert _jira_get("https://test", "Basic token", "/rest") is None

    def test_jira_post_success_sets_method_and_body(self):
        from app.jira_notifications import _jira_post

        response = self._Response(b'{"id": "10001"}')
        with patch("urllib.request.urlopen", return_value=response) as mock_open:
            result = _jira_post(
                "https://test.atlassian.net",
                "Basic token",
                "/rest/api/3/issue",
                {"fields": {"summary": "Test"}},
            )

        assert result == {"id": "10001"}
        request = mock_open.call_args.args[0]
        assert request.get_method() == "POST"
        assert json.loads(request.data.decode("utf-8"))["fields"]["summary"] == "Test"

    def test_jira_post_empty_response_returns_none(self):
        from app.jira_notifications import _jira_post

        with patch("urllib.request.urlopen", return_value=self._Response(b"")):
            assert _jira_post("https://test", "Basic token", "/rest", {}) is None


class TestJiraIssueHelpers:
    def _patch_enabled_config(self):
        return (
            patch("app.utils.load_config", return_value={"jira": {"enabled": True}}),
            patch("app.jira_config.get_jira_enabled", return_value=True),
            patch("app.jira_config.validate_jira_config", return_value=None),
            patch("app.jira_config.get_jira_base_url", return_value="https://test.atlassian.net"),
            patch("app.jira_config.get_jira_email", return_value="bot@example.com"),
            patch("app.jira_config.get_jira_api_token", return_value="secret"),
        )

    def test_text_to_adf_splits_blank_lines_into_paragraphs(self):
        from app.jira_notifications import _text_to_adf

        adf = _text_to_adf("first line\nstill first\n\nsecond paragraph")

        assert adf["type"] == "doc"
        assert len(adf["content"]) == 2
        assert adf["content"][0]["content"][0]["text"] == "first line\nstill first"
        assert adf["content"][1]["content"][0]["text"] == "second paragraph"

    def test_resolve_branch_from_jira_key(self):
        from app.jira_notifications import resolve_branch_from_jira_key

        assert resolve_branch_from_jira_key("foo-123", {"FOO": "develop"}) == "develop"
        assert resolve_branch_from_jira_key("NO_DASH", {"NO": "main"}) is None

    def test_auth_from_config_disabled_raises(self):
        from app.jira_notifications import _jira_auth_from_config

        with (
            patch("app.utils.load_config", return_value={"jira": {"enabled": False}}),
            patch("app.jira_config.get_jira_enabled", return_value=False),
            pytest.raises(RuntimeError, match="not enabled"),
        ):
            _jira_auth_from_config()

    def test_fetch_jira_issue_returns_title_description_and_comments(self):
        from contextlib import ExitStack

        from app.jira_notifications import fetch_jira_issue

        issue = {
            "fields": {
                "summary": "Fix widget",
                "description": {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Details"}]}],
                },
            }
        }
        comments = {
            "comments": [
                {
                    "author": {"displayName": "Reviewer"},
                    "body": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Please fix"}]}],
                    },
                },
                {"author": {"emailAddress": "empty@example.com"}, "body": ""},
            ],
            "total": 2,
        }

        def get_side_effect(base_url, auth_header, path, params=None):
            if path == "/rest/api/3/issue/FOO-1":
                return issue
            if path == "/rest/api/3/issue/FOO-1/comment":
                return comments
            return None

        with ExitStack() as stack:
            for cm in self._patch_enabled_config():
                stack.enter_context(cm)
            stack.enter_context(
                patch("app.jira_notifications._jira_get", side_effect=get_side_effect)
            )
            title, body, fetched_comments = fetch_jira_issue("FOO-1")

        assert title == "Fix widget"
        assert "Details" in body
        assert fetched_comments == [{"author": "Reviewer", "body": "Please fix"}]

    def test_fetch_jira_issue_preserves_rich_adf_and_updated_metadata(self):
        from contextlib import ExitStack

        from app.jira_notifications import fetch_jira_issue

        issue = {"fields": {"summary": "Plan", "description": None}}
        comments = {
            "comments": [{
                "author": {"displayName": "Koan"},
                "updated": "2026-07-31T12:00:00.000+0000",
                "body": {
                    "type": "doc",
                    "content": [
                        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Summary"}]},
                        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [{"type": "text", "text": "print('ok')"}]},
                    ],
                },
            }],
            "total": 1,
        }

        def get_side_effect(_base_url, _auth_header, path, _params=None):
            return issue if path.endswith("/FOO-1") else comments

        with ExitStack() as stack:
            for cm in self._patch_enabled_config():
                stack.enter_context(cm)
            stack.enter_context(patch("app.jira_notifications._jira_get", side_effect=get_side_effect))
            _title, _body, fetched_comments = fetch_jira_issue("FOO-1")

        assert fetched_comments == [{
            "author": "Koan",
            "body": "## Summary\n\n```python\nprint('ok')\n```",
            "updated": "2026-07-31T12:00:00.000+0000",
        }]

    def test_fetch_jira_issue_api_failure_raises(self):
        from contextlib import ExitStack

        from app.jira_notifications import fetch_jira_issue

        with ExitStack() as stack:
            for cm in self._patch_enabled_config():
                stack.enter_context(cm)
            stack.enter_context(patch("app.jira_notifications._jira_get", return_value=None))
            with pytest.raises(RuntimeError, match="Failed to fetch"):
                fetch_jira_issue("FOO-404")

    def test_fetch_jira_issue_raises_when_comment_pagination_fails(self):
        """A failed page must not masquerade as the end of the comment list.

        `/implement` locates a (possibly multipart) plan in these comments; a
        silently truncated list sends it back to stale issue-body content.
        """
        from contextlib import ExitStack

        from app.jira_notifications import fetch_jira_issue

        issue = {"fields": {"summary": "Plan", "description": None}}

        def get_side_effect(_base_url, _auth_header, path, _params=None):
            return issue if path.endswith("/FOO-1") else None

        with ExitStack() as stack:
            for cm in self._patch_enabled_config():
                stack.enter_context(cm)
            stack.enter_context(
                patch("app.jira_notifications._jira_get", side_effect=get_side_effect)
            )
            with pytest.raises(RuntimeError, match="Failed to fetch comments"):
                fetch_jira_issue("FOO-1")

    def test_jira_add_comment_posts_adf(self):
        from app.jira_notifications import jira_add_comment

        with (
            patch("app.jira_notifications._jira_auth_from_config", return_value=("https://test", "Basic token")),
            patch("app.jira_notifications._jira_post", return_value={"id": "1"}) as mock_post,
        ):
            assert jira_add_comment("FOO-1", "hello\n\nworld") is True

        payload = mock_post.call_args.args[3]
        assert payload["body"]["type"] == "doc"
        assert len(payload["body"]["content"]) == 2

    def test_jira_edit_comment_posts_adf_via_put(self):
        from app.jira_notifications import jira_edit_comment

        with (
            patch("app.jira_notifications._jira_auth_from_config", return_value=("https://test", "Basic token")),
            patch("app.jira_notifications._jira_put", return_value={"id": "1"}) as mock_put,
        ):
            assert jira_edit_comment("FOO-1", "123", "hello\n\nworld") is True

        payload = mock_put.call_args.args[3]
        assert payload["body"]["type"] == "doc"
        assert "/rest/api/3/issue/FOO-1/comment/123" in mock_put.call_args.args[2]


    def test_jira_create_issue_rejects_invalid_project_key(self):
        from app.jira_notifications import jira_create_issue

        with pytest.raises(RuntimeError, match="Invalid Jira project key"):
            jira_create_issue("FOO;DROP", "title", "body")

    def test_jira_create_issue_returns_browse_url(self):
        from app.jira_notifications import jira_create_issue

        with (
            patch("app.jira_notifications._jira_auth_from_config", return_value=("https://test", "Basic token")),
            patch("app.jira_notifications._jira_post", return_value={"key": "FOO-123"}) as mock_post,
        ):
            url = jira_create_issue("FOO", "Title", "Body", issue_type="Bug")

        assert url == "https://test/browse/FOO-123"
        payload = mock_post.call_args.args[3]
        assert payload["fields"]["issuetype"]["name"] == "Bug"
        assert payload["fields"]["project"]["key"] == "FOO"

    def test_jira_create_issue_description_is_rich_adf(self):
        from app.jira_notifications import jira_create_issue

        body = "## Why This Matters\n\n- one\n- two\n\n---\n\nSome **bold** text."
        with (
            patch("app.jira_notifications._jira_auth_from_config", return_value=("https://test", "Basic token")),
            patch("app.jira_notifications._jira_post", return_value={"key": "FOO-9"}) as mock_post,
        ):
            jira_create_issue("FOO", "Title", body)

        desc = mock_post.call_args.args[3]["fields"]["description"]
        assert desc["type"] == "doc"
        block_types = [n["type"] for n in desc["content"]]
        # markdown structure became native ADF, not a wall of paragraphs
        assert "heading" in block_types
        assert "bulletList" in block_types
        assert "rule" in block_types
        marks = [
            m["type"]
            for n in desc["content"] if n["type"] == "paragraph"
            for t in n["content"] for m in t.get("marks", [])
        ]
        assert "strong" in marks

    def test_jira_add_comment_uses_rich_markdown_adf(self):
        """Comments retain their Markdown structure in Jira."""
        from app.jira_notifications import jira_add_comment

        body = "## Heading-looking line\n\n- bullet-looking line"
        with (
            patch("app.jira_notifications._jira_auth_from_config", return_value=("https://test", "Basic token")),
            patch("app.jira_notifications._jira_post", return_value={"id": "1"}) as mock_post,
        ):
            jira_add_comment("FOO-1", body)

        adf = mock_post.call_args.args[3]["body"]
        assert {n["type"] for n in adf["content"]} == {"heading", "bulletList"}

    def test_jira_search_issues_rejects_unsafe_project_key(self):
        from app.jira_notifications import jira_search_issues

        assert jira_search_issues("FOO;DROP", "widget") == []

    def test_jira_search_issues_maps_results_and_skips_missing_keys(self):
        from app.jira_notifications import jira_search_issues

        result_payload = {
            "issues": [
                {"key": "FOO-1", "fields": {"summary": "One"}},
                {"fields": {"summary": "Missing key"}},
                {"key": "FOO-2", "fields": None},
            ]
        }
        with (
            patch("app.jira_notifications._jira_auth_from_config", return_value=("https://test", "Basic token")),
            patch("app.jira_notifications._jira_post", return_value=result_payload) as mock_post,
        ):
            matches = jira_search_issues("FOO", "fix widget quickly please now", limit=0)

        assert matches == [
            {"key": "FOO-1", "title": "One", "url": "https://test/browse/FOO-1"},
            {"key": "FOO-2", "title": "", "url": "https://test/browse/FOO-2"},
        ]
        payload = mock_post.call_args.args[3]
        assert payload["maxResults"] == 1
        assert 'project = "FOO"' in payload["jql"]

    def test_search_issues_ignores_invalid_project_keys(self):
        from datetime import datetime, timezone

        from app.jira_notifications import _search_issues_with_comments

        with patch("app.jira_notifications._jira_post") as mock_post:
            result = _search_issues_with_comments(
                "https://test", "Basic token", ["FOO-BAD", "also_bad"],
                datetime.now(timezone.utc),
            )

        assert result == []
        mock_post.assert_not_called()
