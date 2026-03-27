"""Tests for github_url_parser.py — centralized URL parsing."""

import pytest

from app.github_url_parser import (
    parse_github_url,
    parse_issue_url,
    parse_pr_url,
    search_issue_url,
    search_pr_url,
)


class TestParsePrUrl:
    def test_valid_pr_url(self):
        owner, repo, number = parse_pr_url("https://github.com/sukria/koan/pull/42")
        assert owner == "sukria"
        assert repo == "koan"
        assert number == "42"

    def test_pr_url_with_fragment(self):
        owner, repo, number = parse_pr_url(
            "https://github.com/owner/repo/pull/1#issuecomment-123"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert number == "1"

    def test_pr_url_with_whitespace(self):
        owner, repo, number = parse_pr_url("  https://github.com/a/b/pull/99  ")
        assert owner == "a"
        assert repo == "b"
        assert number == "99"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid PR URL"):
            parse_pr_url("https://github.com/owner/repo/issues/42")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_pr_url("not a url")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_pr_url("")


class TestParseIssueUrl:
    def test_valid_issue_url(self):
        owner, repo, number = parse_issue_url("https://github.com/sukria/koan/issues/243")
        assert owner == "sukria"
        assert repo == "koan"
        assert number == "243"

    def test_issue_url_with_fragment(self):
        owner, repo, number = parse_issue_url(
            "https://github.com/o/r/issues/5#issuecomment-999"
        )
        assert owner == "o"
        assert repo == "r"
        assert number == "5"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid issue URL"):
            parse_issue_url("https://github.com/owner/repo/pull/42")


class TestParseGithubUrl:
    def test_pr_url(self):
        owner, repo, url_type, number = parse_github_url(
            "https://github.com/sukria/koan/pull/42"
        )
        assert owner == "sukria"
        assert repo == "koan"
        assert url_type == "pull"
        assert number == "42"

    def test_issue_url(self):
        owner, repo, url_type, number = parse_github_url(
            "https://github.com/sukria/koan/issues/243"
        )
        assert url_type == "issues"
        assert number == "243"

    def test_with_fragment(self):
        owner, repo, url_type, number = parse_github_url(
            "https://github.com/a/b/pull/1#diff"
        )
        assert number == "1"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            parse_github_url("https://example.com/not-github")


class TestSearchPrUrl:
    def test_clean_url(self):
        owner, repo, number = search_pr_url(
            "https://github.com/sukria/koan/pull/42"
        )
        assert owner == "sukria"
        assert repo == "koan"
        assert number == "42"

    def test_embedded_url(self):
        owner, repo, number = search_pr_url(
            "Check this: https://github.com/foo/bar/pull/77 please"
        )
        assert owner == "foo"
        assert repo == "bar"
        assert number == "77"

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="No PR URL found"):
            search_pr_url("no url here")

    def test_issue_url_not_matched(self):
        with pytest.raises(ValueError):
            search_pr_url("https://github.com/o/r/issues/1")


class TestSearchIssueUrl:
    def test_clean_url(self):
        owner, repo, number = search_issue_url(
            "https://github.com/acme/widget/issues/42"
        )
        assert owner == "acme"
        assert repo == "widget"
        assert number == "42"

    def test_embedded_url(self):
        owner, repo, number = search_issue_url(
            "See https://github.com/org/repo/issues/99 for details"
        )
        assert owner == "org"
        assert repo == "repo"
        assert number == "99"

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="No issue URL found"):
            search_issue_url("no url here")

    def test_pr_url_not_matched(self):
        with pytest.raises(ValueError):
            search_issue_url("https://github.com/o/r/pull/1")
