"""Tests for missions.py CI section helpers (Phase 1 of #1132)."""

import pytest

from app.missions import (
    add_ci_item,
    get_ci_items,
    parse_sections,
    remove_ci_item,
    update_ci_item_attempt,
)

PR_URL = "https://github.com/owner/repo/pull/42"
PR_URL_2 = "https://github.com/owner/repo/pull/99"


def _make_ci_content(extra_lines=""):
    """Return a minimal missions.md with a ## CI section."""
    return f"# Missions\n\n## CI\n\n{extra_lines}\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"


class TestAddCiItem:
    def test_adds_item_to_empty_ci_section(self):
        content = _make_ci_content()
        result = add_ci_item(content, "myproj", PR_URL, "42", "koan/fix", "owner/repo", 5)
        items = get_ci_items(result)
        assert len(items) == 1
        item = items[0]
        assert item["pr_url"] == PR_URL
        assert item["project"] == "myproj"
        assert item["branch"] == "koan/fix"
        assert item["full_repo"] == "owner/repo"
        assert item["pr_number"] == "42"
        assert item["attempt"] == 0
        assert item["max_attempts"] == 5

    def test_deduplicates_by_pr_url_resets_attempt(self):
        content = _make_ci_content()
        content = add_ci_item(content, "myproj", PR_URL, "42", "koan/fix", "owner/repo", 5)
        content = update_ci_item_attempt(content, PR_URL)
        # Re-add the same URL — should reset attempt to 0
        content = add_ci_item(content, "myproj", PR_URL, "42", "koan/fix-v2", "owner/repo", 5)
        items = get_ci_items(content)
        assert len(items) == 1
        assert items[0]["attempt"] == 0
        assert items[0]["branch"] == "koan/fix-v2"

    def test_creates_ci_section_if_missing(self):
        content = "# Missions\n\n## Pending\n\n## Done\n"
        result = add_ci_item(content, "proj", PR_URL, "42", "koan/b", "o/r", 3)
        assert "## CI" in result
        items = get_ci_items(result)
        assert len(items) == 1

    def test_creates_from_empty_content(self):
        result = add_ci_item("", "proj", PR_URL, "42", "koan/b", "o/r", 3)
        items = get_ci_items(result)
        assert len(items) == 1
        assert items[0]["pr_url"] == PR_URL

    def test_multiple_items(self):
        content = _make_ci_content()
        content = add_ci_item(content, "p1", PR_URL, "42", "koan/a", "o/r", 5)
        content = add_ci_item(content, "p2", PR_URL_2, "99", "koan/b", "o/r", 3)
        items = get_ci_items(content)
        assert len(items) == 2
        urls = {i["pr_url"] for i in items}
        assert urls == {PR_URL, PR_URL_2}

    def test_no_project_name(self):
        content = _make_ci_content()
        result = add_ci_item(content, "", PR_URL, "42", "koan/b", "o/r", 5)
        items = get_ci_items(result)
        assert items[0]["project"] == ""


class TestRemoveCiItem:
    def test_removes_matching_entry(self):
        content = _make_ci_content()
        content = add_ci_item(content, "proj", PR_URL, "42", "koan/b", "o/r", 5)
        content = remove_ci_item(content, PR_URL)
        assert get_ci_items(content) == []

    def test_noop_if_not_found(self):
        content = _make_ci_content()
        result = remove_ci_item(content, PR_URL)
        assert result == remove_ci_item(content, PR_URL)  # Idempotent

    def test_only_removes_matching_url(self):
        content = _make_ci_content()
        content = add_ci_item(content, "p1", PR_URL, "42", "koan/a", "o/r", 5)
        content = add_ci_item(content, "p2", PR_URL_2, "99", "koan/b", "o/r", 3)
        content = remove_ci_item(content, PR_URL)
        items = get_ci_items(content)
        assert len(items) == 1
        assert items[0]["pr_url"] == PR_URL_2

    def test_noop_on_empty_content(self):
        assert remove_ci_item("", PR_URL) == ""


class TestGetCiItems:
    def test_empty_section_returns_empty_list(self):
        assert get_ci_items("") == []
        assert get_ci_items(_make_ci_content()) == []

    def test_parses_all_fields(self):
        content = _make_ci_content()
        content = add_ci_item(content, "myproj", PR_URL, "42", "koan/fix", "owner/repo", 5)
        items = get_ci_items(content)
        assert len(items) == 1
        item = items[0]
        assert item["project"] == "myproj"
        assert item["pr_url"] == PR_URL
        assert item["pr_number"] == "42"
        assert item["branch"] == "koan/fix"
        assert item["full_repo"] == "owner/repo"
        assert "queued" in item
        assert item["attempt"] == 0
        assert item["max_attempts"] == 5

    def test_ci_items_not_in_pending_section(self):
        """CI items must not appear in the pending section."""
        content = _make_ci_content()
        content = add_ci_item(content, "proj", PR_URL, "42", "koan/b", "o/r", 5)
        sections = parse_sections(content)
        assert sections["pending"] == []
        assert sections["ci"] == [content.splitlines()[
            next(i for i, l in enumerate(content.splitlines()) if PR_URL in l)
        ]]


class TestUpdateCiItemAttempt:
    def test_increments_attempt(self):
        content = _make_ci_content()
        content = add_ci_item(content, "proj", PR_URL, "42", "koan/b", "o/r", 5)
        content = update_ci_item_attempt(content, PR_URL)
        items = get_ci_items(content)
        assert items[0]["attempt"] == 1

    def test_increments_multiple_times(self):
        content = _make_ci_content()
        content = add_ci_item(content, "proj", PR_URL, "42", "koan/b", "o/r", 5)
        for _ in range(3):
            content = update_ci_item_attempt(content, PR_URL)
        items = get_ci_items(content)
        assert items[0]["attempt"] == 3

    def test_does_not_exceed_max(self):
        content = _make_ci_content()
        content = add_ci_item(content, "proj", PR_URL, "42", "koan/b", "o/r", 2)
        for _ in range(10):
            content = update_ci_item_attempt(content, PR_URL)
        items = get_ci_items(content)
        assert items[0]["attempt"] == 2  # Capped at max

    def test_noop_if_not_found(self):
        content = _make_ci_content()
        result = update_ci_item_attempt(content, PR_URL)
        # No CI items should appear after a no-op update
        assert get_ci_items(result) == []

    def test_noop_on_empty_content(self):
        assert update_ci_item_attempt("", PR_URL) == ""


class TestRoundTrip:
    def test_add_get_update_get_remove(self):
        """Full round-trip: add → get → update → get → remove → get (empty)."""
        content = "# Missions\n\n## CI\n\n## Pending\n\n## Done\n"

        # Add
        content = add_ci_item(content, "proj", PR_URL, "42", "koan/feat", "o/r", 5)
        items = get_ci_items(content)
        assert len(items) == 1
        assert items[0]["attempt"] == 0

        # Update attempt
        content = update_ci_item_attempt(content, PR_URL)
        items = get_ci_items(content)
        assert items[0]["attempt"] == 1

        # Remove
        content = remove_ci_item(content, PR_URL)
        assert get_ci_items(content) == []
