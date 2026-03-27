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


def _read_current_project_file() -> str:
    """Read the current project name from .koan-project, if available."""
    import os
    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return ""
    try:
        from pathlib import Path
        return Path(koan_root, ".koan-project").read_text().strip()
    except (OSError, ValueError):
        return ""


def _resolve_project_for_refs(*texts: str) -> str:
    """Resolve a project name for GitHub ref expansion.

    Tries each strategy in order:
    1. extract_project_from_message on each provided text
    2. .koan-project file (written by run.py during mission execution)
    3. KOAN_CURRENT_PROJECT env var

    Returns the project name, or empty string if none found.
    """
    import os

    for text in texts:
        if text:
            name = extract_project_from_message(text)
            if name:
                return name

    name = _read_current_project_file()
    if name and name != "unknown":
        return name

    return os.environ.get("KOAN_CURRENT_PROJECT", "")


def expand_github_refs_auto(text: str, *hint_texts: str) -> str:
    """Expand bare #123 GitHub refs with auto-detected project context.

    Uses :func:`_resolve_project_for_refs` to detect the project, then
    looks up its GitHub URL and expands refs.  If the project or URL
    cannot be determined, returns the text unchanged.

    Args:
        text: The message text to expand refs in.
        *hint_texts: Additional texts to search for project context
            (e.g. the user's original message).
    """
    if not text:
        return text

    # Quick check: no # in text means nothing to expand
    if "#" not in text:
        return text

    project_name = _resolve_project_for_refs(text, *hint_texts)
    if not project_name:
        return text

    try:
        from app.projects_merged import get_github_url
        github_url = get_github_url(project_name)
    except Exception as e:
        import sys
        print(f"[text_utils] GitHub URL lookup failed: {e}", file=sys.stderr)
        return text

    if not github_url:
        return text

    return expand_github_refs(text, github_url)


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
