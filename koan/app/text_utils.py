"""Kōan — Text utilities for messaging delivery.

Shared helpers for cleaning CLI output and stripping markdown formatting
before sending to messaging providers (Telegram, Slack, etc.).

These functions ensure consistent text processing across all message paths:
- Chat responses (awake.py)
- Outbox formatting (format_outbox.py)
- AI runner responses (ai_runner.py)
"""

import re

# Markdown symbols to strip (order matters: longer patterns first)
_MARKDOWN_SYMBOLS = ("```", "**", "__", "~~")

# CLI error patterns to filter from responses
_CLI_ERROR_RE = re.compile(r'^Error:.*max turns', re.IGNORECASE)

# Heading markers: # through ######
_HEADING_RE = re.compile(r'^#{1,6}\s+', re.MULTILINE)

# Default max message length for smartphone readability
DEFAULT_MAX_LENGTH = 2000


def strip_markdown(text: str) -> str:
    """Strip markdown formatting artifacts from text.

    Removes code fences, bold, underline, strikethrough, and heading markers.
    Designed for plain-text messaging (Telegram, Slack).

    Args:
        text: Raw text potentially containing markdown.

    Returns:
        Text with markdown artifacts removed.
    """
    for symbol in _MARKDOWN_SYMBOLS:
        text = text.replace(symbol, "")
    text = _HEADING_RE.sub("", text)
    return text


def expand_github_refs(text: str, github_url: str) -> str:
    """Expand bare #123 GitHub references to full clickable URLs.

    Converts patterns like ``#123`` or ``PR #42`` so the number links to
    the project on GitHub.  Already-expanded references (where the number
    is immediately followed by its URL in parentheses) are left untouched.

    Args:
        text: Message text potentially containing ``#N`` references.
        github_url: Base GitHub URL for the project, e.g.
            ``https://github.com/owner/repo``.

    Returns:
        Text with bare ``#N`` references expanded to include a full URL.
    """
    if not text or not github_url:
        return text

    base = github_url.rstrip("/")

    def _replace(m):
        number = m.group(1)
        # Don't expand if already followed by the full URL in parens
        end = m.end()
        remaining = text[end:end + len(base) + 30]
        if remaining.startswith(f" ({base}/"):
            return m.group(0)
        return f"#{number} ({base}/issues/{number})"

    return re.sub(r'(?<![/\w])#(\d+)\b', _replace, text)


def extract_project_from_message(text: str) -> str:
    """Extract a project name from a message's tag markers.

    Recognises ``[project_name]`` (typically after an emoji prefix like
    ``🏁 [koan]``) and ``[project:project_name]`` patterns.

    Returns:
        The project name, or empty string if none found.
    """
    # Match [project:name] first (more specific)
    m = re.search(r'\[project:(\w[\w.-]*)\]', text)
    if m:
        return m.group(1)
    # Match emoji-prefixed [name] pattern (e.g. "🏁 [koan]")
    m = re.search(r'(?:^|\s)\[(\w[\w.-]*)\]', text)
    if m:
        return m.group(1)
    return ""


def clean_cli_response(text: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Clean Claude CLI output for messaging delivery.

    Strips CLI error artifacts, markdown formatting, and truncates for
    smartphone reading. Used by chat responses and AI runner output.

    Args:
        text: Raw CLI stdout text.
        max_length: Maximum character length (default 2000).

    Returns:
        Cleaned, truncated plain text.
    """
    lines = text.splitlines()
    lines = [line for line in lines if not _CLI_ERROR_RE.match(line)]
    cleaned = "\n".join(lines).strip()
    cleaned = strip_markdown(cleaned)
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length - 3] + "..."
    return cleaned.strip()
