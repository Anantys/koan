#!/usr/bin/env python3
"""
Extract a short summary from today's journal for post-mission outbox notification.

Usage: python mission_summary.py <instance_dir> <project_name> [max_chars]

Reads the latest journal entry for the given project, extracts the last
## section, and prints a 2-3 line summary suitable for Telegram.
"""

import re
import sys
from datetime import date
from pathlib import Path


def extract_latest_section(journal_text: str) -> str:
    """Extract the last ## section from journal text."""
    sections = re.split(r'^## ', journal_text, flags=re.MULTILINE)
    if len(sections) < 2:
        return journal_text.strip()
    # Last section (with its heading restored)
    last = "## " + sections[-1]
    return last.strip()


def summarize_section(section: str, max_chars: int = 400) -> str:
    """Produce a short summary from a journal section.

    Strategy: take the heading + first non-empty paragraph.
    """
    lines = section.splitlines()
    if not lines:
        return ""

    # Extract heading
    heading = ""
    body_lines = []
    for line in lines:
        if line.startswith("## ") and not heading:
            heading = line[3:].strip()
        elif line.strip():
            body_lines.append(line.strip())

    # Take first meaningful lines (skip code blocks, metadata)
    summary_lines = []
    for line in body_lines:
        if line.startswith("```"):
            break
        if line.startswith("---"):
            continue
        summary_lines.append(line)
        if len("\n".join(summary_lines)) > max_chars:
            break
        if len(summary_lines) >= 4:
            break

    summary = "\n".join(summary_lines)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "..."

    if heading:
        return f"{heading}\n\n{summary}"
    return summary


def get_mission_summary(instance_dir: str, project_name: str, max_chars: int = 400) -> str:
    """Get a summary of the latest journal entry for a project."""
    from app.journal import get_journal_file
    instance = Path(instance_dir)

    journal_file = get_journal_file(instance, date.today(), project_name)
    if not journal_file.exists():
        return ""

    content = journal_file.read_text().strip()
    if not content:
        return ""

    section = extract_latest_section(content)
    return summarize_section(section, max_chars)


# Error signal patterns — ordered by specificity (most specific first)
_ERROR_PATTERNS = [
    re.compile(r"(?:FAIL|FAILED|failures?)\b.*", re.IGNORECASE),
    re.compile(r"Error:.*", re.IGNORECASE),
    re.compile(r"Traceback \(most recent call last\).*"),
    re.compile(r"(?:exit code|returncode)[:\s]+\d+", re.IGNORECASE),
    re.compile(r"Rebase conflict.*", re.IGNORECASE),
    re.compile(r"Permission denied.*", re.IGNORECASE),
    re.compile(r"fatal:.*"),
]

# Lines to ignore even if they match error patterns
_ERROR_NOISE = re.compile(
    r"Error:.*max turns|error_context|error handling|"
    r"error.*test|test.*error|fix.*error|error.*fix",
    re.IGNORECASE,
)


def extract_error_lines(text: str, max_lines: int = 5) -> list[str]:
    """Extract lines containing error signals from text.

    Returns up to *max_lines* error-relevant lines, preserving order.
    """
    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ERROR_NOISE.search(stripped):
            continue
        for pattern in _ERROR_PATTERNS:
            if pattern.search(stripped):
                hits.append(stripped)
                break
        if len(hits) >= max_lines:
            break
    return hits


def get_failure_context(
    instance_dir: str, project_name: str, max_chars: int = 300,
) -> str:
    """Extract error context from the latest journal entry for a failed mission.

    Scans the most recent journal section for error patterns (stack traces,
    failure messages, non-zero exit codes) and returns a brief summary
    suitable for inclusion in Telegram notifications.

    Returns empty string if no error context is found.
    """
    from app.journal import get_journal_file
    instance = Path(instance_dir)

    journal_file = get_journal_file(instance, date.today(), project_name)
    if not journal_file.exists():
        return ""

    content = journal_file.read_text().strip()
    if not content:
        return ""

    section = extract_latest_section(content)
    error_lines = extract_error_lines(section)
    if not error_lines:
        return ""

    result = "\n".join(error_lines)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit("\n", 1)[0]
        if not result:
            result = error_lines[0][:max_chars]
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: mission_summary.py <instance_dir> <project_name> [max_chars]", file=sys.stderr)
        sys.exit(1)

    instance_dir = sys.argv[1]
    project_name = sys.argv[2]
    max_chars = int(sys.argv[3]) if len(sys.argv) > 3 else 400

    summary = get_mission_summary(instance_dir, project_name, max_chars)
    if summary:
        print(summary)
