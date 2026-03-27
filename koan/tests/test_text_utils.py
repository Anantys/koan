"""Tests for text_utils — shared text processing for messaging delivery."""

from unittest.mock import patch
import pytest
from app.text_utils import (
    strip_markdown, clean_cli_response, DEFAULT_MAX_LENGTH,
    expand_github_refs, extract_project_from_message,
    expand_github_refs_auto, _resolve_project_for_refs,
)


class TestStripMarkdown:
    """Tests for strip_markdown()."""

    def test_plain_text_unchanged(self):
        assert strip_markdown("Hello world") == "Hello world"

    def test_empty_string(self):
        assert strip_markdown("") == ""

    def test_strips_bold(self):
        assert strip_markdown("This is **bold** text") == "This is bold text"

    def test_strips_underline(self):
        assert strip_markdown("This is __underlined__") == "This is underlined"

    def test_strips_strikethrough(self):
        assert strip_markdown("This is ~~deleted~~") == "This is deleted"

    def test_strips_code_fences(self):
        text = "```python\nprint('hello')\n```"
        assert strip_markdown(text) == "python\nprint('hello')\n"

    def test_strips_inline_code_fence(self):
        assert strip_markdown("Use ```code``` here") == "Use code here"

    def test_strips_heading_h1(self):
        assert strip_markdown("# Title") == "Title"

    def test_strips_heading_h2(self):
        assert strip_markdown("## Section") == "Section"

    def test_strips_heading_h3(self):
        assert strip_markdown("### Subsection") == "Subsection"

    def test_strips_heading_h6(self):
        assert strip_markdown("###### Deep heading") == "Deep heading"

    def test_hash_without_space_preserved(self):
        """A # without trailing space is NOT a heading."""
        assert strip_markdown("#hashtag") == "#hashtag"

    def test_multiline_headings(self):
        text = "# Title\n\n## Section\n\nSome text\n\n### Sub"
        expected = "Title\n\nSection\n\nSome text\n\nSub"
        assert strip_markdown(text) == expected

    def test_combined_formatting(self):
        text = "## **Bold heading**\n\nSome ~~deleted~~ and __underlined__ text"
        expected = "Bold heading\n\nSome deleted and underlined text"
        assert strip_markdown(text) == expected

    def test_nested_bold_in_code_fence(self):
        text = "```\n**not bold inside fence**\n```"
        result = strip_markdown(text)
        assert "**" not in result
        assert "```" not in result
        assert "not bold inside fence" in result

    def test_whitespace_only(self):
        assert strip_markdown("   \n  \n  ") == "   \n  \n  "

    def test_no_heading_mid_line(self):
        """Heading markers only stripped at line start."""
        assert strip_markdown("This is not ## a heading") == "This is not ## a heading"

    def test_multiple_bold_pairs(self):
        text = "**first** and **second** bold"
        assert strip_markdown(text) == "first and second bold"


class TestCleanCliResponse:
    """Tests for clean_cli_response()."""

    def test_plain_response(self):
        assert clean_cli_response("Hello world") == "Hello world"

    def test_empty_response(self):
        assert clean_cli_response("") == ""

    def test_strips_max_turns_error(self):
        text = "Error: max turns reached\nActual response"
        assert clean_cli_response(text) == "Actual response"

    def test_strips_max_turns_case_insensitive(self):
        text = "error: MAX TURNS reached in conversation\nOK"
        assert clean_cli_response(text) == "OK"

    def test_strips_markdown(self):
        text = "## Title\n\n**Bold** and ```code```"
        result = clean_cli_response(text)
        assert "##" not in result
        assert "**" not in result
        assert "```" not in result
        assert "Title" in result
        assert "Bold" in result

    def test_truncates_at_default_limit(self):
        text = "x" * 3000
        result = clean_cli_response(text)
        assert len(result) == DEFAULT_MAX_LENGTH
        assert result.endswith("...")

    def test_truncates_at_custom_limit(self):
        text = "x" * 500
        result = clean_cli_response(text, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_no_truncation_within_limit(self):
        text = "Short message"
        result = clean_cli_response(text)
        assert result == "Short message"

    def test_exact_limit_no_truncation(self):
        text = "x" * DEFAULT_MAX_LENGTH
        result = clean_cli_response(text)
        assert result == text
        assert not result.endswith("...")

    def test_strips_surrounding_whitespace(self):
        text = "  \n  Hello  \n  "
        assert clean_cli_response(text) == "Hello"

    def test_preserves_non_error_lines(self):
        text = "Line 1\nError: something else\nLine 3"
        result = clean_cli_response(text)
        assert "Line 1" in result
        assert "Error: something else" in result
        assert "Line 3" in result

    def test_only_strips_max_turns_errors(self):
        text = "Error: max turns limit\nError: network timeout\nDone"
        result = clean_cli_response(text)
        assert "max turns" not in result
        assert "Error: network timeout" in result
        assert "Done" in result

    def test_full_pipeline(self):
        """Tests the complete cleaning pipeline: error strip + markdown strip + truncate."""
        text = (
            "Error: max turns reached\n"
            "## Report\n"
            "\n"
            "**Found** 3 issues:\n"
            "- ~~old~~ new approach\n"
            "- __underline__ check\n"
        )
        result = clean_cli_response(text)
        assert "max turns" not in result
        assert "**" not in result
        assert "~~" not in result
        assert "__" not in result
        assert "Found 3 issues:" in result
        assert "Report" in result

    def test_multiple_error_lines(self):
        text = "Error: max turns 1\nError: max turns 2\nActual content"
        assert clean_cli_response(text) == "Actual content"

    def test_error_line_only(self):
        text = "Error: max turns reached"
        assert clean_cli_response(text) == ""


class TestExpandGithubRefs:
    """Tests for expand_github_refs()."""

    GITHUB_URL = "https://github.com/sukria/koan"

    def test_basic_ref(self):
        result = expand_github_refs("See #123", self.GITHUB_URL)
        assert result == "See #123 (https://github.com/sukria/koan/issues/123)"

    def test_multiple_refs(self):
        result = expand_github_refs("Fix #10 and #20", self.GITHUB_URL)
        assert "(https://github.com/sukria/koan/issues/10)" in result
        assert "(https://github.com/sukria/koan/issues/20)" in result

    def test_pr_prefix(self):
        result = expand_github_refs("PR #42 is ready", self.GITHUB_URL)
        assert "#42 (https://github.com/sukria/koan/issues/42)" in result

    def test_no_expansion_in_url_path(self):
        """A number after / (like in a URL path) should not be expanded."""
        text = "See /issues/123 for details"
        result = expand_github_refs(text, self.GITHUB_URL)
        assert result == text

    def test_no_expansion_for_word_prefix(self):
        """#123 preceded by a word character should not expand."""
        text = "tag#123 is not a ref"
        result = expand_github_refs(text, self.GITHUB_URL)
        assert result == text

    def test_already_expanded_not_doubled(self):
        """If #123 is already followed by its URL in parens, skip it."""
        text = "#123 (https://github.com/sukria/koan/issues/123)"
        result = expand_github_refs(text, self.GITHUB_URL)
        assert result == text

    def test_empty_text(self):
        assert expand_github_refs("", self.GITHUB_URL) == ""

    def test_none_text(self):
        assert expand_github_refs(None, self.GITHUB_URL) is None

    def test_empty_github_url(self):
        assert expand_github_refs("See #1", "") == "See #1"

    def test_no_refs_in_text(self):
        text = "No references here"
        assert expand_github_refs(text, self.GITHUB_URL) == text

    def test_trailing_slash_stripped(self):
        result = expand_github_refs("#5", "https://github.com/o/r/")
        assert "(https://github.com/o/r/issues/5)" in result

    def test_ref_at_start_of_line(self):
        result = expand_github_refs("#99 was fixed", self.GITHUB_URL)
        assert "#99 (https://github.com/sukria/koan/issues/99)" in result

    def test_ref_after_paren(self):
        result = expand_github_refs("(#7)", self.GITHUB_URL)
        assert "#7 (https://github.com/sukria/koan/issues/7)" in result

    def test_hex_color_not_expanded(self):
        """Hex color codes like #FF0000 should not match (they have letters)."""
        text = "Color is #FF0000"
        result = expand_github_refs(text, self.GITHUB_URL)
        assert result == text


class TestExtractProjectFromMessage:
    """Tests for extract_project_from_message()."""

    def test_emoji_bracket_pattern(self):
        assert extract_project_from_message("🏁 [koan]") == "koan"

    def test_project_colon_pattern(self):
        assert extract_project_from_message("[project:my-app]") == "my-app"

    def test_project_with_dots(self):
        assert extract_project_from_message("[project:perl-XML-LibXML]") == "perl-XML-LibXML"

    def test_no_project(self):
        assert extract_project_from_message("Just a message") == ""

    def test_bracket_mid_text(self):
        assert extract_project_from_message("Done [koan] stuff") == "koan"

    def test_empty_string(self):
        assert extract_project_from_message("") == ""

    def test_project_colon_preferred(self):
        """[project:X] should be found even if [Y] also appears."""
        text = "[project:real] and [decoy]"
        assert extract_project_from_message(text) == "real"


class TestResolveProjectForRefs:
    """Tests for _resolve_project_for_refs()."""

    def test_from_text_tag(self):
        assert _resolve_project_for_refs("🏁 [koan] done") == "koan"

    def test_from_hint_text(self):
        assert _resolve_project_for_refs("no tag here", "[project:myapp]") == "myapp"

    def test_first_text_wins(self):
        assert _resolve_project_for_refs("[first]", "[second]") == "first"

    @patch("app.text_utils._read_current_project_file", return_value="running-proj")
    def test_falls_back_to_project_file(self, _mock):
        assert _resolve_project_for_refs("no tag") == "running-proj"

    @patch("app.text_utils._read_current_project_file", return_value="unknown")
    def test_ignores_unknown_project_file(self, _mock, monkeypatch):
        monkeypatch.setenv("KOAN_CURRENT_PROJECT", "envproj")
        assert _resolve_project_for_refs("no tag") == "envproj"

    @patch("app.text_utils._read_current_project_file", return_value="")
    def test_falls_back_to_env(self, _mock, monkeypatch):
        monkeypatch.setenv("KOAN_CURRENT_PROJECT", "envproj")
        assert _resolve_project_for_refs("no tag") == "envproj"

    @patch("app.text_utils._read_current_project_file", return_value="")
    def test_no_project_found(self, _mock, monkeypatch):
        monkeypatch.delenv("KOAN_CURRENT_PROJECT", raising=False)
        assert _resolve_project_for_refs("no tag") == ""


class TestExpandGithubRefsAuto:
    """Tests for expand_github_refs_auto()."""

    GITHUB_URL = "https://github.com/sukria/koan"

    @patch("app.text_utils._resolve_project_for_refs", return_value="koan")
    @patch("app.projects_merged.get_github_url", return_value="https://github.com/sukria/koan")
    def test_expands_with_detected_project(self, _mock_url, _mock_proj):
        result = expand_github_refs_auto("See #42")
        assert "(https://github.com/sukria/koan/issues/42)" in result

    @patch("app.text_utils._resolve_project_for_refs", return_value="")
    def test_no_project_returns_unchanged(self, _mock):
        assert expand_github_refs_auto("See #42") == "See #42"

    @patch("app.text_utils._resolve_project_for_refs", return_value="koan")
    @patch("app.projects_merged.get_github_url", return_value=None)
    def test_no_github_url_returns_unchanged(self, _mock_url, _mock_proj):
        assert expand_github_refs_auto("See #42") == "See #42"

    def test_empty_text(self):
        assert expand_github_refs_auto("") == ""

    def test_none_text(self):
        assert expand_github_refs_auto(None) is None

    def test_no_hash_skips_lookup(self):
        """Text without # should return immediately without project lookup."""
        result = expand_github_refs_auto("No refs here")
        assert result == "No refs here"

    @patch("app.text_utils._resolve_project_for_refs", return_value="koan")
    @patch("app.projects_merged.get_github_url", return_value="https://github.com/sukria/koan")
    def test_passes_hint_texts(self, _mock_url, mock_proj):
        expand_github_refs_auto("See #1", "hint1", "hint2")
        mock_proj.assert_called_once_with("See #1", "hint1", "hint2")

    @patch("app.text_utils._resolve_project_for_refs", return_value="koan")
    @patch("app.projects_merged.get_github_url", side_effect=ImportError("no module"))
    def test_import_error_returns_unchanged(self, _mock_url, _mock_proj):
        assert expand_github_refs_auto("See #42") == "See #42"
