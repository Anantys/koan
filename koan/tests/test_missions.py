"""Tests for missions.py — centralized missions.md parser."""

import pytest
from unittest.mock import patch
from datetime import datetime
from app.missions import (
    cancel_pending_mission,
    classify_section,
    complete_mission,
    delete_idea,
    extract_timestamps,
    fail_mission,
    format_duration,
    insert_idea,
    insert_mission,
    list_pending,
    mission_timing_display,
    parse_ideas,
    parse_sections,
    count_pending,
    extract_next_pending,
    extract_project_tag,
    extract_tdd_tag,
    extract_now_flag,
    group_by_project,
    find_section_boundaries,
    normalize_content,
    promote_all_ideas,
    promote_idea,
    quarantine_mission,
    QUARANTINE_MAX_BYTES,
    reorder_mission,
    requeue_mission,
    sanitize_mission_text,
    stamp_queued,
    stamp_started,
    start_mission,
    strip_timestamps,
    prune_done_section,
    _enforce_quarantine_cap,
    _flush_in_progress_to_done,
    DEFAULT_SKELETON,
)


# --- classify_section ---

class TestClassifySection:
    def test_french_pending(self):
        assert classify_section("En attente") == "pending"

    def test_english_pending(self):
        assert classify_section("Pending") == "pending"

    def test_french_in_progress(self):
        assert classify_section("En cours") == "in_progress"

    def test_english_in_progress(self):
        assert classify_section("In Progress") == "in_progress"

    def test_french_done(self):
        assert classify_section("Terminées") == "done"

    def test_english_done(self):
        assert classify_section("Done") == "done"
        assert classify_section("Completed") == "done"

    def test_unknown(self):
        assert classify_section("Random") is None

    def test_case_insensitive(self):
        assert classify_section("EN ATTENTE") == "pending"
        assert classify_section("pending") == "pending"


# --- parse_sections ---

SAMPLE_CONTENT = (
    "# Missions\n\n"
    "## Pending\n\n"
    "- Fix the bug\n"
    "- Another task\n\n"
    "## In Progress\n\n"
    "- Working on it\n\n"
    "## Done\n\n"
    "- **Done task** (session 1)\n"
)

class TestParseSections:
    def test_basic_parsing(self):
        result = parse_sections(SAMPLE_CONTENT)
        assert len(result["pending"]) == 2
        assert len(result["in_progress"]) == 1
        assert len(result["done"]) == 1

    def test_empty_content(self):
        result = parse_sections("")
        assert result == {"pending": [], "in_progress": [], "done": [], "failed": []}

    def test_complex_mission(self):
        content = (
            "## In Progress\n\n"
            "### Big project\n"
            "- Step 1\n"
            "- Step 2\n\n"
            "## Done\n"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 1
        assert "### Big project" in result["in_progress"][0]
        assert "- Step 1" in result["in_progress"][0]

    def test_english_headers(self):
        content = "## Pending\n\n- Task\n\n## In Progress\n\n## Done\n"
        result = parse_sections(content)
        assert len(result["pending"]) == 1

    def test_continuation_lines(self):
        content = "## Pending\n\n- Main task\n  sub-item detail\n"
        result = parse_sections(content)
        assert len(result["pending"]) == 1
        assert "sub-item detail" in result["pending"][0]


# --- insert_mission ---

class TestInsertMission:
    def test_insert_into_existing(self):
        content = "# Missions\n\n## Pending\n\n## In Progress\n"
        result = insert_mission(content, "- New task")
        assert "- New task" in result
        # Should be before "## In Progress"
        assert result.index("- New task") < result.index("## In Progress")

    def test_insert_into_empty(self):
        result = insert_mission("", "- New task")
        assert "## Pending" in result
        assert "- New task" in result

    def test_insert_english_header(self):
        content = "## Pending\n\n## In Progress\n"
        result = insert_mission(content, "- Task")
        assert "- Task" in result

    def test_insert_no_pending_section(self):
        content = "# Missions\n\n## In Progress\n"
        result = insert_mission(content, "- Task")
        assert "## Pending" in result
        assert "- Task" in result


# --- count_pending ---

class TestCountPending:
    def test_count(self):
        assert count_pending(SAMPLE_CONTENT) == 2

    def test_empty(self):
        assert count_pending("## Pending\n\n## In Progress\n") == 0

    def test_ignores_in_progress(self):
        content = "## Pending\n\n- One\n\n## In Progress\n\n- Two\n"
        assert count_pending(content) == 1


# --- extract_next_pending ---

class TestExtractNextPending:
    def test_basic(self):
        assert extract_next_pending(SAMPLE_CONTENT) == "- Fix the bug"

    def test_empty(self):
        assert extract_next_pending("## Pending\n\n## In Progress\n") == ""

    def test_project_filter_match(self):
        content = "## Pending\n\n- [projet:koan] Fix memory\n- [projet:anantys] Fix stripe\n"
        assert extract_next_pending(content, "koan") == "- [projet:koan] Fix memory"

    def test_project_filter_skip(self):
        content = "## Pending\n\n- [projet:anantys] Fix stripe\n"
        assert extract_next_pending(content, "koan") == ""

    def test_untagged_matches_any_project(self):
        content = "## Pending\n\n- Untagged task\n"
        assert extract_next_pending(content, "koan") == "- Untagged task"

    def test_english_sections(self):
        content = "## Pending\n\n- English task\n\n## In Progress\n"
        assert extract_next_pending(content) == "- English task"


# --- extract_next_pending: multi-line missions ---

class TestExtractNextPendingMultiLine:
    """Tests for multi-line mission block extraction."""

    def test_simple_continuation(self):
        """Indented continuation lines are included in the block."""
        content = (
            "## Pending\n\n"
            "- Fix the parser\n"
            "  to handle multi-line missions\n"
            "  and code blocks\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content)
        assert "- Fix the parser" in result
        assert "to handle multi-line missions" in result
        assert "and code blocks" in result

    def test_code_block_included(self):
        """Code-fenced blocks within a mission are included."""
        content = (
            "## Pending\n\n"
            "- Implement the following:\n"
            "  ```python\n"
            "  def hello():\n"
            "      print('world')\n"
            "  ```\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content)
        assert "- Implement the following:" in result
        assert "```python" in result
        assert "def hello():" in result
        assert "print('world')" in result

    def test_code_block_with_empty_lines(self):
        """Empty lines inside code fences do NOT terminate the block."""
        content = (
            "## Pending\n\n"
            "- Fix the bug with this code:\n"
            "  ```\n"
            "  line1\n"
            "\n"
            "  line3\n"
            "  ```\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content)
        assert "line1" in result
        assert "line3" in result
        assert result.count("```") == 2

    def test_stops_at_next_item(self):
        """Multi-line block stops at the next '- ' item."""
        content = (
            "## Pending\n\n"
            "- First mission\n"
            "  with details\n"
            "- Second mission\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content)
        assert "First mission" in result
        assert "with details" in result
        assert "Second mission" not in result

    def test_stops_at_empty_line(self):
        """Multi-line block stops at an empty line (outside code fences)."""
        content = (
            "## Pending\n\n"
            "- Mission text\n"
            "  continuation\n"
            "\n"
            "- Next mission\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content)
        assert "Mission text" in result
        assert "continuation" in result
        assert "Next mission" not in result

    def test_stops_at_section_header(self):
        """Multi-line block stops at ## header."""
        content = (
            "## Pending\n\n"
            "- Mission\n"
            "  details\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content)
        assert "Mission" in result
        assert "details" in result

    def test_project_filter_with_multiline(self):
        """Project filter works with multi-line missions."""
        content = (
            "## Pending\n\n"
            "- [project:koan] Fix the parser\n"
            "  to handle multi-line\n"
            "- [project:other] Other task\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "koan")
        assert "Fix the parser" in result
        assert "to handle multi-line" in result
        assert "Other task" not in result

    def test_project_filter_skips_multiline_non_match(self):
        """When first multi-line mission doesn't match, skip to next."""
        content = (
            "## Pending\n\n"
            "- [project:other] Wrong project\n"
            "  with continuation\n"
            "- [project:koan] Right project\n"
            "  with its own continuation\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "koan")
        assert "Right project" in result
        assert "its own continuation" in result
        assert "Wrong project" not in result

    def test_single_line_still_works(self):
        """Single-line missions still return just the one line."""
        content = "## Pending\n\n- Simple task\n\n## In Progress\n"
        result = extract_next_pending(content)
        assert result == "- Simple task"

    def test_real_world_multiline_mission(self):
        """Real-world multi-line mission from missions.md."""
        content = (
            "## Pending\n\n"
            "- [project:koan] mission in the pending queue can be on multiple lines\n"
            "  to for example provide a code block or continue the discussion.\n"
            "  I want to improve the parsing from Missions when we read them.\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "koan")
        assert "multiple lines" in result
        assert "code block" in result
        assert "improve the parsing" in result

    def test_subheader_with_multiline(self):
        """Multi-line missions under ### project sub-headers."""
        content = (
            "## Pending\n\n"
            "### project:koan\n"
            "- Fix the parser\n"
            "  with continuation\n"
            "- Another task\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "koan")
        assert "Fix the parser" in result
        assert "with continuation" in result
        assert "Another task" not in result


class TestParseSectionsCodeFence:
    """Tests for code-fenced block handling in parse_sections."""

    def test_code_fence_in_mission(self):
        """Code fences within a mission item are preserved."""
        content = (
            "## Pending\n\n"
            "- Task with code\n"
            "  ```python\n"
            "  x = 1\n"
            "  ```\n\n"
            "## In Progress\n"
        )
        result = parse_sections(content)
        assert len(result["pending"]) == 1
        assert "```python" in result["pending"][0]
        assert "x = 1" in result["pending"][0]

    def test_code_fence_with_empty_lines_in_complex_block(self):
        """Empty lines inside code fences in ### blocks don't split the block."""
        content = (
            "## In Progress\n\n"
            "### Big task\n"
            "```\n"
            "line1\n"
            "\n"
            "line3\n"
            "```\n\n"
            "## Done\n"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 1
        assert "line1" in result["in_progress"][0]
        assert "line3" in result["in_progress"][0]


# --- extract_project_tag ---

class TestExtractProjectTag:
    def test_with_tag(self):
        assert extract_project_tag("- [project:koan] Fix bug") == "koan"

    def test_french_tag(self):
        assert extract_project_tag("- [projet:anantys] Fix stripe") == "anantys"

    def test_no_tag(self):
        assert extract_project_tag("- Plain task") == "default"


# --- group_by_project ---

class TestGroupByProject:
    def test_grouping(self):
        content = (
            "## Pending\n\n"
            "- [project:koan] Fix memory\n"
            "- [project:anantys] Fix stripe\n"
            "- Untagged\n\n"
            "## In Progress\n\n"
            "- [project:koan] Working\n"
        )
        result = group_by_project(content)
        assert len(result["koan"]["pending"]) == 1
        assert len(result["koan"]["in_progress"]) == 1
        assert len(result["anantys"]["pending"]) == 1
        assert len(result["default"]["pending"]) == 1


    def test_single_project_no_tags(self):
        """Single-project setup: all missions untagged go to 'default'."""
        content = (
            "## Pending\n\n"
            "- Fix the login bug\n"
            "- Add dark mode\n\n"
            "## In Progress\n\n"
            "- Write documentation\n\n"
            "## Done\n\n"
            "- Initial setup\n"
        )
        result = group_by_project(content)
        assert list(result.keys()) == ["default"]
        assert len(result["default"]["pending"]) == 2
        assert len(result["default"]["in_progress"]) == 1


# --- find_section_boundaries ---

class TestFindSectionBoundaries:
    def test_boundaries(self):
        lines = [
            "# Missions",
            "",
            "## Pending",
            "",
            "- Task",
            "",
            "## In Progress",
            "",
            "## Done",
            "",
        ]
        result = find_section_boundaries(lines)
        assert result["pending"] == (2, 6)
        assert result["in_progress"] == (6, 8)
        assert result["done"] == (8, 10)

    def test_missing_section(self):
        lines = ["## Pending", "", "- Task"]
        result = find_section_boundaries(lines)
        assert "pending" in result
        assert "in_progress" not in result


# --- parse_sections edge cases (complex blocks) ---

class TestParseSectionsComplexBlocks:
    """Tests for ### block flushing at section boundaries, sequential blocks, and EOF."""

    def test_complex_block_flushed_at_section_boundary(self):
        """### block in one section should be flushed when next ## section starts (lines 53-55)."""
        content = (
            "## In Progress\n\n"
            "### Big project\n"
            "- Step 1\n"
            "- Step 2\n"
            "## Done\n\n"
            "- Done task\n"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 1
        assert "### Big project" in result["in_progress"][0]
        assert "- Step 2" in result["in_progress"][0]
        assert len(result["done"]) == 1
        assert "- Done task" in result["done"][0]

    def test_sequential_complex_blocks_same_section(self):
        """Two ### blocks in the same section should be separate entries (lines 65-66)."""
        content = (
            "## In Progress\n\n"
            "### Block A\n"
            "- Detail A\n"
            "### Block B\n"
            "- Detail B\n\n"
            "## Done\n"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 2
        assert "### Block A" in result["in_progress"][0]
        assert "### Block B" in result["in_progress"][1]

    def test_complex_block_at_eof_no_trailing_newline(self):
        """### block at end of file with no trailing blank line (lines 82-83)."""
        content = (
            "## In Progress\n\n"
            "### Final block\n"
            "- Last item"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 1
        assert "### Final block" in result["in_progress"][0]
        assert "- Last item" in result["in_progress"][0]

    def test_mixed_simple_and_complex_same_section(self):
        """Simple - items followed by ### block in same section."""
        content = (
            "## Pending\n\n"
            "- Simple task\n"
            "### Complex task\n"
            "- Sub-detail\n\n"
            "## In Progress\n"
        )
        result = parse_sections(content)
        assert len(result["pending"]) == 2
        assert result["pending"][0] == "- Simple task"
        assert "### Complex task" in result["pending"][1]

    def test_complex_block_with_strikethrough(self):
        """Real-world pattern: ### block with ~~done~~ items (from actual missions.md)."""
        content = (
            "## In Progress\n\n"
            "### project:anantys Admin Dashboard\n"
            "- ~~Explorer l'admin~~ done\n"
            "- ~~Cartographier les données~~ done\n"
            "- Reste à faire : V2\n\n"
            "## Done\n"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 1
        block = result["in_progress"][0]
        assert "~~Explorer" in block
        assert "Reste à faire" in block

    def test_empty_complex_block(self):
        """### header with no content lines before next ### or section."""
        content = (
            "## In Progress\n\n"
            "### Empty block\n"
            "### Second block\n"
            "- Content\n\n"
            "## Done\n"
        )
        result = parse_sections(content)
        assert len(result["in_progress"]) == 2

    def test_unrecognized_section_header(self):
        """Content under unrecognized ## header should be ignored."""
        content = (
            "## Pending\n\n"
            "- Task\n\n"
            "## Random section\n\n"
            "- Should be ignored\n\n"
            "## In Progress\n"
        )
        result = parse_sections(content)
        assert len(result["pending"]) == 1
        assert result["pending"][0] == "- Task"
        # "Should be ignored" must not appear in any section
        for key in result:
            for item in result[key]:
                assert "Should be ignored" not in item


# --- Sub-header project grouping (### project:X) ---

class TestSubHeaderProjectGrouping:
    """Tests for ### project:X sub-headers in pending section."""

    SUBHEADER_CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "### projet:anantys-back\n\n"
        "### project:koan\n"
        "- Fix the rotation bug\n"
        "- Fix test warnings\n\n"
        "## In Progress\n\n"
        "## Done\n"
    )

    def test_extract_pending_with_subheader_filter_match(self):
        """Missions under ### project:koan should match when filtering for koan."""
        result = extract_next_pending(self.SUBHEADER_CONTENT, "koan")
        assert result == "- Fix the rotation bug"

    def test_extract_pending_with_subheader_filter_skip(self):
        """Missions under ### project:koan should NOT match when filtering for anantys-back."""
        result = extract_next_pending(self.SUBHEADER_CONTENT, "anantys-back")
        assert result == ""

    def test_extract_pending_no_filter_returns_first(self):
        """Without project filter, returns first mission regardless of sub-header."""
        result = extract_next_pending(self.SUBHEADER_CONTENT)
        assert result == "- Fix the rotation bug"

    def test_inline_tag_overrides_subheader(self):
        """Inline [project:X] tag takes priority over ### sub-header context."""
        content = (
            "## Pending\n\n"
            "### project:koan\n"
            "- [project:anantys] Overridden task\n"
            "- Normal koan task\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "koan")
        assert result == "- Normal koan task"

    def test_untagged_outside_subheader_matches_any(self):
        """Missions outside any sub-header (untagged) match any project filter."""
        content = (
            "## Pending\n\n"
            "- Untagged task\n"
            "### project:koan\n"
            "- Kōan task\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "anantys")
        assert result == "- Untagged task"

    def test_french_subheader_variant(self):
        """### projet:X (French) should also work."""
        content = (
            "## Pending\n\n"
            "### projet:anantys-back\n"
            "- French tagged task\n\n"
            "## In Progress\n"
        )
        result = extract_next_pending(content, "anantys-back")
        assert result == "- French tagged task"

    def test_extract_project_tag_from_subheader(self):
        """extract_project_tag should match ### project:X format in block text."""
        block = "### project:koan\n- Fix bug\n- Fix tests"
        assert extract_project_tag(block) == "koan"

    def test_extract_project_tag_french_subheader(self):
        block = "### projet:anantys-back\n- Task"
        assert extract_project_tag(block) == "anantys-back"

    # --- extract_tdd_tag ---

    def test_tdd_tag_present(self):
        """extract_tdd_tag should detect [tdd] tag."""
        assert extract_tdd_tag("- [tdd] Add user validation") is True

    def test_tdd_tag_absent(self):
        """extract_tdd_tag should return False when no [tdd] tag."""
        assert extract_tdd_tag("- Add user validation") is False

    def test_tdd_tag_case_insensitive(self):
        """extract_tdd_tag should be case-insensitive."""
        assert extract_tdd_tag("- [TDD] Add tests") is True
        assert extract_tdd_tag("- [Tdd] Add tests") is True

    def test_tdd_tag_with_project_tag(self):
        """extract_tdd_tag should work alongside [project:name] tag."""
        assert extract_tdd_tag("- [tdd] [project:koan] Fix bug") is True
        assert extract_tdd_tag("- [project:koan] [tdd] Fix bug") is True

    def test_tdd_tag_does_not_match_partial(self):
        """extract_tdd_tag should not match partial words like [tddd]."""
        assert extract_tdd_tag("- [tddd] Not a match") is False

    def test_tdd_tag_in_multiline_block(self):
        """extract_tdd_tag should detect tag in multi-line mission text."""
        block = "- [tdd] Build feature\n  with detailed spec"
        assert extract_tdd_tag(block) is True

    def test_group_by_project_with_subheaders(self):
        """group_by_project should correctly assign missions under ### sub-headers."""
        content = (
            "## Pending\n\n"
            "### project:koan\n"
            "- Kōan task 1\n"
            "- Kōan task 2\n\n"
            "### project:anantys\n"
            "- Anantys task\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = group_by_project(content)
        assert "koan" in result
        assert len(result["koan"]["pending"]) >= 1
        assert "anantys" in result
        assert len(result["anantys"]["pending"]) >= 1


# --- normalize_content ---

class TestNormalizeContent:
    def test_collapses_consecutive_blank_lines(self):
        content = "# Missions\n\n## Pending\n\n\n\n\n- Task\n\n## In Progress\n"
        result = normalize_content(content)
        assert "\n\n\n" not in result
        assert "- Task" in result

    def test_preserves_single_blank_lines(self):
        content = "# Missions\n\n## Pending\n\n- Task 1\n\n- Task 2\n"
        result = normalize_content(content)
        assert result == content

    def test_many_blank_lines_in_pending(self):
        """Real-world case: 40+ blank lines accumulated in pending section."""
        content = "# Missions\n\n## Pending\n" + "\n" * 40 + "- Fix bug\n\n## In Progress\n"
        result = normalize_content(content)
        lines = result.splitlines()
        # Should have at most 1 blank line between header and item
        header_idx = lines.index("## Pending")
        item_idx = next(i for i, l in enumerate(lines) if l.startswith("- Fix"))
        assert item_idx - header_idx <= 2  # header, blank, item

    def test_empty_content(self):
        assert normalize_content("") == ""

    def test_only_blank_lines(self):
        assert normalize_content("\n\n\n\n") == ""

    def test_no_trailing_blank_lines(self):
        content = "# Missions\n\n## Pending\n\n- Task\n\n\n\n"
        result = normalize_content(content)
        assert result.endswith("- Task\n")

    def test_preserves_content_between_items(self):
        content = (
            "## Done\n\n"
            "- Done 1\n\n"
            "- Done 2\n\n"
            "- Done 3\n"
        )
        result = normalize_content(content)
        assert result.count("- Done") == 3
        # Single blank line between items preserved
        assert "- Done 1\n\n- Done 2" in result

    def test_multiple_sections_all_cleaned(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n\n\n\n"
            "- Pending task\n\n\n"
            "## In Progress\n\n\n\n"
            "## Done\n\n\n"
            "- Done task\n"
        )
        result = normalize_content(content)
        # No triple newlines anywhere
        assert "\n\n\n" not in result
        assert "- Pending task" in result
        assert "- Done task" in result

    def test_preserves_indentation(self):
        content = "## Pending\n\n- Task\n  sub-detail\n  more detail\n"
        result = normalize_content(content)
        assert "  sub-detail" in result
        assert "  more detail" in result

    def test_insert_mission_returns_normalized(self):
        """insert_mission should return normalized content (no excessive blanks)."""
        content = "# Missions\n\n## Pending\n" + "\n" * 20 + "- Old task\n\n## In Progress\n"
        result = insert_mission(content, "- New task")
        assert "\n\n\n" not in result
        assert "- New task" in result
        assert "- Old task" in result


# ---------------------------------------------------------------------------
# reorder_mission
# ---------------------------------------------------------------------------

class TestReorderMission:
    SAMPLE = (
        "## Pending\n\n"
        "- first task\n"
        "- second task\n"
        "- third task\n\n"
        "## In Progress\n\n"
        "## Done\n"
    )

    def test_move_to_top(self):
        new_content, moved = reorder_mission(self.SAMPLE, 3, 1)
        assert "third task" in moved
        lines = [l for l in new_content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- third task"
        assert lines[1] == "- first task"
        assert lines[2] == "- second task"

    def test_move_to_position(self):
        new_content, moved = reorder_mission(self.SAMPLE, 3, 2)
        assert "third task" in moved
        lines = [l for l in new_content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- first task"
        assert lines[1] == "- third task"
        assert lines[2] == "- second task"

    def test_move_to_last(self):
        new_content, moved = reorder_mission(self.SAMPLE, 1, 3)
        assert "first task" in moved
        lines = [l for l in new_content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- second task"
        assert lines[1] == "- third task"
        assert lines[2] == "- first task"

    def test_invalid_position_raises(self):
        with pytest.raises(ValueError, match="Invalid position"):
            reorder_mission(self.SAMPLE, 5, 1)

    def test_zero_position_raises(self):
        with pytest.raises(ValueError, match="Invalid position"):
            reorder_mission(self.SAMPLE, 0, 1)

    def test_invalid_target_raises(self):
        with pytest.raises(ValueError, match="Invalid target"):
            reorder_mission(self.SAMPLE, 1, 5)

    def test_same_position_raises(self):
        with pytest.raises(ValueError, match="already at"):
            reorder_mission(self.SAMPLE, 2, 2)

    def test_no_pending_raises(self):
        content = "## Pending\n\n## In Progress\n\n## Done\n"
        with pytest.raises(ValueError, match="No pending"):
            reorder_mission(content, 1, 1)

    def test_no_pending_section_raises(self):
        content = "## In Progress\n\n- working\n\n## Done\n"
        with pytest.raises(ValueError, match="No pending section"):
            reorder_mission(content, 1, 1)

    def test_preserves_project_tags(self):
        content = (
            "## Pending\n\n"
            "- [project:koan] first\n"
            "- [project:web] second\n"
            "- third\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        new_content, moved = reorder_mission(content, 2, 1)
        assert "second" in moved
        assert "[project:web]" in new_content

    def test_multiline_mission_moves_intact(self):
        content = (
            "## Pending\n\n"
            "- first task\n"
            "- second task\n"
            "  with continuation\n"
            "- third task\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        new_content, moved = reorder_mission(content, 2, 1)
        assert "second task" in moved
        lines = new_content.splitlines()
        # The continuation line should follow the moved item
        idx = lines.index("- second task")
        assert lines[idx + 1] == "  with continuation"

    def test_english_section_headers(self):
        content = (
            "## Pending\n\n"
            "- alpha\n"
            "- beta\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        new_content, moved = reorder_mission(content, 2, 1)
        assert "beta" in moved
        lines = [l for l in new_content.splitlines() if l.startswith("- ")]
        assert lines[0] == "- beta"
        assert lines[1] == "- alpha"

    def test_display_uses_clean_format(self):
        content = (
            "## Pending\n\n"
            "- [project:koan] fix the parser bug\n"
            "- simple task\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        _, moved = reorder_mission(content, 1, 2)
        # clean_mission_display should convert [project:koan] to [koan]
        assert "[koan]" in moved
        assert "[project:koan]" not in moved


# ---------------------------------------------------------------------------
# edit_pending_mission
# ---------------------------------------------------------------------------

class TestEditPendingMission:
    SAMPLE = (
        "## Pending\n\n"
        "- first task ⏳(2026-03-13T10:00)\n"
        "- [project:koan] second task\n"
        "- third task\n\n"
        "## In Progress\n\n"
        "## Done\n"
    )

    def test_edit_simple(self):
        from app.missions import edit_pending_mission
        new_content, display = edit_pending_mission(self.SAMPLE, 3, "updated task")
        assert "- updated task" in new_content
        assert "third task" not in new_content

    def test_edit_preserves_timestamp(self):
        from app.missions import edit_pending_mission
        new_content, _ = edit_pending_mission(self.SAMPLE, 1, "renamed first")
        assert "⏳(2026-03-13T10:00)" in new_content
        assert "- renamed first" in new_content

    def test_edit_strips_leading_dash(self):
        from app.missions import edit_pending_mission
        new_content, _ = edit_pending_mission(self.SAMPLE, 3, "- new text")
        assert "- new text" in new_content
        # Should not have double "- - "
        assert "- - " not in new_content

    def test_edit_invalid_position(self):
        from app.missions import edit_pending_mission
        with pytest.raises(ValueError, match="Invalid position"):
            edit_pending_mission(self.SAMPLE, 5, "text")

    def test_edit_zero_position(self):
        from app.missions import edit_pending_mission
        with pytest.raises(ValueError, match="Invalid position"):
            edit_pending_mission(self.SAMPLE, 0, "text")

    def test_edit_empty_text(self):
        from app.missions import edit_pending_mission
        with pytest.raises(ValueError, match="empty"):
            edit_pending_mission(self.SAMPLE, 1, "")

    def test_edit_whitespace_only_text(self):
        from app.missions import edit_pending_mission
        with pytest.raises(ValueError, match="empty"):
            edit_pending_mission(self.SAMPLE, 1, "   ")

    def test_edit_no_pending_section(self):
        from app.missions import edit_pending_mission
        content = "## In Progress\n\n- working\n\n## Done\n"
        with pytest.raises(ValueError, match="No pending section"):
            edit_pending_mission(content, 1, "text")

    def test_edit_empty_pending(self):
        from app.missions import edit_pending_mission
        content = "## Pending\n\n## In Progress\n\n## Done\n"
        with pytest.raises(ValueError, match="No pending missions"):
            edit_pending_mission(content, 1, "text")

    def test_edit_multiline_replaces_all(self):
        """Editing a multi-line mission replaces all lines with the new single line."""
        from app.missions import edit_pending_mission
        content = (
            "## Pending\n\n"
            "- multi task\n"
            "  continuation line\n"
            "- other task\n\n"
            "## Done\n"
        )
        new_content, _ = edit_pending_mission(content, 1, "simple replacement")
        assert "continuation line" not in new_content
        assert "- simple replacement" in new_content
        assert "- other task" in new_content


# ---------------------------------------------------------------------------
# extract_now_flag
# ---------------------------------------------------------------------------

class TestExtractNowFlag:
    def test_no_flag(self):
        urgent, text = extract_now_flag("fix the login bug")
        assert urgent is False
        assert text == "fix the login bug"

    def test_flag_at_start(self):
        urgent, text = extract_now_flag("--now fix the login bug")
        assert urgent is True
        assert text == "fix the login bug"

    def test_flag_in_first_five_words(self):
        urgent, text = extract_now_flag("fix the --now login bug")
        assert urgent is True
        assert text == "fix the login bug"

    def test_flag_at_position_five(self):
        urgent, text = extract_now_flag("one two three four --now rest")
        assert urgent is True
        assert text == "one two three four rest"

    def test_flag_beyond_first_five_words(self):
        urgent, text = extract_now_flag("one two three four five --now six")
        assert urgent is False
        assert text == "one two three four five --now six"

    def test_flag_with_project_tag(self):
        urgent, text = extract_now_flag("--now [project:koan] fix auth")
        assert urgent is True
        assert text == "[project:koan] fix auth"

    def test_empty_text(self):
        urgent, text = extract_now_flag("")
        assert urgent is False
        assert text == ""

    def test_only_flag(self):
        urgent, text = extract_now_flag("--now")
        assert urgent is True
        assert text == ""

    def test_flag_case_sensitive(self):
        urgent, text = extract_now_flag("--NOW fix bug")
        assert urgent is False
        assert text == "--NOW fix bug"

    def test_flag_not_partial_match(self):
        urgent, text = extract_now_flag("--nowhere fix bug")
        assert urgent is False
        assert text == "--nowhere fix bug"

    def test_em_dash_variant_at_start(self):
        """Mobile keyboards auto-correct -- to em dash (\u2014)."""
        urgent, text = extract_now_flag("\u2014now fix the login bug")
        assert urgent is True
        assert text == "fix the login bug"

    def test_em_dash_variant_in_middle(self):
        urgent, text = extract_now_flag("fix \u2014now the login bug")
        assert urgent is True
        assert text == "fix the login bug"

    def test_en_dash_variant_at_start(self):
        """En dash (\u2013) variant also works."""
        urgent, text = extract_now_flag("\u2013now fix the login bug")
        assert urgent is True
        assert text == "fix the login bug"

    def test_em_dash_variant_with_project_tag(self):
        urgent, text = extract_now_flag("\u2014now [project:koan] fix auth")
        assert urgent is True
        assert text == "[project:koan] fix auth"

    def test_em_dash_variant_stripped_from_text(self):
        urgent, text = extract_now_flag("\u2014now deploy hotfix")
        assert urgent is True
        assert "\u2014now" not in text
        assert "--now" not in text
        assert text == "deploy hotfix"

    def test_em_dash_only_flag(self):
        urgent, text = extract_now_flag("\u2014now")
        assert urgent is True
        assert text == ""


# ---------------------------------------------------------------------------
# insert_mission — queue ordering
# ---------------------------------------------------------------------------

class TestInsertMissionOrdering:
    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- existing task one\n"
        "- existing task two\n\n"
        "## In Progress\n\n"
        "## Done\n"
    )

    def test_default_inserts_at_bottom(self):
        result = insert_mission(self.CONTENT, "- new task")
        lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert lines[0] == "- existing task one"
        assert lines[1] == "- existing task two"
        assert lines[2].startswith("- new task")

    def test_urgent_inserts_at_top(self):
        result = insert_mission(self.CONTENT, "- urgent task", urgent=True)
        lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- urgent task")
        assert lines[1] == "- existing task one"
        assert lines[2] == "- existing task two"

    def test_multiple_bottom_inserts_preserve_order(self):
        result = insert_mission(self.CONTENT, "- third task")
        result = insert_mission(result, "- fourth task")
        lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert lines[0] == "- existing task one"
        assert lines[1] == "- existing task two"
        assert lines[2].startswith("- third task")
        assert lines[3].startswith("- fourth task")

    def test_multiple_urgent_inserts(self):
        result = insert_mission(self.CONTENT, "- urgent A", urgent=True)
        result = insert_mission(result, "- urgent B", urgent=True)
        lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert lines[0].startswith("- urgent B")
        assert lines[1].startswith("- urgent A")

    def test_bottom_insert_into_empty_section(self):
        content = "# Missions\n\n## Pending\n\n## In Progress\n"
        result = insert_mission(content, "- first task")
        assert "- first task" in result
        assert result.index("- first task") < result.index("## In Progress")

    def test_bottom_insert_with_french_headers(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- tache existante\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = insert_mission(content, "- nouvelle tache")
        lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert lines[-1].startswith("- nouvelle tache")

    def test_bottom_insert_with_multiline_mission(self):
        content = (
            "## Pending\n\n"
            "- task one\n"
            "  with details\n"
            "- task two\n\n"
            "## In Progress\n"
        )
        result = insert_mission(content, "- task three")
        lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert lines[-1].startswith("- task three")
        # task three should come after "task two" not after "with details"
        assert result.index("- task three") > result.index("  with details")

    def test_urgent_preserves_existing_order(self):
        """Urgent adds to top but existing order is preserved."""
        result = insert_mission(self.CONTENT, "- urgent!", urgent=True)
        existing = [l for l in result.splitlines() if l.startswith("- ")]
        assert existing.index("- existing task one") < existing.index("- existing task two")


# ---------------------------------------------------------------------------
# complete_mission
# ---------------------------------------------------------------------------

class TestCompleteMission:
    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- /plan Add dark mode\n"
        "- Fix the login bug\n\n"
        "## In Progress\n\n"
        "## Done\n\n"
        "- Old completed task\n"
    )

    def test_moves_mission_to_done(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "/plan Add dark mode" not in "\n".join(sections["pending"])
        # Should appear in Done
        done_text = "\n".join(sections["done"])
        assert "/plan Add dark mode" in done_text

    def test_done_entry_has_timestamp(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        # Timestamp format: YYYY-MM-DD HH:MM
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", done_text)

    def test_done_entry_has_checkmark(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert "✅" in done_text

    def test_nonexistent_mission_unchanged(self):
        result = complete_mission(self.CONTENT, "/nonexistent thing")
        assert result == normalize_content(self.CONTENT)

    def test_empty_pending_unchanged(self):
        content = "# Missions\n\n## Pending\n\n## Done\n"
        result = complete_mission(content, "/plan something")
        assert result == normalize_content(content)

    def test_remaining_pending_preserved(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert "- Fix the login bug" in sections["pending"]

    def test_existing_done_preserved(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert "Old completed task" in done_text

    def test_no_done_section_creates_one(self):
        content = "# Missions\n\n## Pending\n\n- /plan Test\n\n## In Progress\n"
        result = complete_mission(content, "/plan Test")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 0
        assert len(sections["done"]) == 1
        assert "/plan Test" in sections["done"][0]

    def test_project_tagged_mission(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] /plan Add dark mode\n\n"
            "## Done\n"
        )
        result = complete_mission(content, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 0
        assert len(sections["done"]) == 1


# ---------------------------------------------------------------------------
# fail_mission
# ---------------------------------------------------------------------------

class TestFailMission:
    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- /plan Add dark mode\n"
        "- Fix the login bug\n\n"
        "## In Progress\n\n"
        "## Done\n\n"
        "- Old completed task\n"
    )

    def test_moves_mission_to_failed(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "/plan Add dark mode" not in "\n".join(sections["pending"])
        failed_text = "\n".join(sections["failed"])
        assert "/plan Add dark mode" in failed_text

    def test_failed_entry_has_cross_mark(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        failed_text = "\n".join(sections["failed"])
        assert "\u274c" in failed_text

    def test_failed_entry_has_timestamp(self):
        import re
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        failed_text = "\n".join(sections["failed"])
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", failed_text)

    def test_creates_failed_section_if_missing(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- /plan Test\n\n"
            "## In Progress\n"
        )
        result = fail_mission(content, "/plan Test")
        assert "## Failed" in result
        sections = parse_sections(result)
        assert len(sections["pending"]) == 0
        assert len(sections["failed"]) == 1

    def test_nonexistent_mission_unchanged(self):
        result = fail_mission(self.CONTENT, "/nonexistent thing")
        assert result == normalize_content(self.CONTENT)

    def test_empty_pending_unchanged(self):
        content = "# Missions\n\n## Pending\n\n## Done\n"
        result = fail_mission(content, "/plan something")
        assert result == normalize_content(content)

    def test_remaining_pending_preserved(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert "- Fix the login bug" in sections["pending"]

    def test_existing_done_preserved(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert "Old completed task" in done_text

    def test_existing_failed_section_preserved(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New task\n\n"
            "## In Progress\n\n"
            "## Done\n\n"
            "## Failed\n\n"
            "- Old failed task\n"
        )
        result = fail_mission(content, "New task")
        sections = parse_sections(result)
        failed_text = "\n".join(sections["failed"])
        assert "Old failed task" in failed_text
        assert "New task" in failed_text

    def test_project_tagged_mission(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] /plan Add dark mode\n\n"
            "## Done\n\n"
            "## Failed\n"
        )
        result = fail_mission(content, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 0
        assert len(sections["failed"]) == 1


# ---------------------------------------------------------------------------
# requeue_mission — move from In Progress back to Pending
# ---------------------------------------------------------------------------

class TestRequeueMission:
    def test_basic_requeue(self):
        content = (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix login bug\n"
        )
        result = requeue_mission(content, "Fix login bug")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert len(sections["pending"]) == 1
        assert "Fix login bug" in sections["pending"][0]

    def test_requeue_strips_started_timestamp(self):
        content = (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Fix login bug ▶(2026-03-26T22:00)\n"
        )
        result = requeue_mission(content, "Fix login bug")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "▶" not in sections["pending"][0]
        assert "Fix login bug" in sections["pending"][0]

    def test_requeue_preserves_project_tag(self):
        content = (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:koan] Fix login bug ▶(2026-03-26T22:00)\n"
        )
        result = requeue_mission(content, "Fix login bug")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "[project:koan]" in sections["pending"][0]

    def test_requeue_not_found_returns_unchanged(self):
        content = (
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Some other mission\n"
        )
        result = requeue_mission(content, "Fix login bug")
        assert result == normalize_content(content)

    def test_requeue_with_existing_pending(self):
        content = (
            "## Pending\n\n"
            "- Existing task\n\n"
            "## In Progress\n\n"
            "- Fix login bug ▶(2026-03-26T22:00)\n"
        )
        result = requeue_mission(content, "Fix login bug")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 2
        assert len(sections["in_progress"]) == 0


# ---------------------------------------------------------------------------
# parse_sections — failed section
# ---------------------------------------------------------------------------

class TestParseSectionsFailed:
    def test_includes_failed_key(self):
        result = parse_sections("## Pending\n\n## Failed\n\n- Bad task\n")
        assert "failed" in result
        assert len(result["failed"]) == 1

    def test_classify_section_failed(self):
        assert classify_section("Failed") == "failed"

    def test_default_skeleton_has_failed(self):
        assert "## Failed" in DEFAULT_SKELETON


# ---------------------------------------------------------------------------
# complete_mission / fail_mission — sub-header edge cases (Bug #1 fix)
# ---------------------------------------------------------------------------

class TestCompleteMissionWithSubHeaders:
    """Verify text-based removal handles ### project:X sub-headers correctly."""

    CONTENT_WITH_SUBHEADERS = (
        "# Missions\n\n"
        "## Pending\n\n"
        "### project:alpha\n"
        "- Fix auth in alpha\n"
        "- Add tests for alpha\n\n"
        "### project:beta\n"
        "- Fix auth in beta\n\n"
        "## Done\n\n"
        "- Old task ✅ (2025-01-01 12:00)\n"
    )

    def test_complete_first_subheader_mission(self):
        result = complete_mission(self.CONTENT_WITH_SUBHEADERS, "Fix auth in alpha")
        sections = parse_sections(result)
        pending_text = "\n".join(sections["pending"])
        assert "Fix auth in alpha" not in pending_text
        done_text = "\n".join(sections["done"])
        assert "Fix auth in alpha" in done_text

    def test_complete_second_subheader_mission(self):
        result = complete_mission(self.CONTENT_WITH_SUBHEADERS, "Fix auth in beta")
        sections = parse_sections(result)
        pending_text = "\n".join(sections["pending"])
        assert "Fix auth in beta" not in pending_text
        done_text = "\n".join(sections["done"])
        assert "Fix auth in beta" in done_text

    def test_remaining_missions_preserved_after_subheader_complete(self):
        result = complete_mission(self.CONTENT_WITH_SUBHEADERS, "Fix auth in alpha")
        sections = parse_sections(result)
        pending_text = "\n".join(sections["pending"])
        assert "Add tests for alpha" in pending_text
        assert "Fix auth in beta" in pending_text

    def test_fail_with_subheaders(self):
        result = fail_mission(self.CONTENT_WITH_SUBHEADERS, "Fix auth in beta")
        sections = parse_sections(result)
        pending_text = "\n".join(sections["pending"])
        assert "Fix auth in beta" not in pending_text
        failed_text = "\n".join(sections["failed"])
        assert "Fix auth in beta" in failed_text

    def test_complete_with_multiple_subheaders_all_preserved(self):
        """Completing one mission doesn't disrupt other sub-header groups."""
        result = complete_mission(self.CONTENT_WITH_SUBHEADERS, "Add tests for alpha")
        sections = parse_sections(result)
        pending_text = "\n".join(sections["pending"])
        assert "Fix auth in alpha" in pending_text
        assert "Fix auth in beta" in pending_text
        done_text = "\n".join(sections["done"])
        assert "Add tests for alpha" in done_text


# ---------------------------------------------------------------------------
# start_mission — move from Pending to In Progress
# ---------------------------------------------------------------------------

class TestStartMission:
    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- /plan Add dark mode\n"
        "- Fix the login bug\n\n"
        "## In Progress\n\n"
        "## Done\n\n"
        "- Old completed task\n"
    )

    def test_moves_to_in_progress(self):
        result = start_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "/plan Add dark mode" not in "\n".join(sections["pending"])
        in_progress = "\n".join(sections["in_progress"])
        assert "/plan Add dark mode" in in_progress

    def test_remaining_pending_preserved(self):
        result = start_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert "- Fix the login bug" in sections["pending"]

    def test_nonexistent_mission_unchanged(self):
        result = start_mission(self.CONTENT, "/nonexistent thing")
        assert result == normalize_content(self.CONTENT)

    def test_with_project_tag(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] /plan Add dark mode\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = start_mission(content, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 0
        assert len(sections["in_progress"]) == 1
        assert "/plan Add dark mode" in sections["in_progress"][0]

    def test_creates_in_progress_section_if_missing(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- /plan Test\n\n"
            "## Done\n"
        )
        result = start_mission(content, "/plan Test")
        assert "## In Progress" in result
        sections = parse_sections(result)
        assert len(sections["pending"]) == 0
        assert len(sections["in_progress"]) == 1

    def test_no_timestamp_added(self):
        """start_mission should NOT add a timestamp (unlike complete/fail)."""
        import re
        result = start_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        in_progress_text = "\n".join(sections["in_progress"])
        # Should not have a YYYY-MM-DD HH:MM timestamp
        assert not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", in_progress_text)

    def test_existing_in_progress_flushed_to_done(self):
        """Sanity enforcement: existing In Progress missions move to Done."""
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New task\n\n"
            "## In Progress\n\n"
            "- Already running task\n\n"
            "## Done\n"
        )
        result = start_mission(content, "New task")
        sections = parse_sections(result)
        # Only the new task should be In Progress
        assert len(sections["in_progress"]) == 1
        assert "New task" in sections["in_progress"][0]
        # The old task should have been moved to Done
        done_text = "\n".join(sections["done"])
        assert "Already running task" in done_text
        assert "\u2705" in done_text


# ---------------------------------------------------------------------------
# complete_mission / fail_mission — from In Progress (Bug fix)
# ---------------------------------------------------------------------------

class TestCompleteMissionFromInProgress:
    """Bug: complete_mission only searched Pending, not In Progress.

    When Claude moves a mission to In Progress before execution, _finalize_mission
    couldn't find it for completion because _remove_pending_by_text only searched
    the Pending section. These tests verify the fix.
    """

    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- Another pending task\n\n"
        "## In Progress\n\n"
        "- /plan Add dark mode\n\n"
        "## Done\n\n"
        "- Old completed task\n"
    )

    def test_completes_from_in_progress(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        done_text = "\n".join(sections["done"])
        assert "/plan Add dark mode" in done_text

    def test_done_entry_has_timestamp(self):
        import re
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", done_text)

    def test_done_entry_has_checkmark(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert "\u2705" in done_text

    def test_pending_section_untouched(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "Another pending task" in sections["pending"][0]

    def test_with_project_tag_in_progress(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:koan] /plan Add dark mode\n\n"
            "## Done\n"
        )
        result = complete_mission(content, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert len(sections["done"]) == 1

    def test_prefers_pending_over_in_progress(self):
        """If mission exists in BOTH sections (edge case), prefer Pending."""
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- /plan Add dark mode\n\n"
            "## In Progress\n\n"
            "- /plan Add dark mode\n\n"
            "## Done\n"
        )
        result = complete_mission(content, "/plan Add dark mode")
        sections = parse_sections(result)
        # Should remove from Pending first
        assert len(sections["pending"]) == 0
        assert len(sections["in_progress"]) == 1
        assert len(sections["done"]) == 1


class TestFailMissionFromInProgress:
    """Same bug as complete_mission — fail_mission should also search In Progress."""

    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- Another pending task\n\n"
        "## In Progress\n\n"
        "- /plan Add dark mode\n\n"
        "## Done\n\n"
        "## Failed\n"
    )

    def test_fails_from_in_progress(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        failed_text = "\n".join(sections["failed"])
        assert "/plan Add dark mode" in failed_text

    def test_failed_entry_has_cross_mark(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        failed_text = "\n".join(sections["failed"])
        assert "\u274c" in failed_text

    def test_pending_section_untouched(self):
        result = fail_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "Another pending task" in sections["pending"][0]


# ---------------------------------------------------------------------------
# Full lifecycle: Pending -> In Progress -> Done
# ---------------------------------------------------------------------------

class TestMissionLifecycle:
    """End-to-end tests for the full mission lifecycle."""

    INITIAL = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- [project:koan] /plan Add dark mode\n"
        "- [project:koan] Fix the login bug\n\n"
        "## In Progress\n\n"
        "## Done\n\n"
        "## Failed\n"
    )

    def test_full_lifecycle_pending_to_in_progress_to_done(self):
        # Step 1: Start mission (Pending -> In Progress)
        after_start = start_mission(self.INITIAL, "/plan Add dark mode")
        sections = parse_sections(after_start)
        assert len(sections["pending"]) == 1
        assert len(sections["in_progress"]) == 1
        assert "/plan Add dark mode" in sections["in_progress"][0]

        # Step 2: Complete mission (In Progress -> Done)
        after_done = complete_mission(after_start, "/plan Add dark mode")
        sections = parse_sections(after_done)
        assert len(sections["pending"]) == 1
        assert len(sections["in_progress"]) == 0
        assert len(sections["done"]) == 1
        assert "/plan Add dark mode" in sections["done"][0]

    def test_full_lifecycle_pending_to_in_progress_to_failed(self):
        after_start = start_mission(self.INITIAL, "/plan Add dark mode")
        after_fail = fail_mission(after_start, "/plan Add dark mode")
        sections = parse_sections(after_fail)
        assert len(sections["pending"]) == 1
        assert len(sections["in_progress"]) == 0
        assert len(sections["failed"]) == 1
        assert "/plan Add dark mode" in sections["failed"][0]

    def test_direct_pending_to_done_still_works(self):
        """Skill dispatch skips In Progress — direct Pending -> Done."""
        result = complete_mission(self.INITIAL, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert len(sections["done"]) == 1
        assert "/plan Add dark mode" in sections["done"][0]

    def test_multiple_missions_lifecycle(self):
        """Two missions, one started, one still pending."""
        after_start = start_mission(self.INITIAL, "/plan Add dark mode")
        after_done = complete_mission(after_start, "/plan Add dark mode")
        # Second mission still in Pending
        sections = parse_sections(after_done)
        assert "Fix the login bug" in sections["pending"][0]
        # Start and complete second mission
        after_start2 = start_mission(after_done, "Fix the login bug")
        after_done2 = complete_mission(after_start2, "Fix the login bug")
        sections = parse_sections(after_done2)
        assert len(sections["pending"]) == 0
        assert len(sections["in_progress"]) == 0
        assert len(sections["done"]) == 2


# ---------------------------------------------------------------------------
# Project tag preservation in Done/Failed entries
# ---------------------------------------------------------------------------

class TestProjectTagPreservation:
    """Done/Failed entries should preserve [project:X] tags from original text."""

    CONTENT = (
        "# Missions\n\n"
        "## Pending\n\n"
        "- [project:koan] /plan Add dark mode\n"
        "- [project:backend] Fix the login bug\n\n"
        "## In Progress\n\n"
        "## Done\n\n"
        "## Failed\n"
    )

    def test_complete_preserves_project_tag_from_pending(self):
        result = complete_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(result)
        done_entry = sections["done"][0]
        assert "[project:koan]" in done_entry

    def test_complete_preserves_project_tag_from_in_progress(self):
        after_start = start_mission(self.CONTENT, "/plan Add dark mode")
        result = complete_mission(after_start, "/plan Add dark mode")
        sections = parse_sections(result)
        done_entry = sections["done"][0]
        assert "[project:koan]" in done_entry

    def test_fail_preserves_project_tag_from_in_progress(self):
        after_start = start_mission(self.CONTENT, "Fix the login bug")
        result = fail_mission(after_start, "Fix the login bug")
        sections = parse_sections(result)
        failed_entry = sections["failed"][0]
        assert "[project:backend]" in failed_entry

    def test_complete_without_project_tag_works(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- /plan Add dark mode\n\n"
            "## Done\n"
        )
        result = complete_mission(content, "/plan Add dark mode")
        sections = parse_sections(result)
        assert len(sections["done"]) == 1
        assert "/plan Add dark mode" in sections["done"][0]

    def test_full_lifecycle_preserves_tag(self):
        """End-to-end: Pending -> In Progress -> Done preserves project tag."""
        after_start = start_mission(self.CONTENT, "/plan Add dark mode")
        sections = parse_sections(after_start)
        # In Progress entry preserves tag
        assert "[project:koan]" in sections["in_progress"][0]

        after_done = complete_mission(after_start, "/plan Add dark mode")
        sections = parse_sections(after_done)
        # Done entry preserves tag
        done_entry = sections["done"][0]
        assert "[project:koan]" in done_entry
        assert "/plan Add dark mode" in done_entry
        assert "\u2705" in done_entry

    def test_skill_dispatch_lifecycle_preserves_tag(self):
        """Simulate exact skill dispatch flow: mission queued with tag,
        picked up (tag stripped from needle), started, completed."""
        # Handler queues: "- [project:koan] /plan Fix auth module"
        # pick_mission returns: "koan:/plan Fix auth module"
        # mission_title becomes: "/plan Fix auth module" (tag stripped)
        mission_title = "/plan Fix auth module"
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] /plan Fix auth module\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )

        # Step 1: start_mission with stripped title
        after_start = start_mission(content, mission_title)
        sections = parse_sections(after_start)
        assert len(sections["in_progress"]) == 1
        assert "[project:koan]" in sections["in_progress"][0]

        # Step 2: complete_mission with stripped title
        after_done = complete_mission(after_start, mission_title)
        sections = parse_sections(after_done)
        assert len(sections["in_progress"]) == 0
        assert len(sections["done"]) == 1
        # Project tag must be preserved
        assert "[project:koan]" in sections["done"][0]


# ---------------------------------------------------------------------------
# modify_missions_file return value
# ---------------------------------------------------------------------------

class TestModifyMissionsFileReturn:
    """modify_missions_file must return transformed content for diagnostics."""

    def test_returns_transformed_content(self, tmp_path):
        missions_path = tmp_path / "missions.md"
        missions_path.write_text(
            "# Missions\n\n## Pending\n- test mission\n\n## Done\n"
        )
        from app.utils import modify_missions_file

        result = modify_missions_file(
            missions_path,
            lambda c: complete_mission(c, "test mission"),
        )
        assert result is not None
        assert "## Done" in result
        assert "test mission" in result
        assert "\u2705" in result

    def test_returns_unchanged_on_noop(self, tmp_path):
        missions_path = tmp_path / "missions.md"
        original = "# Missions\n\n## Pending\n\n## Done\n"
        missions_path.write_text(original)
        from app.utils import modify_missions_file

        result = modify_missions_file(missions_path, lambda c: c)
        assert result == original


# ---------------------------------------------------------------------------
# _flush_in_progress_to_done — sanity enforcement
# ---------------------------------------------------------------------------

class TestFlushInProgressToDone:
    """Tests for _flush_in_progress_to_done() — ensures only one mission
    can be In Progress at a time."""

    def test_empty_in_progress_noop(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Task\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = _flush_in_progress_to_done(content)
        assert result == normalize_content(content)

    def test_single_in_progress_moved_to_done(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Stale mission\n\n"
            "## Done\n"
        )
        result = _flush_in_progress_to_done(content)
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        done_text = "\n".join(sections["done"])
        assert "Stale mission" in done_text
        assert "\u2705" in done_text

    def test_multiple_in_progress_all_moved(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- First stale\n"
            "- Second stale\n"
            "- Third stale\n\n"
            "## Done\n"
        )
        result = _flush_in_progress_to_done(content)
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert len(sections["done"]) == 3
        done_text = "\n".join(sections["done"])
        assert "First stale" in done_text
        assert "Second stale" in done_text
        assert "Third stale" in done_text

    def test_preserves_project_tags(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:koan] Stale koan mission\n\n"
            "## Done\n"
        )
        result = _flush_in_progress_to_done(content)
        sections = parse_sections(result)
        done_entry = sections["done"][0]
        assert "[project:koan]" in done_entry
        assert "Stale koan mission" in done_entry

    def test_preserves_existing_done(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Stale\n\n"
            "## Done\n\n"
            "- Old done task\n"
        )
        result = _flush_in_progress_to_done(content)
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert "Old done task" in done_text
        assert "Stale" in done_text

    def test_creates_done_section_if_missing(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Stale mission\n"
        )
        result = _flush_in_progress_to_done(content)
        assert "## Done" in result
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 0
        assert len(sections["done"]) == 1

    def test_timestamp_added_to_flushed_missions(self):
        import re
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Stale mission\n\n"
            "## Done\n"
        )
        result = _flush_in_progress_to_done(content)
        sections = parse_sections(result)
        done_entry = sections["done"][0]
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", done_entry)


# ---------------------------------------------------------------------------
# start_mission — sanity enforcement (In Progress cleanup)
# ---------------------------------------------------------------------------

class TestStartMissionSanityEnforcement:
    """Tests that start_mission() flushes stale In Progress missions to Done."""

    def test_single_stale_in_progress_flushed(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New mission\n\n"
            "## In Progress\n\n"
            "- Old stale mission\n\n"
            "## Done\n"
        )
        result = start_mission(content, "New mission")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 1
        assert "New mission" in sections["in_progress"][0]
        done_text = "\n".join(sections["done"])
        assert "Old stale mission" in done_text
        assert "\u2705" in done_text

    def test_multiple_stale_in_progress_all_flushed(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Fresh task\n\n"
            "## In Progress\n\n"
            "- Stale A\n"
            "- Stale B\n"
            "- Stale C\n\n"
            "## Done\n"
        )
        result = start_mission(content, "Fresh task")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 1
        assert "Fresh task" in sections["in_progress"][0]
        assert len(sections["done"]) == 3
        done_text = "\n".join(sections["done"])
        assert "Stale A" in done_text
        assert "Stale B" in done_text
        assert "Stale C" in done_text

    def test_no_stale_in_progress_still_works(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New task\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = start_mission(content, "New task")
        sections = parse_sections(result)
        assert len(sections["in_progress"]) == 1
        assert "New task" in sections["in_progress"][0]
        assert len(sections["done"]) == 0

    def test_stale_mission_preserves_project_tag_in_done(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New task\n\n"
            "## In Progress\n\n"
            "- [project:backend] Old backend work\n\n"
            "## Done\n"
        )
        result = start_mission(content, "New task")
        sections = parse_sections(result)
        done_entry = sections["done"][0]
        assert "[project:backend]" in done_entry
        assert "Old backend work" in done_entry

    def test_full_lifecycle_with_sanity_enforcement(self):
        """Simulate: mission A starts, then mission B starts without A finishing."""
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Mission A\n"
            "- Mission B\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        # Start mission A
        after_a = start_mission(content, "Mission A")
        sections = parse_sections(after_a)
        assert len(sections["in_progress"]) == 1
        assert "Mission A" in sections["in_progress"][0]

        # Start mission B without completing A — A should be flushed to Done
        after_b = start_mission(after_a, "Mission B")
        sections = parse_sections(after_b)
        assert len(sections["in_progress"]) == 1
        assert "Mission B" in sections["in_progress"][0]
        assert len(sections["done"]) == 1
        assert "Mission A" in sections["done"][0]
        assert "\u2705" in sections["done"][0]

    def test_sanity_enforcement_preserves_pending(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Task to start\n"
            "- Remaining task\n\n"
            "## In Progress\n\n"
            "- Stale task\n\n"
            "## Done\n"
        )
        result = start_mission(content, "Task to start")
        sections = parse_sections(result)
        assert len(sections["pending"]) == 1
        assert "Remaining task" in sections["pending"][0]

    def test_sanity_enforcement_preserves_existing_done(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- New task\n\n"
            "## In Progress\n\n"
            "- Stale task\n\n"
            "## Done\n\n"
            "- Previously done task\n"
        )
        result = start_mission(content, "New task")
        sections = parse_sections(result)
        done_text = "\n".join(sections["done"])
        assert "Previously done task" in done_text
        assert "Stale task" in done_text

    def test_nonexistent_mission_skips_sanity_check(self):
        """If mission not found in Pending, no sanity check runs."""
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- Should stay here\n\n"
            "## Done\n"
        )
        result = start_mission(content, "Nonexistent")
        sections = parse_sections(result)
        # In Progress should be untouched since start_mission returned early
        assert len(sections["in_progress"]) == 1
        assert "Should stay here" in sections["in_progress"][0]

    def test_sequential_starts_always_flush(self):
        """Starting 3 missions sequentially: only the last should be in progress."""
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Alpha\n"
            "- Beta\n"
            "- Gamma\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        r1 = start_mission(content, "Alpha")
        r2 = start_mission(r1, "Beta")
        r3 = start_mission(r2, "Gamma")
        sections = parse_sections(r3)
        assert len(sections["pending"]) == 0
        assert len(sections["in_progress"]) == 1
        assert "Gamma" in sections["in_progress"][0]
        assert len(sections["done"]) == 2
        done_text = "\n".join(sections["done"])
        assert "Alpha" in done_text
        assert "Beta" in done_text


# --- stamp_queued / stamp_started ---

class TestStampQueued:
    def test_appends_queued_marker(self):
        result = stamp_queued("Fix the bug")
        assert result.startswith("Fix the bug ⏳(")
        assert "T" in result  # ISO format

    def test_preserves_existing_text(self):
        result = stamp_queued("[project:koan] Do something")
        assert result.startswith("[project:koan] Do something ⏳(")


class TestStampStarted:
    def test_appends_started_marker(self):
        result = stamp_started("Fix the bug")
        assert result.startswith("Fix the bug ▶(")

    def test_preserves_existing_text(self):
        result = stamp_started("Fix it ⏳(2026-02-18T10:00)")
        assert "⏳(2026-02-18T10:00)" in result
        assert "▶(" in result


# --- extract_timestamps ---

class TestExtractTimestamps:
    def test_all_timestamps_present(self):
        text = "Fix bug ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05) ✅ (2026-02-18 10:15)"
        ts = extract_timestamps(text)
        assert ts["queued"] == datetime(2026, 2, 18, 10, 0)
        assert ts["started"] == datetime(2026, 2, 18, 10, 5)
        assert ts["completed"] == datetime(2026, 2, 18, 10, 15)

    def test_only_queued(self):
        ts = extract_timestamps("Fix bug ⏳(2026-02-18T10:00)")
        assert ts["queued"] == datetime(2026, 2, 18, 10, 0)
        assert ts["started"] is None
        assert ts["completed"] is None

    def test_no_timestamps(self):
        ts = extract_timestamps("Fix the bug")
        assert ts["queued"] is None
        assert ts["started"] is None
        assert ts["completed"] is None

    def test_failed_marker(self):
        ts = extract_timestamps("Fix bug ❌ (2026-02-18 10:15)")
        assert ts["completed"] == datetime(2026, 2, 18, 10, 15)

    def test_queued_and_started(self):
        text = "Do thing ⏳(2026-01-15T08:30) ▶(2026-01-15T09:00)"
        ts = extract_timestamps(text)
        assert ts["queued"] == datetime(2026, 1, 15, 8, 30)
        assert ts["started"] == datetime(2026, 1, 15, 9, 0)
        assert ts["completed"] is None


# --- format_duration ---

class TestFormatDuration:
    def test_less_than_minute(self):
        assert format_duration(30) == "< 1m"

    def test_zero(self):
        assert format_duration(0) == "< 1m"

    def test_negative(self):
        assert format_duration(-10) == "< 1m"

    def test_exact_minutes(self):
        assert format_duration(300) == "5m"

    def test_exact_hour(self):
        assert format_duration(3600) == "1h"

    def test_hours_and_minutes(self):
        assert format_duration(5400) == "1h 30m"

    def test_multiple_hours(self):
        assert format_duration(10800) == "3h"

    def test_just_over_hour(self):
        assert format_duration(3660) == "1h 1m"


# --- mission_timing_display ---

class TestMissionTimingDisplay:
    def test_completed_with_start_and_end(self):
        text = "Fix bug ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05) ✅ (2026-02-18 10:35)"
        result = mission_timing_display(text)
        assert "took 30m" in result
        assert "waited 5m" in result

    def test_completed_no_wait(self):
        text = "Fix bug ▶(2026-02-18T10:00) ✅ (2026-02-18 10:30)"
        result = mission_timing_display(text)
        assert result == "took 30m"

    def test_completed_short_wait_included(self):
        # Wait of exactly 1 minute (>= 60s) IS shown
        text = "Fix bug ⏳(2026-02-18T10:04) ▶(2026-02-18T10:05) ✅ (2026-02-18 10:35)"
        result = mission_timing_display(text)
        assert "took 30m" in result
        assert "waited" in result

    def test_in_progress(self):
        # Use a fixed "now" to avoid flaky tests
        text = "Fix bug ▶(2026-02-18T10:00)"
        with patch("app.missions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 18, 10, 15)
            mock_dt.strptime = datetime.strptime
            result = mission_timing_display(text)
        assert result == "running 15m"

    def test_queued_waiting(self):
        text = "Fix bug ⏳(2026-02-18T10:00)"
        with patch("app.missions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 18, 10, 30)
            mock_dt.strptime = datetime.strptime
            result = mission_timing_display(text)
        assert result == "waiting 30m"

    def test_no_timestamps(self):
        assert mission_timing_display("Fix the bug") == ""

    def test_negative_duration_returns_empty(self):
        # Completed before started (clock issue)
        text = "Bug ▶(2026-02-18T11:00) ✅ (2026-02-18 10:00)"
        result = mission_timing_display(text)
        assert result == ""


# --- strip_timestamps ---

class TestStripTimestamps:
    def test_strips_queued_and_started(self):
        text = "Fix bug ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05)"
        result = strip_timestamps(text)
        assert result == "Fix bug"

    def test_preserves_completion_marker(self):
        text = "Fix bug ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05) ✅ (2026-02-18 10:30)"
        result = strip_timestamps(text)
        assert "✅" in result
        assert "⏳" not in result
        assert "▶" not in result

    def test_no_timestamps(self):
        assert strip_timestamps("Fix the bug") == "Fix the bug"


# --- parse_ideas ---

IDEAS_CONTENT = (
    "# Missions\n\n"
    "## Ideas\n"
    "- Build a widget\n"
    "- Improve performance\n"
    "  with better caching\n"
    "- Fix the login page\n\n"
    "## Pending\n\n"
    "- Some task\n\n"
    "## In Progress\n\n"
    "## Done\n"
)


class TestParseIdeas:
    def test_basic_ideas(self):
        ideas = parse_ideas(IDEAS_CONTENT)
        assert len(ideas) == 3
        assert ideas[0] == "- Build a widget"
        assert ideas[2] == "- Fix the login page"

    def test_multiline_idea(self):
        ideas = parse_ideas(IDEAS_CONTENT)
        assert "with better caching" in ideas[1]

    def test_no_ideas_section(self):
        content = "# Missions\n\n## Pending\n\n- Task\n\n## Done\n"
        assert parse_ideas(content) == []

    def test_empty_ideas_section(self):
        content = "# Missions\n\n## Ideas\n\n## Pending\n\n## Done\n"
        assert parse_ideas(content) == []

    def test_ideas_only(self):
        content = "# Missions\n\n## Ideas\n- One idea\n"
        ideas = parse_ideas(content)
        assert len(ideas) == 1
        assert ideas[0] == "- One idea"


# --- insert_idea ---

class TestInsertIdea:
    def test_insert_into_existing_section(self):
        result = insert_idea(IDEAS_CONTENT, "- New idea")
        ideas = parse_ideas(result)
        assert len(ideas) == 4
        assert ideas[-1] == "- New idea"

    def test_insert_creates_section_when_missing(self):
        content = "# Missions\n\n## Pending\n\n## Done\n"
        result = insert_idea(content, "- First idea")
        ideas = parse_ideas(result)
        assert len(ideas) == 1
        assert ideas[0] == "- First idea"

    def test_insert_into_empty_content(self):
        result = insert_idea("", "- An idea")
        ideas = parse_ideas(result)
        assert len(ideas) == 1

    def test_insert_preserves_other_sections(self):
        result = insert_idea(IDEAS_CONTENT, "- New idea")
        sections = parse_sections(result)
        assert "Some task" in sections["pending"][0]


# --- delete_idea ---

class TestDeleteIdea:
    def test_delete_first_idea(self):
        updated, deleted = delete_idea(IDEAS_CONTENT, 1)
        assert deleted == "- Build a widget"
        ideas = parse_ideas(updated)
        assert len(ideas) == 2
        assert "Build a widget" not in "\n".join(ideas)

    def test_delete_last_idea(self):
        updated, deleted = delete_idea(IDEAS_CONTENT, 3)
        assert deleted == "- Fix the login page"
        ideas = parse_ideas(updated)
        assert len(ideas) == 2

    def test_delete_multiline_idea(self):
        updated, deleted = delete_idea(IDEAS_CONTENT, 2)
        assert "Improve performance" in deleted
        ideas = parse_ideas(updated)
        assert len(ideas) == 2
        # Verify the multiline content is gone
        assert all("caching" not in i for i in ideas)

    def test_delete_out_of_range(self):
        updated, deleted = delete_idea(IDEAS_CONTENT, 99)
        assert deleted is None
        assert updated == IDEAS_CONTENT

    def test_delete_zero_index(self):
        updated, deleted = delete_idea(IDEAS_CONTENT, 0)
        assert deleted is None

    def test_delete_preserves_other_sections(self):
        updated, _ = delete_idea(IDEAS_CONTENT, 1)
        sections = parse_sections(updated)
        assert len(sections["pending"]) == 1


# --- promote_idea ---

class TestPromoteIdea:
    def test_promote_moves_to_pending(self):
        updated, promoted = promote_idea(IDEAS_CONTENT, 1)
        assert promoted == "- Build a widget"
        sections = parse_sections(updated)
        # Should be in pending now (urgent = top)
        assert any("Build a widget" in p for p in sections["pending"])
        # Should no longer be in ideas
        ideas = parse_ideas(updated)
        assert all("Build a widget" not in i for i in ideas)

    def test_promote_invalid_index(self):
        updated, promoted = promote_idea(IDEAS_CONTENT, 99)
        assert promoted is None
        assert updated == IDEAS_CONTENT

    def test_promote_last_idea(self):
        updated, promoted = promote_idea(IDEAS_CONTENT, 3)
        assert promoted == "- Fix the login page"
        ideas = parse_ideas(updated)
        assert len(ideas) == 2


# --- promote_all_ideas ---

class TestPromoteAllIdeas:
    def test_promotes_all(self):
        updated, promoted = promote_all_ideas(IDEAS_CONTENT)
        assert len(promoted) == 3
        ideas = parse_ideas(updated)
        assert len(ideas) == 0
        sections = parse_sections(updated)
        # Original pending + 3 promoted
        assert len(sections["pending"]) == 4

    def test_promotes_none_when_empty(self):
        content = "# Missions\n\n## Ideas\n\n## Pending\n\n## Done\n"
        updated, promoted = promote_all_ideas(content)
        assert promoted == []
        assert updated == content

    def test_preserves_original_order(self):
        updated, promoted = promote_all_ideas(IDEAS_CONTENT)
        assert "Build a widget" in promoted[0]
        assert "Fix the login page" in promoted[2]


# --- list_pending ---

class TestListPending:
    def test_returns_pending_items(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Alpha\n"
            "- Beta\n\n"
            "## Done\n"
        )
        result = list_pending(content)
        assert len(result) == 2
        assert "Alpha" in result[0]
        assert "Beta" in result[1]

    def test_empty_pending(self):
        content = "# Missions\n\n## Pending\n\n## Done\n"
        assert list_pending(content) == []

    def test_no_pending_section(self):
        content = "# Missions\n\n## Done\n- Task\n"
        assert list_pending(content) == []

    def test_skips_strikethrough_items(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- ~~[project:foo] Old task~~ ✅ (2026-03-07, PR #1)\n"
            "- ~~Another done task~~ ✅ (2026-03-07)\n"
            "- [project:foo] Real pending task\n\n"
            "## Done\n"
        )
        result = list_pending(content)
        assert len(result) == 1
        assert "Real pending task" in result[0]


# --- extract_next_pending with strikethrough ---


class TestExtractNextPendingStrikethrough:
    def test_skips_strikethrough_returns_real_pending(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- ~~[project:foo] Completed task~~ ✅ (2026-03-07)\n"
            "- [project:foo] Actual pending task\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = extract_next_pending(content, "foo")
        assert "Actual pending task" in result
        assert "Completed task" not in result

    def test_skips_all_strikethrough_returns_empty(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- ~~[project:foo] Done 1~~ ✅ (2026-03-07)\n"
            "- ~~[project:foo] Done 2~~ ✅ (2026-03-07)\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        result = extract_next_pending(content, "foo")
        assert result == ""

    def test_count_pending_skips_strikethrough(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- ~~Old task~~ ✅ (2026-03-07)\n"
            "- Real task\n\n"
            "## Done\n"
        )
        assert count_pending(content) == 2  # count_pending uses parse_sections (raw)
        assert len(list_pending(content)) == 1  # list_pending filters


# --- cancel_pending_mission ---

class TestCancelPendingMission:
    def test_cancel_by_number(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Fix auth\n"
            "- Add tests\n"
            "- Update docs\n\n"
            "## Done\n"
        )
        updated, cancelled = cancel_pending_mission(content, "2")
        assert "Add tests" in cancelled
        pending = list_pending(updated)
        assert len(pending) == 2
        assert all("Add tests" not in p for p in pending)

    def test_cancel_by_keyword(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Fix auth bug\n"
            "- Add tests for login\n\n"
            "## Done\n"
        )
        updated, cancelled = cancel_pending_mission(content, "login")
        assert "login" in cancelled
        pending = list_pending(updated)
        assert len(pending) == 1

    def test_cancel_keyword_case_insensitive(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Fix AUTH bug\n\n"
            "## Done\n"
        )
        updated, cancelled = cancel_pending_mission(content, "auth")
        assert "AUTH" in cancelled

    def test_cancel_no_pending_raises(self):
        content = "# Missions\n\n## Pending\n\n## Done\n"
        with pytest.raises(ValueError, match="No pending missions"):
            cancel_pending_mission(content, "1")

    def test_cancel_number_out_of_range(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Only one\n\n"
            "## Done\n"
        )
        with pytest.raises(ValueError, match="not found"):
            cancel_pending_mission(content, "5")

    def test_cancel_keyword_no_match(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Fix auth\n\n"
            "## Done\n"
        )
        with pytest.raises(ValueError, match="No pending mission matching"):
            cancel_pending_mission(content, "nonexistent")

    def test_cancel_first_item(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Alpha\n"
            "- Beta\n\n"
            "## Done\n"
        )
        updated, cancelled = cancel_pending_mission(content, "1")
        assert "Alpha" in cancelled
        pending = list_pending(updated)
        assert len(pending) == 1
        assert "Beta" in pending[0]

    def test_cancel_with_whitespace_identifier(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Fix auth\n\n"
            "## Done\n"
        )
        updated, cancelled = cancel_pending_mission(content, "  1  ")
        assert "Fix auth" in cancelled


# ---------------------------------------------------------------------------
# prune_done_section
# ---------------------------------------------------------------------------

class TestPruneDoneSection:
    """Tests for prune_done_section() — keeps Done section bounded."""

    def test_no_pruning_when_under_limit(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "## Done\n"
            "- Task 1 ✅\n"
            "- Task 2 ✅\n"
            "- Task 3 ✅\n"
        )
        result, pruned = prune_done_section(content, keep=5)
        assert pruned == 0
        assert result == content

    def test_prunes_excess_done_items(self):
        done_items = "\n".join(f"- Task {i} ✅" for i in range(10))
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Pending task\n\n"
            "## In Progress\n\n"
            "## Done\n"
            f"{done_items}\n"
        )
        result, pruned = prune_done_section(content, keep=3)
        assert pruned == 7
        sections = parse_sections(result)
        assert len(sections["done"]) == 3
        # Keeps the first 3 (most recent, at top of Done)
        assert "Task 0" in sections["done"][0]
        assert "Task 2" in sections["done"][2]
        # Pending is preserved
        assert len(sections["pending"]) == 1

    def test_no_done_section(self):
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- Task\n"
        )
        result, pruned = prune_done_section(content, keep=5)
        assert pruned == 0

    def test_preserves_other_sections(self):
        done_items = "\n".join(f"- Done {i}" for i in range(20))
        content = (
            "# Missions\n\n"
            "## Ideas\n\n"
            "- An idea\n\n"
            "## Pending\n\n"
            "- Pending task\n\n"
            "## In Progress\n\n"
            "- Active task\n\n"
            "## Done\n"
            f"{done_items}\n"
        )
        result, pruned = prune_done_section(content, keep=5)
        assert pruned == 15
        sections = parse_sections(result)
        assert len(sections["done"]) == 5
        assert len(sections["pending"]) == 1
        assert len(sections["in_progress"]) == 1
        assert "An idea" in result

    def test_exactly_at_limit(self):
        done_items = "\n".join(f"- Task {i}" for i in range(5))
        content = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## Done\n"
            f"{done_items}\n"
        )
        result, pruned = prune_done_section(content, keep=5)
        assert pruned == 0


# ---------------------------------------------------------------------------
# sanitize_mission_text
# ---------------------------------------------------------------------------

class TestSanitizeMissionText:
    def test_replaces_newlines_with_space(self):
        text = "line one\nline two\nline three"
        assert sanitize_mission_text(text) == "line one line two line three"

    def test_replaces_crlf(self):
        text = "line one\r\nline two\r\nline three"
        assert sanitize_mission_text(text) == "line one line two line three"

    def test_replaces_cr(self):
        text = "line one\rline two"
        assert sanitize_mission_text(text) == "line one line two"

    def test_collapses_multiple_spaces(self):
        text = "word   word    word"
        assert sanitize_mission_text(text) == "word word word"

    def test_strips_whitespace(self):
        text = "  hello world  "
        assert sanitize_mission_text(text) == "hello world"

    def test_mixed_newlines_and_spaces(self):
        text = "line one\n  \n  line two  \n"
        assert sanitize_mission_text(text) == "line one line two"

    def test_empty_string(self):
        assert sanitize_mission_text("") == ""

    def test_single_line_unchanged(self):
        text = "a normal single-line mission"
        assert sanitize_mission_text(text) == text

    def test_preserves_markdown_content(self):
        text = "fix the **bold** and `code` formatting"
        assert sanitize_mission_text(text) == text


# ---------------------------------------------------------------------------
# insert_mission newline safety net
# ---------------------------------------------------------------------------

class TestInsertMissionNewlineSanitization:
    @patch("app.missions._now_iso", return_value="2026-01-01T12:00")
    def test_newlines_in_entry_collapsed(self, _mock):
        content = "## Pending\n\n## Done\n"
        entry = "- fix\nthe\nbug"
        result = insert_mission(content, entry)
        # The entry should appear as a single line
        assert "- fix the bug" in result
        # No bare newlines within the entry text
        for line in result.split("\n"):
            if "fix" in line and "bug" in line:
                assert "\n" not in line.replace("\n", "")  # trivially true per line


# ---------------------------------------------------------------------------
# quarantine_mission shared helper
# ---------------------------------------------------------------------------

class TestQuarantineMission:

    def test_writes_entry(self, tmp_path):
        qpath = tmp_path / "missions-quarantine.md"
        ok = quarantine_mission(qpath, "bad text", "injection", source="telegram")
        assert ok is True
        content = qpath.read_text()
        assert "injection" in content
        assert "bad text" in content
        assert "telegram" in content
        assert "\U0001f6e1\ufe0f" in content  # shield emoji

    def test_appends_multiple(self, tmp_path):
        qpath = tmp_path / "missions-quarantine.md"
        quarantine_mission(qpath, "first", "reason1", source="telegram")
        quarantine_mission(qpath, "second", "reason2", source="github/@alice")
        content = qpath.read_text()
        assert "first" in content
        assert "second" in content
        assert "github/@alice" in content

    def test_truncates_long_text(self, tmp_path):
        qpath = tmp_path / "missions-quarantine.md"
        long_text = "x" * 1000
        quarantine_mission(qpath, long_text, "too long")
        content = qpath.read_text()
        # Entry should contain at most 500 chars of the text
        assert "x" * 500 in content
        assert "x" * 501 not in content

    def test_returns_false_on_oserror(self, tmp_path):
        # Non-existent parent → OSError
        qpath = tmp_path / "no" / "such" / "dir" / "quarantine.md"
        ok = quarantine_mission(qpath, "text", "reason")
        assert ok is False

    def test_default_source(self, tmp_path):
        qpath = tmp_path / "missions-quarantine.md"
        quarantine_mission(qpath, "text", "reason")
        assert "unknown" in qpath.read_text()


class TestQuarantineSizeCap:

    def test_no_prune_under_limit(self, tmp_path):
        qpath = tmp_path / "quarantine.md"
        qpath.write_text("- entry 1\n- entry 2\n")
        _enforce_quarantine_cap(qpath)
        assert "entry 1" in qpath.read_text()
        assert "entry 2" in qpath.read_text()

    def test_prunes_when_over_limit(self, tmp_path):
        qpath = tmp_path / "quarantine.md"
        # Write enough data to exceed the cap
        lines = [f"- entry {i}: {'x' * 200}\n" for i in range(600)]
        qpath.write_text("".join(lines))
        assert qpath.stat().st_size > QUARANTINE_MAX_BYTES
        _enforce_quarantine_cap(qpath)
        remaining = qpath.read_text()
        # Older half was pruned
        assert "entry 0" not in remaining
        assert "entry 1" not in remaining
        # Newer half is kept
        assert "entry 599" in remaining
        # File is smaller now
        assert qpath.stat().st_size < QUARANTINE_MAX_BYTES * 0.7

    def test_cap_enforced_on_write(self, tmp_path):
        qpath = tmp_path / "quarantine.md"
        lines = [f"- old entry {i}: {'y' * 200}\n" for i in range(600)]
        qpath.write_text("".join(lines))
        quarantine_mission(qpath, "new entry", "reason", source="test")
        content = qpath.read_text()
        # Old entries pruned
        assert "old entry 0" not in content
        # New entry present
        assert "new entry" in content
