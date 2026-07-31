"""Jira notification fetching and parsing.

Handles polling Jira for @mention comments, parsing commands, and
tracking processed comments to avoid duplicate mission creation.

Authentication uses Atlassian Basic auth (email + API token).
Jira Cloud comment bodies are ADF (Atlassian Document Format) JSON —
this module extracts plain text from ADF before regex matching.
"""

import json
import logging
import os
import re
import time
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.bounded_set import BoundedSet

log = logging.getLogger(__name__)

# In-memory set of processed Jira comment IDs (resets on restart).
_MAX_PROCESSED_COMMENTS = 10000
_processed_comments: BoundedSet = BoundedSet(maxlen=_MAX_PROCESSED_COMMENTS)

# Regex for stripping code blocks before @mention search (same as GitHub module)
_CODE_BLOCK_RE = re.compile(r'\{\{.*?\}\}|{{noformat.*?noformat}}|\{code.*?\{code\}', re.DOTALL)


class JiraFetchResult:
    """Result from fetch_jira_mentions."""

    __slots__ = ("mentions",)

    def __init__(self, mentions: List[dict]):
        self.mentions = mentions


def _make_auth_header(email: str, api_token: str) -> str:
    """Build Basic auth header value for Atlassian API."""
    creds = f"{email}:{api_token}"
    encoded = b64encode(creds.encode()).decode()
    return f"Basic {encoded}"


def _jira_get(
    base_url: str,
    auth_header: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Optional[dict]:
    """Make a GET request to the Jira REST API.

    Args:
        base_url: Jira instance base URL (e.g. https://myorg.atlassian.net).
        auth_header: Basic auth header value.
        path: API path (e.g. /rest/api/3/issue/{key}/comment).
        params: Optional query parameters.
        timeout: Per-request socket timeout in seconds.

    Returns:
        Parsed JSON dict/list, or None on error.
    """
    try:
        import urllib.request
        import urllib.parse

        url = base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url)
        req.add_header("Authorization", auth_header)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except Exception as e:
        log.warning("Jira API GET %s failed: %s", path, e)
        return None


def _jira_post(base_url: str, auth_header: str, path: str, body: Dict[str, Any]) -> Optional[dict]:
    """Make a POST request to the Jira REST API.

    Args:
        base_url: Jira instance base URL (e.g. https://myorg.atlassian.net).
        auth_header: Basic auth header value.
        path: API path (e.g. /rest/api/3/search/jql).
        body: JSON request body.

    Returns:
        Parsed JSON dict/list, or None on error.
    """
    try:
        import urllib.request

        url = base_url + path
        data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", auth_header)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except Exception as e:
        log.warning("Jira API POST %s failed: %s", path, e)
        return None


def _jira_put(base_url: str, auth_header: str, path: str, body: Dict[str, Any]) -> Optional[dict]:
    """Make a PUT request to the Jira REST API."""
    try:
        import urllib.request

        url = base_url + path
        data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Authorization", auth_header)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except Exception as e:
        log.warning("Jira API PUT %s failed: %s", path, e)
        return None


def _adf_to_text(node: Any) -> str:
    """Recursively extract plain text from an Atlassian Document Format (ADF) node.

    ADF is a JSON tree format used by Jira Cloud comment bodies.
    This extracts text nodes while ignoring formatting and code blocks.

    Args:
        node: An ADF node (dict) or list of nodes.

    Returns:
        Plain text string.
    """
    if not node:
        return ""

    if isinstance(node, list):
        return " ".join(_adf_to_text(item) for item in node)

    if not isinstance(node, dict):
        return str(node)

    node_type = node.get("type", "")

    # Skip code blocks — don't want to match @mentions inside code
    if node_type in ("codeBlock", "code", "inlineCard"):
        return ""

    # Text nodes carry the actual content
    if node_type == "text":
        return node.get("text", "")

    # Mention nodes (Jira @mentions different from text @mentions)
    if node_type == "mention":
        attrs = node.get("attrs", {})
        text = attrs.get("text", "")
        return text

    # Hard break → space
    if node_type in ("hardBreak", "rule"):
        return " "

    # Recurse into content children
    children = node.get("content", [])
    parts = []
    for child in children:
        text = _adf_to_text(child)
        if text:
            parts.append(text)
    return " ".join(parts)


def _adf_inline_to_markdown(nodes: Any) -> str:
    """Render inline ADF text nodes as the Markdown subset Koan emits."""
    if not isinstance(nodes, list):
        return ""
    rendered: List[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "hardBreak":
            rendered.append("\n")
            continue
        if node.get("type") == "mention":
            rendered.append(str(node.get("attrs", {}).get("text", "")))
            continue
        if node.get("type") != "text":
            continue
        text = str(node.get("text", ""))
        marks = {mark.get("type"): mark for mark in node.get("marks", [])}
        if "code" in marks:
            text = f"`{text}`"
        if "strong" in marks:
            text = f"**{text}**"
        if "em" in marks:
            text = f"*{text}*"
        link = marks.get("link")
        if link:
            href = str(link.get("attrs", {}).get("href", ""))
            text = f"[{text}]({href})" if href else text
        rendered.append(text)
    return "".join(rendered)


def _adf_to_markdown(node: Any) -> str:
    """Render Jira ADF as Markdown for tracker skill context.

    This is deliberately separate from :func:`_adf_to_text`: mention polling
    must ignore code, while plan extraction needs headings and code intact.
    """
    if not node:
        return ""
    if isinstance(node, list):
        return "\n\n".join(filter(None, (_adf_to_markdown(item) for item in node)))
    if not isinstance(node, dict):
        return str(node)

    node_type = node.get("type", "")
    content = node.get("content", [])
    if node_type == "text":
        return _adf_inline_to_markdown([node])
    if node_type in ("doc", "listItem"):
        return _adf_to_markdown(content)
    if node_type == "paragraph":
        return _adf_inline_to_markdown(content)
    if node_type == "heading":
        level = max(1, min(6, int(node.get("attrs", {}).get("level", 1))))
        return f"{'#' * level} {_adf_inline_to_markdown(content)}".rstrip()
    if node_type == "codeBlock":
        language = str(node.get("attrs", {}).get("language", ""))
        return f"```{language}\n{_adf_inline_to_markdown(content)}\n```"
    if node_type == "rule":
        return "---"
    if node_type == "blockquote":
        body = _adf_to_markdown(content)
        return "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    if node_type in ("bulletList", "orderedList"):
        lines: List[str] = []
        for index, item in enumerate(content, 1):
            item_body = _adf_to_markdown(item).replace("\n\n", "\n")
            prefix = "- " if node_type == "bulletList" else f"{index}. "
            lines.append(prefix + item_body)
        return "\n".join(lines)
    if node_type == "table":
        rows: List[str] = []
        for index, row in enumerate(content):
            cells = [
                _adf_to_markdown(cell).replace("\n", " ")
                for cell in row.get("content", [])
            ]
            rows.append("| " + " | ".join(cells) + " |")
            if index == 0:
                rows.append("| " + " | ".join("---" for _ in cells) + " |")
        return "\n".join(rows)
    return _adf_to_markdown(content)


def _text_to_adf(text: str) -> Dict[str, Any]:
    """Convert plain markdown-ish text to a simple Jira ADF document."""
    lines = (text or "").splitlines() or [""]
    content = []
    paragraph = []

    def flush_paragraph():
        if paragraph:
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": "\n".join(paragraph)}],
            })
            paragraph.clear()

    for line in lines:
        if line.strip():
            paragraph.append(line)
        else:
            flush_paragraph()
    flush_paragraph()

    if not content:
        content = [{"type": "paragraph", "content": []}]

    return {"version": 1, "type": "doc", "content": content}


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_ULIST_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_MD_OLIST_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_MD_RULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_MD_FENCE_RE = re.compile(r"^\s*```(.*)$")
_MD_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_MD_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)(.*)$")
_MD_TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-{3,}:?$")
_MD_INLINE_RE = re.compile(
    r"(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^\s)]+)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*)"
    # Underscore emphasis must be flanked by non-word boundaries so intra-word
    # underscores (snake_case identifiers, file paths like ``my_module.py``) are
    # left literal — matching CommonMark. Asterisk emphasis stays intra-word.
    r"|(?P<em>\*[^*\s][^*]*\*|(?<!\w)_[^_\s][^_]*_(?!\w))"
)


def _normalise_jira_markdown(text: str) -> str:
    """Convert GitHub-only markdown extensions into Jira-readable Markdown.

    Jira's ADF schema has no collapsible ``details`` node and does not
    understand GitHub alert syntax.  Keep the useful content, but remove only
    those wrappers before the standard Markdown parser sees the text.
    """
    from app.tracker_comment_format import flatten_github_markdown_for_jira

    return flatten_github_markdown_for_jira(text or "")


def _split_table_row(line: str) -> List[str]:
    """Split a simple GFM table row, preserving escaped pipe characters."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]

    cells: List[str] = []
    current: List[str] = []
    escaped = False
    for char in stripped:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
            continue
        if char == "\\" and not escaped:
            escaped = True
            current.append(char)
            continue
        escaped = False
        current.append(char)
    cells.append("".join(current).strip())
    return cells


def _is_table_delimiter(line: str, column_count: int) -> bool:
    cells = _split_table_row(line)
    return len(cells) == column_count and all(
        _MD_TABLE_DELIMITER_CELL_RE.fullmatch(cell.replace(" ", ""))
        for cell in cells
    )


def _adf_table_row(cells: List[str], header: bool) -> Dict[str, Any]:
    cell_type = "tableHeader" if header else "tableCell"
    return {
        "type": "tableRow",
        "content": [
            {
                "type": cell_type,
                "content": [{"type": "paragraph", "content": _inline_to_adf(cell)}],
            }
            for cell in cells
        ],
    }


def _inline_to_adf(text: str) -> List[Dict[str, Any]]:
    """Split a line of markdown into ADF text nodes with inline marks.

    Recognizes ``**bold**``, ``*em*`` / ``_em_``, and ``` `code` ```. Inline
    code takes precedence (its content is never re-parsed for other marks). A
    lone or unbalanced marker is emitted as literal text — never raises.
    """
    nodes: List[Dict[str, Any]] = []
    pos = 0
    for match in _MD_INLINE_RE.finditer(text):
        if match.start() > pos:
            nodes.append({"type": "text", "text": text[pos:match.start()]})
        if match.group("code"):
            nodes.append({
                "type": "text",
                "text": match.group("code")[1:-1],
                "marks": [{"type": "code"}],
            })
        elif match.group("link"):
            nodes.append({
                "type": "text",
                "text": match.group("link_text"),
                "marks": [{"type": "link", "attrs": {"href": match.group("link_url")}}],
            })
        elif match.group("bold"):
            nodes.append({
                "type": "text",
                "text": match.group("bold")[2:-2],
                "marks": [{"type": "strong"}],
            })
        else:  # em
            nodes.append({
                "type": "text",
                "text": match.group("em")[1:-1],
                "marks": [{"type": "em"}],
            })
        pos = match.end()
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return nodes


def _adf_list_items(item_texts: List[str]) -> List[Dict[str, Any]]:
    """Build ADF ``listItem`` nodes (each a paragraph) from raw item texts."""
    return [
        {
            "type": "listItem",
            "content": [{"type": "paragraph", "content": _inline_to_adf(text)}],
        }
        for text in item_texts
    ]


def markdown_to_adf(text: str) -> Dict[str, Any]:
    """Convert the markdown subset Kōan emits into a Jira ADF ``doc``.

    Structural constructs are mapped to native ADF nodes so Jira renders them
    richly instead of showing literal markdown:

    - ``#``–``######`` → ``heading`` (level = number of ``#``)
    - ``-``/``*``/``+`` list items → ``bulletList`` (task markers ``[ ]``/``[x]``
      are preserved as leading text)
    - ``1.`` list items → ``orderedList``
    - ``---``/``***``/``___`` → ``rule``
    - ``> `` lines → ``blockquote``
    - fenced ```` ``` ```` blocks → ``codeBlock`` (content is emitted verbatim,
      never re-parsed)
    - inline ``**bold**`` / ``*em*`` / ``` `code` `` → marks

    Any line that matches none of the above degrades to paragraph text, so
    unmodeled markdown is readable rather than dropped. Empty input yields a
    ``doc`` with a single empty paragraph.  The converter is used for both
    Jira issue descriptions and comments.
    """
    lines = _normalise_jira_markdown(text).splitlines()
    content: List[Dict[str, Any]] = []
    paragraph: List[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            content.append({
                "type": "paragraph",
                "content": _inline_to_adf("\n".join(paragraph)),
            })
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        fence = _MD_FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            language = fence.group(1).strip()
            code_lines: List[str] = []
            i += 1
            while i < len(lines) and not _MD_FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume closing fence (if present)
            node: Dict[str, Any] = {"type": "codeBlock"}
            if language:
                node["attrs"] = {"language": language}
            # Only attach a text node when there is actual code — an ADF text
            # node with an empty string is invalid and 400s the whole request
            # (e.g. a fence wrapping a single blank line).
            code_text = "\n".join(code_lines)
            if code_text:
                node["content"] = [{"type": "text", "text": code_text}]
            content.append(node)
            continue

        indented_code = _MD_INDENTED_CODE_RE.match(line)
        if indented_code:
            flush_paragraph()
            code_lines: List[str] = []
            while i < len(lines):
                indented_code = _MD_INDENTED_CODE_RE.match(lines[i])
                if indented_code:
                    code_lines.append(lines[i])
                    i += 1
                    continue
                if not lines[i].strip() and i + 1 < len(lines) and _MD_INDENTED_CODE_RE.match(lines[i + 1]):
                    code_lines.append("")
                    i += 1
                    continue
                break
            indent = min(
                len(code_line) - len(code_line.lstrip(" \t"))
                for code_line in code_lines if code_line.strip()
            )
            code_text = "\n".join(
                code_line[indent:] if code_line.strip() else ""
                for code_line in code_lines
            )
            node = {"type": "codeBlock"}
            if code_text:
                node["content"] = [{"type": "text", "text": code_text}]
            content.append(node)
            continue

        if not line.strip():
            flush_paragraph()
            i += 1
            continue

        if _MD_RULE_RE.match(line):
            flush_paragraph()
            content.append({"type": "rule"})
            i += 1
            continue

        # A table is a header row followed immediately by a GFM delimiter row.
        # Preserve malformed tables as paragraphs rather than risking data loss.
        header_cells = _split_table_row(line) if "|" in line else []
        if (
            len(header_cells) > 1
            and i + 1 < len(lines)
            and _is_table_delimiter(lines[i + 1], len(header_cells))
        ):
            flush_paragraph()
            rows = [_adf_table_row(header_cells, header=True)]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                cells = _split_table_row(lines[i])
                if len(cells) != len(header_cells):
                    break
                rows.append(_adf_table_row(cells, header=False))
                i += 1
            content.append({
                "type": "table",
                "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                "content": rows,
            })
            continue

        heading = _MD_HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            heading_content = _inline_to_adf(heading.group(2).strip())
            # Skip a hashes-only heading (``## `` with no text) — an ADF heading
            # with an empty content array can be rejected by the API.
            if heading_content:
                content.append({
                    "type": "heading",
                    "attrs": {"level": len(heading.group(1))},
                    "content": heading_content,
                })
            i += 1
            continue

        if _MD_ULIST_RE.match(line):
            flush_paragraph()
            items: List[str] = []
            while i < len(lines) and _MD_ULIST_RE.match(lines[i]):
                items.append(_MD_ULIST_RE.match(lines[i]).group(1))
                i += 1
            content.append({"type": "bulletList", "content": _adf_list_items(items)})
            continue

        if _MD_OLIST_RE.match(line):
            flush_paragraph()
            items = []
            while i < len(lines) and _MD_OLIST_RE.match(lines[i]):
                items.append(_MD_OLIST_RE.match(lines[i]).group(1))
                i += 1
            content.append({"type": "orderedList", "content": _adf_list_items(items)})
            continue

        if _MD_QUOTE_RE.match(line):
            flush_paragraph()
            quote_lines: List[str] = []
            while i < len(lines) and _MD_QUOTE_RE.match(lines[i]):
                quote_lines.append(_MD_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            content.append({
                "type": "blockquote",
                "content": [
                    {"type": "paragraph", "content": _inline_to_adf("\n".join(quote_lines))}
                ],
            })
            continue

        paragraph.append(line)
        i += 1

    flush_paragraph()

    if not content:
        content = [{"type": "paragraph", "content": []}]

    return {"version": 1, "type": "doc", "content": content}


def _extract_comment_text(comment_body: Any) -> str:
    """Extract plain text from a Jira comment body.

    Handles both:
    - ADF JSON (Jira Cloud): dict with "type": "doc"
    - Plain text (Jira Server/older): string

    Args:
        comment_body: The comment body field from Jira API.

    Returns:
        Plain text string.
    """
    if isinstance(comment_body, str):
        return comment_body
    if isinstance(comment_body, dict):
        return _adf_to_text(comment_body)
    return ""


def parse_jira_mention_command(text: str, nickname: str) -> Optional[Tuple[str, str]]:
    """Extract command and args from a @mention in a Jira comment body.

    Mirrors parse_mention_command() from github_notifications.py.
    Ignores mentions inside Jira code blocks ({code} ... {code}).
    Only processes the first @mention found.

    Args:
        text: The comment plain text.
        nickname: The bot's Jira mention name (without @).

    Returns:
        Tuple of (command, context) or None if no valid mention found.
        Command is lowercase. Context is remaining text after command.
    """
    if not text or not nickname:
        return None

    # Remove Jira code blocks to avoid matching mentions in code
    clean_text = _CODE_BLOCK_RE.sub("", text)

    # Match @nickname followed by a command word (optional leading / is stripped)
    pattern = rf'@{re.escape(nickname)}\s+/?(\w+)(.*?)(?:\n|$)'
    match = re.search(pattern, clean_text, re.IGNORECASE)
    if not match:
        return None

    command = match.group(1).strip().lower()
    context = match.group(2).strip()

    if not command:
        return None

    return command, context


def _get_comment_age_hours(updated_str: str) -> Optional[float]:
    """Compute hours since a Jira comment's updated timestamp.

    Args:
        updated_str: ISO 8601 timestamp string from Jira API.

    Returns:
        Age in hours, or None if unparseable.
    """
    try:
        # Jira returns timestamps like "2024-01-15T10:30:00.000+0000"
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
        return age
    except (ValueError, TypeError):
        return None


def _load_processed_tracker(tracker_path: Path) -> Set[str]:
    """Load the set of processed comment IDs from the persistent tracker file.

    Args:
        tracker_path: Path to .jira-processed.json in instance dir.

    Returns:
        Set of processed comment IDs.
    """
    try:
        if tracker_path.exists():
            data = json.loads(tracker_path.read_text())
            if isinstance(data, list):
                return set(str(x) for x in data)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return set()


def _save_processed_tracker(tracker_path: Path, processed: Set[str]) -> None:
    """Persist the processed comment IDs to disk.

    Keeps only the most recent 5000 IDs to prevent unbounded growth.
    Uses atomic write via temp file + rename.

    Args:
        tracker_path: Path to .jira-processed.json in instance dir.
        processed: Set of processed comment IDs.
    """
    try:
        from app.utils import atomic_write

        # Trim to most recent 5000 entries (arbitrary stable order)
        ids = sorted(processed, key=lambda x: int(x) if x.isdigit() else 0)[-5000:]
        atomic_write(tracker_path, json.dumps(ids, indent=2))
    except Exception as e:
        log.debug("Failed to save Jira processed tracker: %s", e)


def check_jira_already_processed(
    comment_id: str,
    processed_set: Set[str],
) -> bool:
    """Check if a Jira comment has already been processed.

    Checks both the in-memory BoundedSet and the caller-supplied
    persistent set (loaded from .jira-processed.json).

    Args:
        comment_id: The Jira comment ID.
        processed_set: Persistent processed IDs from tracker file.

    Returns:
        True if already processed.
    """
    str_id = str(comment_id)
    if str_id in _processed_comments:
        return True
    if str_id in processed_set:
        _processed_comments.add(str_id)
        return True
    return False


def mark_jira_comment_processed(comment_id: str, processed_set: Set[str]) -> None:
    """Mark a Jira comment as processed in both in-memory and persistent sets.

    Args:
        comment_id: The Jira comment ID.
        processed_set: The persistent processed set (mutated in-place).
    """
    str_id = str(comment_id)
    _processed_comments.add(str_id)
    processed_set.add(str_id)


def acknowledge_jira_comment(issue_key: str, command_name: str, base_url: str, auth_header: str) -> bool:
    """Post a brief acknowledgment reply on a Jira issue comment.

    Mirrors GitHub's 👍 reaction by posting a short reply comment.

    Note: posting this comment updates the issue's ``updated`` timestamp,
    which will cause ``_search_issues_with_comments`` to re-fetch the issue
    on the next polling cycle.  This is harmless (the bot won't self-trigger
    because the ack comment lacks an @mention), but does add extra API calls
    for the remainder of the ``max_age_hours`` window.

    Args:
        issue_key: Jira issue key (e.g. "PROJ-52372").
        command_name: The command being executed (e.g. "fix").
        base_url: Jira instance base URL (e.g. https://myorg.atlassian.net).
        auth_header: Basic auth header value.

    Returns:
        True if the comment was posted, False on error.
    """
    try:
        # ADF body with thumbs-up emoji + command acknowledgment
        body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "emoji",
                            "attrs": {
                                "shortName": ":thumbsup:",
                                "id": "1f44d",
                                "text": "\U0001f44d",
                            },
                        },
                        {
                            "type": "text",
                            "text": f" Mission queued: /{command_name}",
                        },
                    ],
                }],
            },
        }

        result = _jira_post(
            base_url, auth_header,
            f"/rest/api/3/issue/{issue_key}/comment",
            body,
        )
        return result is not None
    except Exception as e:
        log.debug("Failed to acknowledge Jira comment on %s: %s", issue_key, e)
        return False


def resolve_project_from_jira_key(issue_key: str, project_map: Dict[str, str]) -> Optional[str]:
    """Map a Jira issue key (e.g. FOO-123) to a Kōan project name.

    Args:
        issue_key: Full Jira issue key like "FOO-123".
        project_map: Jira project key -> Koan project name from projects.yaml.

    Returns:
        Kōan project name or None if not mapped.
    """
    if not issue_key or "-" not in issue_key:
        return None
    jira_project_key = issue_key.split("-")[0].upper()
    return project_map.get(jira_project_key)


def resolve_branch_from_jira_key(issue_key: str, branch_map: Dict[str, str]) -> Optional[str]:
    """Map a Jira issue key to a configured target branch.

    Args:
        issue_key: Full Jira issue key like "FOO-123".
        branch_map: Jira project key -> target branch from projects.yaml.

    Returns:
        Branch name or None if no branch is configured for this project key.
    """
    if not issue_key or "-" not in issue_key:
        return None
    jira_project_key = issue_key.split("-")[0].upper()
    return branch_map.get(jira_project_key)


def _search_issues_with_comments(
    base_url: str,
    auth_header: str,
    project_keys: List[str],
    since: datetime,
    max_issues: Optional[int] = None,
) -> List[dict]:
    """Search for Jira issues updated since a given time using JQL.

    Uses JQL to find recently-updated issues in the mapped projects.
    Paginates to handle large result sets, stopping once ``max_issues`` have
    been collected so callers can bound the total API cost.

    Args:
        base_url: Jira instance base URL.
        auth_header: Basic auth header value.
        project_keys: List of Jira project keys to search.
        since: Minimum updated timestamp.
        max_issues: Upper bound on the number of issues to return; pagination
            halts once this many issues have been collected. ``None`` means no
            cap (return everything).

    Returns:
        List of issue dicts from Jira API (at most ``max_issues`` when set).
    """
    if not project_keys:
        return []

    # Build JQL: project in (FOO, BAR) AND updated >= "YYYY-MM-DD HH:MM"
    # Jira JQL uses "YYYY-MM-DD HH:MM" format for datetime comparisons
    since_str = since.strftime("%Y-%m-%d %H:%M")
    # Validate project keys to prevent JQL injection (keys must be alphanumeric)
    _PROJECT_KEY_RE = re.compile(r'^[A-Z0-9]+$')
    safe_keys = [k for k in project_keys if _PROJECT_KEY_RE.match(k)]
    if not safe_keys:
        log.warning("Jira: no valid project keys after sanitization (got %s)", project_keys)
        return []
    project_in = ", ".join(f'"{k}"' for k in safe_keys)
    jql = f'project in ({project_in}) AND updated >= "{since_str}" ORDER BY updated DESC'

    issues: List[dict] = []
    max_results = 50
    next_page_token: Optional[str] = None

    while True:
        body: Dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "updated"],
        }
        if next_page_token is not None:
            body["nextPageToken"] = next_page_token

        data = _jira_post(base_url, auth_header, "/rest/api/3/search/jql", body)
        if not data or not isinstance(data, dict):
            break

        batch = data.get("issues", [])
        if not batch:
            break

        issues.extend(batch)

        if max_issues is not None and len(issues) >= max_issues:
            issues = issues[:max_issues]
            break

        if data.get("isLast", True):
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return issues


def _get_issue_comments(
    base_url: str,
    auth_header: str,
    issue_key: str,
    since: datetime,
) -> List[dict]:
    """Fetch comments on a Jira issue updated since the given time.

    Paginates through all comments on the issue.

    Args:
        base_url: Jira instance base URL.
        auth_header: Basic auth header value.
        issue_key: Jira issue key (e.g. "FOO-123").
        since: Minimum updated timestamp.

    Returns:
        List of comment dicts from Jira API.
    """
    comments = []
    start_at = 0
    max_results = 100

    while True:
        params = {
            "startAt": start_at,
            "maxResults": max_results,
            "orderBy": "created",
        }
        data = _jira_get(
            base_url, auth_header,
            f"/rest/api/3/issue/{issue_key}/comment",
            params,
        )
        if not data or not isinstance(data, dict):
            break

        batch = data.get("comments", [])
        if not batch:
            break

        for comment in batch:
            # Filter by updated time
            updated_str = comment.get("updated", "")
            if updated_str:
                try:
                    updated = datetime.fromisoformat(
                        updated_str.replace("Z", "+00:00")
                    )
                    if updated >= since:
                        comments.append(comment)
                except (ValueError, TypeError):
                    comments.append(comment)  # Include on parse error

        total = data.get("total", 0)
        start_at += len(batch)

        if start_at >= total or len(batch) < max_results:
            break

    return comments


def fetch_jira_issue(
    issue_key: str,
) -> Tuple[str, str, List[dict]]:
    """Fetch a Jira issue's title, description, and comments.

    Uses the Jira config from config.yaml to authenticate.

    Args:
        issue_key: Jira issue key (e.g. "PROJ-52372").

    Returns:
        Tuple of (title, body, comments) where comments is a list of
        dicts with "author" and "body" keys.

    Raises:
        RuntimeError: If Jira is not configured or the API call fails.
    """
    from app.jira_config import (
        get_jira_api_token,
        get_jira_base_url,
        get_jira_email,
        get_jira_enabled,
        validate_jira_config,
    )
    from app.utils import load_config

    config = load_config()
    if not get_jira_enabled(config):
        raise RuntimeError("Jira integration is not enabled in config.yaml")

    error = validate_jira_config(config)
    if error:
        raise RuntimeError(f"Jira config error: {error}")

    base_url = get_jira_base_url(config)
    email = get_jira_email(config)
    api_token = get_jira_api_token(config)
    auth_header = _make_auth_header(email, api_token)

    # Fetch the issue itself
    data = _jira_get(base_url, auth_header, f"/rest/api/3/issue/{issue_key}")
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"Failed to fetch Jira issue {issue_key}")

    fields = data.get("fields", {})
    title = fields.get("summary", "")

    # Preserve Markdown structure for plan/implementation skill context.
    desc_node = fields.get("description")
    body = _adf_to_markdown(desc_node) if desc_node else ""

    # Fetch all comments (no time filter — we want full context)
    all_comments = []
    start_at = 0
    max_results = 100

    while True:
        params = {
            "startAt": start_at,
            "maxResults": max_results,
            "orderBy": "created",
        }
        cdata = _jira_get(
            base_url, auth_header,
            f"/rest/api/3/issue/{issue_key}/comment",
            params,
        )
        if not cdata or not isinstance(cdata, dict):
            break

        batch = cdata.get("comments", [])
        if not batch:
            break

        for comment in batch:
            author_data = comment.get("author", {})
            author_name = (
                author_data.get("displayName")
                or author_data.get("emailAddress")
                or "unknown"
            )
            comment_body_node = comment.get("body")
            comment_text = _adf_to_markdown(comment_body_node) if comment_body_node else ""
            if comment_text.strip():
                entry = {
                    "author": author_name,
                    "body": comment_text,
                }
                if comment.get("updated"):
                    entry["updated"] = str(comment["updated"])
                all_comments.append(entry)

        total = cdata.get("total", 0)
        start_at += len(batch)
        if start_at >= total or len(batch) < max_results:
            break

    return title, body, all_comments


def fetch_jira_issue_summary(
    issue_key: str,
    timeout: int = 30,
) -> Tuple[str, str]:
    """Fetch only a Jira issue's title and description (no comments).

    A lightweight counterpart to :func:`fetch_jira_issue` for callers that
    need just the summary/description. It issues a single GET scoped to the
    ``summary,description`` fields, so it never paginates comments and makes
    exactly one bounded round-trip.

    Args:
        issue_key: Jira issue key (e.g. "PROJ-52372").
        timeout: Per-request socket timeout in seconds.

    Returns:
        Tuple of (title, body).

    Raises:
        RuntimeError: If Jira is not configured or the API call fails.
    """
    base_url, auth_header = _jira_auth_from_config()
    data = _jira_get(
        base_url,
        auth_header,
        f"/rest/api/3/issue/{issue_key}",
        {"fields": "summary,description"},
        timeout=timeout,
    )
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"Failed to fetch Jira issue {issue_key}")

    fields = data.get("fields", {})
    title = fields.get("summary", "")
    desc_node = fields.get("description")
    body = _adf_to_text(desc_node) if desc_node else ""
    return title, body


def _jira_auth_from_config() -> Tuple[str, str]:
    """Return (base_url, auth_header) using config.yaml Jira credentials."""
    from app.jira_config import (
        get_jira_api_token,
        get_jira_base_url,
        get_jira_email,
        get_jira_enabled,
        validate_jira_config,
    )
    from app.utils import load_config

    config = load_config()
    if not get_jira_enabled(config):
        raise RuntimeError("Jira integration is not enabled in config.yaml")
    error = validate_jira_config(config)
    if error:
        raise RuntimeError(f"Jira config error: {error}")
    base_url = get_jira_base_url(config)
    email = get_jira_email(config)
    api_token = get_jira_api_token(config)
    return base_url, _make_auth_header(email, api_token)


def jira_add_comment(issue_key: str, body_text: str) -> bool:
    """Post a Markdown comment as native Jira ADF."""
    base_url, auth_header = _jira_auth_from_config()
    result = _jira_post(
        base_url,
        auth_header,
        f"/rest/api/3/issue/{issue_key}/comment",
        {"body": markdown_to_adf(body_text)},
    )
    return result is not None


class JiraCommentFetchError(RuntimeError):
    """Raised when Jira's comment listing could not be retrieved."""


def _list_comments_result(issue_key: str) -> Tuple[bool, List[dict]]:
    """Fetch all comments for an issue, reporting whether the API call worked.

    Returns ``(ok, comments)``. ``ok`` is False when Jira did not answer with a
    usable payload, which callers must not confuse with "the issue has no
    comments" — both look like an empty list.
    """
    base_url, auth_header = _jira_auth_from_config()
    all_comments: List[dict] = []
    start_at = 0
    max_results = 100

    while True:
        params = {
            "startAt": start_at,
            "maxResults": max_results,
            "orderBy": "created",
        }
        data = _jira_get(
            base_url,
            auth_header,
            f"/rest/api/3/issue/{issue_key}/comment",
            params,
        )
        if data is None or not isinstance(data, dict):
            return False, all_comments

        batch = data.get("comments", [])
        if not batch:
            break

        for comment in batch:
            comment_id = str(comment.get("id", "")).strip()
            if not comment_id:
                continue
            body_node = comment.get("body")
            body_text = _adf_to_text(body_node) if body_node else ""
            all_comments.append({"id": comment_id, "body": body_text})

        total = data.get("total", 0)
        start_at += len(batch)
        if start_at >= total or len(batch) < max_results:
            break

    return True, all_comments


def jira_list_comments(issue_key: str) -> List[dict]:
    """Fetch all comments for a Jira issue (id + extracted plain text body).

    Degrades to ``[]`` when the API call fails. Callers that decide whether to
    create a comment based on the result want :func:`jira_list_comments_checked`
    instead — a silent ``[]`` there means posting a duplicate.
    """
    return _list_comments_result(issue_key)[1]


def jira_list_comments_checked(issue_key: str) -> List[dict]:
    """Like :func:`jira_list_comments`, but raises instead of degrading to ``[]``.

    Raises:
        JiraCommentFetchError: the comment listing could not be retrieved.
    """
    ok, comments = _list_comments_result(issue_key)
    if not ok:
        raise JiraCommentFetchError(
            f"Could not list comments for {issue_key}"
        )
    return comments


def jira_edit_comment(issue_key: str, comment_id: str, body_text: str) -> bool:
    """Edit a Jira issue comment body."""
    if not str(comment_id).strip():
        return False
    base_url, auth_header = _jira_auth_from_config()
    result = _jira_put(
        base_url,
        auth_header,
        f"/rest/api/3/issue/{issue_key}/comment/{comment_id}",
        {"body": markdown_to_adf(body_text)},
    )
    return result is not None


def jira_create_issue(
    project_key: str,
    title: str,
    body_text: str,
    issue_type: str = "Task",
) -> str:
    """Create a Jira issue and return its browse URL."""
    if not re.match(r"^[A-Z0-9]+$", project_key or ""):
        raise RuntimeError(f"Invalid Jira project key: {project_key!r}")

    base_url, auth_header = _jira_auth_from_config()
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": title,
            # Markdown is converted to rich ADF for issue descriptions and
            # comments so tracker output keeps its intended structure.
            "description": markdown_to_adf(body_text),
            "issuetype": {"name": issue_type or "Task"},
        }
    }
    result = _jira_post(base_url, auth_header, "/rest/api/3/issue", payload)
    if not isinstance(result, dict) or not result.get("key"):
        raise RuntimeError(f"Failed to create Jira issue in {project_key}")
    return f"{base_url}/browse/{result['key']}"


def jira_update_issue_description(issue_key: str, body_text: str) -> bool:
    """Rewrite a Jira issue's description with rich ADF. False on failure.

    Used to resolve SUB-N cross-references after all sibling issues exist.
    Never raises — a transport failure returns False so callers can degrade.
    """
    if not str(issue_key).strip():
        return False
    base_url, auth_header = _jira_auth_from_config()
    # _jira_put returns {} on an empty successful body, None on error.
    result = _jira_put(
        base_url,
        auth_header,
        f"/rest/api/3/issue/{issue_key}",
        {"fields": {"description": markdown_to_adf(body_text)}},
    )
    return result is not None


def jira_link_issues(
    outward_key: str,
    inward_key: str,
    link_type: str = "Relates",
) -> bool:
    """Create a native Jira issue link ``outward`` → ``inward``. False on failure.

    ``outward_key`` is typically the master tracking issue and ``inward_key`` a
    sub-issue; the default ``"Relates"`` link type is always present in Jira. The
    issue-link endpoint returns ``201`` with an empty body, so this checks the
    HTTP status directly rather than relying on a parsed JSON return. Never
    raises — a failure returns False so linking can degrade non-fatally.
    """
    if not str(outward_key).strip() or not str(inward_key).strip():
        return False
    base_url, auth_header = _jira_auth_from_config()
    payload = {
        "type": {"name": link_type or "Relates"},
        "outwardIssue": {"key": outward_key},
        "inwardIssue": {"key": inward_key},
    }
    try:
        import urllib.request

        req = urllib.request.Request(
            base_url + "/rest/api/3/issueLink",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Authorization", auth_header)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.warning("Jira issue link %s -> %s failed: %s", outward_key, inward_key, e)
        return False


def jira_search_issues(
    project_key: str,
    text: str,
    limit: int = 5,
) -> List[dict]:
    """Search recent open Jira issues for roughly matching text."""
    if not re.match(r"^[A-Z0-9]+$", project_key or ""):
        return []
    base_url, auth_header = _jira_auth_from_config()

    # JQL injection safety: `text` is sanitized to tokens matching
    # [A-Za-z][A-Za-z0-9_-]{2,} — no quote, backslash, or whitespace within a
    # token. The joined `query` therefore cannot break out of the surrounding
    # `"..."` literal. If the token regex is ever widened, replace this with a
    # proper JQL escape or a parameterized search call.
    words = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b", text or "")
    query = " ".join(words[:4])
    if query:
        jql = (
            f'project = "{project_key}" AND statusCategory != Done '
            f'AND text ~ "{query}" ORDER BY updated DESC'
        )
    else:
        jql = (
            f'project = "{project_key}" AND statusCategory != Done '
            "ORDER BY updated DESC"
        )
    result = _jira_post(
        base_url,
        auth_header,
        "/rest/api/3/search/jql",
        {"jql": jql, "maxResults": max(1, limit), "fields": ["summary"]},
    )
    if not isinstance(result, dict):
        return []
    issues = result.get("issues", [])
    if not isinstance(issues, list):
        return []
    matches = []
    for issue in issues:
        key = issue.get("key", "")
        if not key:
            continue
        fields = issue.get("fields", {}) or {}
        matches.append({
            "key": key,
            "title": fields.get("summary", ""),
            "url": f"{base_url}/browse/{key}",
        })
    return matches


def fetch_jira_mentions(
    config: dict,
    project_map: Dict[str, str],
    since_iso: Optional[str] = None,
) -> JiraFetchResult:
    """Fetch Jira comments that @mention the bot.

    Searches recently-updated issues in mapped projects, fetches their
    comments, and returns those containing @bot mentions.

    Args:
        config: Global config dict (from config.yaml).
        project_map: Jira project key → Kōan project name mapping.
        since_iso: ISO 8601 timestamp to search from. If None, uses max_age_hours.

    Returns:
        JiraFetchResult with list of mention dicts.
    """
    from app.jira_config import (
        get_jira_api_token,
        get_jira_base_url,
        get_jira_email,
        get_jira_max_age_hours,
        get_jira_max_issues_per_cycle,
        get_jira_nickname,
    )

    base_url = get_jira_base_url(config)
    email = get_jira_email(config)
    api_token = get_jira_api_token(config)
    nickname = get_jira_nickname(config)
    max_age_hours = get_jira_max_age_hours(config)

    if not all([base_url, email, api_token, nickname]):
        log.debug("Jira: missing config (base_url/email/api_token/nickname), skipping")
        return JiraFetchResult([])

    auth_header = _make_auth_header(email, api_token)
    project_keys = sorted(project_map.keys())

    if not project_keys:
        log.debug(
            "Jira: no project keys configured in projects.yaml issue_tracker, "
            "skipping"
        )
        return JiraFetchResult([])

    # Determine time window
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            since = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    # Search for recently-updated issues. Each issue inside the cap triggers
    # its own GET /comment API call, so this cap directly bounds cold-start
    # API consumption. The cap is pushed into _search_issues_with_comments so
    # pagination halts as soon as we have enough issues — both the search and
    # the per-issue comment fetches stay bounded. Default (200) suits
    # multi-project deployments with 24h max_age; configurable via
    # ``jira.max_issues_per_cycle`` so smaller instances can tighten and
    # larger ones can loosen. Steady-state polls narrow the window via
    # ``since_iso`` so the cap rarely binds there.
    max_issues_per_cycle = get_jira_max_issues_per_cycle(config)
    issues = _search_issues_with_comments(
        base_url, auth_header, project_keys, since,
        max_issues=max_issues_per_cycle,
    )
    log.info(
        "Jira: search since %s returned %d issue(s) (cap=%d)",
        since.strftime("%Y-%m-%d %H:%M"), len(issues), max_issues_per_cycle,
    )
    if not issues:
        return JiraFetchResult([])

    if len(issues) >= max_issues_per_cycle:
        log.warning(
            "Jira: hit cap of %d issues this cycle; older issues beyond the "
            "cap were not inspected and any mentions on them will be missed "
            "until a future poll picks them up — raise jira.max_issues_per_cycle, "
            "tighten max_age_hours, or shorten check_interval_seconds",
            max_issues_per_cycle,
        )

    # Collect @mention comments from all issues
    mentions = []
    bot_mention_lower = f"@{nickname}".lower()

    for issue in issues:
        issue_key = issue.get("key", "")
        if not issue_key:
            continue

        # Determine Kōan project for this issue
        project_name = resolve_project_from_jira_key(issue_key, project_map)
        if not project_name:
            log.debug(
                "Jira: issue %s is not registered to this instance, skipping",
                issue_key,
            )
            continue

        comments = _get_issue_comments(base_url, auth_header, issue_key, since)
        for comment in comments:
            body = comment.get("body", "")
            text = _extract_comment_text(body)
            if bot_mention_lower not in text.lower():
                continue

            # Build a normalized mention dict for the command handler
            mentions.append({
                "comment_id": str(comment.get("id", "")),
                "issue_key": issue_key,
                "project_name": project_name,
                "author_email": comment.get("author", {}).get("emailAddress", ""),
                "author_name": comment.get("author", {}).get("displayName", ""),
                "body_text": text,
                "updated": comment.get("updated", ""),
                "issue_url": f"{base_url}/browse/{issue_key}",
                "comment_url": (
                    f"{base_url}/browse/{issue_key}"
                    f"?focusedCommentId={comment.get('id', '')}"
                ),
            })

    if mentions:
        log.debug("Jira: found %d @%s mention(s)", len(mentions), nickname)
    else:
        log.debug("Jira: no @%s mentions found", nickname)

    return JiraFetchResult(mentions)
