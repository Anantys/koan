"""Tests for markdown_to_adf — the rich markdown→ADF converter for Jira."""

from app.jira_notifications import markdown_to_adf


def _types(doc):
    return [node["type"] for node in doc["content"]]


def _marks(node):
    return [m["type"] for text in node.get("content", []) for m in text.get("marks", [])]


class TestMarkdownToAdfBlocks:
    def test_returns_valid_doc_envelope(self):
        doc = markdown_to_adf("hello")
        assert doc["type"] == "doc"
        assert doc["version"] == 1
        assert isinstance(doc["content"], list)

    def test_heading_levels(self):
        doc = markdown_to_adf("# One\n\n## Two\n\n#### Four")
        headings = [n for n in doc["content"] if n["type"] == "heading"]
        assert [h["attrs"]["level"] for h in headings] == [1, 2, 4]
        assert headings[0]["content"][0]["text"] == "One"

    def test_bullet_list(self):
        doc = markdown_to_adf("- a\n- b\n- c")
        assert _types(doc) == ["bulletList"]
        items = doc["content"][0]["content"]
        assert len(items) == 3
        assert items[0]["type"] == "listItem"
        assert items[0]["content"][0]["type"] == "paragraph"

    def test_task_items_kept_as_bullet_text(self):
        doc = markdown_to_adf("- [ ] todo\n- [x] done")
        items = doc["content"][0]["content"]
        # checkbox marker preserved as leading text (per design decision)
        assert items[0]["content"][0]["content"][0]["text"].startswith("[ ] ")
        assert items[1]["content"][0]["content"][0]["text"].startswith("[x] ")

    def test_ordered_list(self):
        doc = markdown_to_adf("1. first\n2. second")
        assert _types(doc) == ["orderedList"]
        assert len(doc["content"][0]["content"]) == 2

    def test_horizontal_rule(self):
        for marker in ("---", "***", "___"):
            doc = markdown_to_adf(f"a\n\n{marker}\n\nb")
            assert "rule" in _types(doc)

    def test_blockquote(self):
        doc = markdown_to_adf("> quoted line\n> second")
        assert _types(doc) == ["blockquote"]
        para = doc["content"][0]["content"][0]
        assert para["type"] == "paragraph"

    def test_fenced_code_block_is_verbatim(self):
        doc = markdown_to_adf("```python\n## not a heading\n**not bold**\n```")
        assert _types(doc) == ["codeBlock"]
        block = doc["content"][0]
        assert block["attrs"]["language"] == "python"
        # inner markdown is NOT parsed
        assert block["content"][0]["text"] == "## not a heading\n**not bold**"

    def test_fenced_code_block_without_language(self):
        doc = markdown_to_adf("```\nplain\n```")
        block = doc["content"][0]
        assert block["type"] == "codeBlock"
        assert "attrs" not in block

    def test_indented_code_block(self):
        doc = markdown_to_adf("Repository implementation:\n\n      return value();")
        assert _types(doc) == ["paragraph", "codeBlock"]
        assert doc["content"][1]["content"][0]["text"] == "return value();"

    def test_paragraph_fallback_for_plain_text(self):
        doc = markdown_to_adf("just a sentence with no structure")
        assert _types(doc) == ["paragraph"]

    def test_gfm_table_becomes_native_adf_table(self):
        doc = markdown_to_adf(
            "| Action | File |\n| --- | --- |\n| Modify | `app.py` |"
        )
        table = doc["content"][0]
        assert table["type"] == "table"
        assert table["content"][0]["content"][0]["type"] == "tableHeader"
        assert table["content"][1]["content"][1]["type"] == "tableCell"

    def test_github_details_are_expanded_without_html(self):
        doc = markdown_to_adf(
            "<details><summary>Test code</summary>\n\n```python\nassert True\n```\n</details>"
        )
        assert "codeBlock" in _types(doc)
        text = " ".join(
            child.get("text", "")
            for node in doc["content"]
            for child in node.get("content", [])
        )
        assert "Test code" in text
        assert "<details>" not in text


class TestMarkdownToAdfInline:
    def test_bold_mark(self):
        doc = markdown_to_adf("some **bold** here")
        assert "strong" in _marks(doc["content"][0])

    def test_inline_code_mark(self):
        doc = markdown_to_adf("call `foo()` now")
        node = doc["content"][0]
        assert "code" in _marks(node)
        code_text = [t["text"] for t in node["content"] if t.get("marks")][0]
        assert code_text == "foo()"

    def test_em_mark(self):
        doc = markdown_to_adf("this is *emphasized* text")
        assert "em" in _marks(doc["content"][0])

    def test_underscore_em_mark(self):
        doc = markdown_to_adf("this is _emphasized_ text")
        assert "em" in _marks(doc["content"][0])

    def test_code_content_not_reparsed_for_marks(self):
        doc = markdown_to_adf("`**not bold**`")
        node = doc["content"][0]
        code_text = [t["text"] for t in node["content"] if t.get("marks")][0]
        assert code_text == "**not bold**"

    def test_unbalanced_marker_is_literal(self):
        doc = markdown_to_adf("a lone * asterisk and ** stars")
        # no marks applied; text preserved (does not raise)
        texts = "".join(t["text"] for t in doc["content"][0]["content"])
        assert "*" in texts

    def test_intra_word_underscore_is_literal(self):
        # snake_case identifiers / file paths must not be italicized and must
        # keep their underscores (CommonMark: intra-word _ is not emphasis).
        doc = markdown_to_adf("see mission_executor.py and run_claude_task")
        node = doc["content"][0]
        assert _marks(node) == []
        texts = "".join(t["text"] for t in node["content"])
        assert texts == "see mission_executor.py and run_claude_task"

    def test_delimited_underscore_em_still_marks(self):
        # a properly flanked _word_ is still emphasis
        doc = markdown_to_adf("this is _emphasized_ text")
        assert "em" in _marks(doc["content"][0])

    def test_link_mark(self):
        doc = markdown_to_adf("Read [the docs](https://example.com/docs).")
        link = next(node for node in doc["content"][0]["content"] if node.get("marks"))
        assert link["text"] == "the docs"
        assert link["marks"] == [{"type": "link", "attrs": {"href": "https://example.com/docs"}}]


class TestMarkdownToAdfEdgeCases:
    def test_empty_input_yields_empty_paragraph(self):
        doc = markdown_to_adf("")
        assert doc["content"] == [{"type": "paragraph", "content": []}]

    def test_whitespace_only_input(self):
        doc = markdown_to_adf("   \n  \n")
        assert doc["content"] == [{"type": "paragraph", "content": []}]

    def test_blank_only_code_fence_has_no_empty_text_node(self):
        # a fence wrapping only a blank line must not emit an empty-string ADF
        # text node (invalid ADF → 400).
        doc = markdown_to_adf("```\n\n```")
        block = doc["content"][0]
        assert block["type"] == "codeBlock"
        assert "content" not in block or all(
            t["text"] for t in block.get("content", [])
        )

    def test_empty_heading_is_dropped(self):
        # a hashes-only heading has no inline content; it must not emit a
        # heading node with an empty content array.
        doc = markdown_to_adf("## \n\nreal text")
        assert "heading" not in _types(doc)

    def test_representative_brainstorm_body(self):
        body = (
            "## Why This Matters\n\n"
            "This unlocks value.\n\n"
            "## Approach\n\n"
            "- step one\n- step two\n\n"
            "## Scores\n\n"
            "Impact: ****- 4/5\n\n"
            "---\n\n"
            "See **SUB-2** for details."
        )
        doc = markdown_to_adf(body)
        types = _types(doc)
        assert "heading" in types
        assert "bulletList" in types
        assert "rule" in types
        # no exception, valid doc envelope
        assert doc["type"] == "doc"


class TestJiraNormalisationPreservesCode:
    """The Jira comment path must not rewrite markup that *is* the content."""

    def test_details_markup_in_a_fence_reaches_adf_verbatim(self):
        # markdown_to_adf normalises internally, so this is the real entry point
        # every production caller uses — no pre-flattening.
        source = "Example:\n\n```html\n<details><summary>x</summary>body</details>\n```"
        doc = markdown_to_adf(source)

        code = [n for n in doc["content"] if n.get("type") == "codeBlock"]
        assert len(code) == 1
        text = "".join(c.get("text", "") for c in code[0].get("content", []))
        assert text == "<details><summary>x</summary>body</details>"
