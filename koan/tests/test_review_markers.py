"""Tests for app.review_markers — HTML comment marker helpers."""

import pytest

from app.review_markers import (
    SUMMARY_TAG,
    COMMIT_IDS_START,
    COMMIT_IDS_END,
    IN_PROGRESS_START,
    IN_PROGRESS_END,
    extract_between_markers,
    remove_section,
    wrap_section,
    replace_section,
)


# ---------------------------------------------------------------------------
# extract_between_markers
# ---------------------------------------------------------------------------

class TestExtractBetweenMarkers:
    def test_extracts_content(self):
        body = f"before{COMMIT_IDS_START}sha1\nsha2{COMMIT_IDS_END}after"
        result = extract_between_markers(body, COMMIT_IDS_START, COMMIT_IDS_END)
        assert result == "sha1\nsha2"

    def test_returns_none_when_start_absent(self):
        body = f"no markers here{COMMIT_IDS_END}"
        assert extract_between_markers(body, COMMIT_IDS_START, COMMIT_IDS_END) is None

    def test_returns_none_when_end_absent(self):
        body = f"{COMMIT_IDS_START}sha1\nsha2 — end tag missing"
        assert extract_between_markers(body, COMMIT_IDS_START, COMMIT_IDS_END) is None

    def test_returns_none_when_both_absent(self):
        assert extract_between_markers("plain text", COMMIT_IDS_START, COMMIT_IDS_END) is None

    def test_returns_empty_string_for_empty_content(self):
        body = f"{COMMIT_IDS_START}{COMMIT_IDS_END}"
        result = extract_between_markers(body, COMMIT_IDS_START, COMMIT_IDS_END)
        assert result == ""

    def test_content_with_double_dashes(self):
        """Content containing '--' (e.g. commit messages) is handled."""
        body = f"{COMMIT_IDS_START}abc--def{COMMIT_IDS_END}"
        assert extract_between_markers(body, COMMIT_IDS_START, COMMIT_IDS_END) == "abc--def"

    def test_returns_none_when_tags_in_wrong_order(self):
        """End tag before start tag → None."""
        body = f"{COMMIT_IDS_END}content{COMMIT_IDS_START}"
        assert extract_between_markers(body, COMMIT_IDS_START, COMMIT_IDS_END) is None


# ---------------------------------------------------------------------------
# remove_section
# ---------------------------------------------------------------------------

class TestRemoveSection:
    def test_removes_tagged_block(self):
        body = f"before\n{IN_PROGRESS_START}⏳ in progress{IN_PROGRESS_END}\nafter"
        result = remove_section(body, IN_PROGRESS_START, IN_PROGRESS_END)
        assert IN_PROGRESS_START not in result
        assert IN_PROGRESS_END not in result
        assert "⏳ in progress" not in result
        assert "before" in result
        assert "after" in result

    def test_returns_unchanged_when_start_absent(self):
        body = "no markers"
        assert remove_section(body, IN_PROGRESS_START, IN_PROGRESS_END) == body

    def test_returns_unchanged_when_end_absent(self):
        body = f"{IN_PROGRESS_START}content without end"
        assert remove_section(body, IN_PROGRESS_START, IN_PROGRESS_END) == body

    def test_removes_only_the_tagged_block(self):
        body = f"header\n{IN_PROGRESS_START}temp{IN_PROGRESS_END}\nfooter"
        result = remove_section(body, IN_PROGRESS_START, IN_PROGRESS_END)
        assert result == "header\n\nfooter"

    def test_empty_content_block(self):
        body = f"{IN_PROGRESS_START}{IN_PROGRESS_END}"
        assert remove_section(body, IN_PROGRESS_START, IN_PROGRESS_END) == ""


# ---------------------------------------------------------------------------
# wrap_section
# ---------------------------------------------------------------------------

class TestWrapSection:
    def test_wraps_content(self):
        result = wrap_section("hello", COMMIT_IDS_START, COMMIT_IDS_END)
        assert result == f"{COMMIT_IDS_START}hello{COMMIT_IDS_END}"

    def test_wraps_empty_content(self):
        result = wrap_section("", COMMIT_IDS_START, COMMIT_IDS_END)
        assert result == f"{COMMIT_IDS_START}{COMMIT_IDS_END}"

    def test_roundtrip_with_extract(self):
        content = "sha-abc\nsha-def"
        wrapped = wrap_section(content, COMMIT_IDS_START, COMMIT_IDS_END)
        extracted = extract_between_markers(wrapped, COMMIT_IDS_START, COMMIT_IDS_END)
        assert extracted == content


# ---------------------------------------------------------------------------
# replace_section
# ---------------------------------------------------------------------------

class TestReplaceSection:
    def test_appends_when_absent(self):
        body = "## Review\n\nLooks good."
        result = replace_section(body, COMMIT_IDS_START, COMMIT_IDS_END, "sha1")
        assert result.endswith(f"{COMMIT_IDS_START}sha1{COMMIT_IDS_END}")
        assert "## Review" in result

    def test_replaces_existing_block(self):
        body = f"header\n{COMMIT_IDS_START}old-sha{COMMIT_IDS_END}\nfooter"
        result = replace_section(body, COMMIT_IDS_START, COMMIT_IDS_END, "new-sha")
        assert "old-sha" not in result
        assert f"{COMMIT_IDS_START}new-sha{COMMIT_IDS_END}" in result
        assert "header" in result
        assert "footer" in result

    def test_idempotent_on_same_content(self):
        body = f"text\n{COMMIT_IDS_START}sha1{COMMIT_IDS_END}"
        result1 = replace_section(body, COMMIT_IDS_START, COMMIT_IDS_END, "sha1")
        result2 = replace_section(result1, COMMIT_IDS_START, COMMIT_IDS_END, "sha1")
        assert result1 == result2

    def test_handles_malformed_missing_end_tag(self):
        """Start present but end absent — appends new block."""
        body = f"text\n{COMMIT_IDS_START}orphan start"
        result = replace_section(body, COMMIT_IDS_START, COMMIT_IDS_END, "sha1")
        assert result.endswith(f"{COMMIT_IDS_START}sha1{COMMIT_IDS_END}")

    def test_replaces_with_empty_content(self):
        body = f"{COMMIT_IDS_START}old{COMMIT_IDS_END}"
        result = replace_section(body, COMMIT_IDS_START, COMMIT_IDS_END, "")
        assert result == f"{COMMIT_IDS_START}{COMMIT_IDS_END}"
