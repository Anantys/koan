"""HTML comment markers for stateful PR interactions.

Defines named string constants for each marker type and string manipulation
helpers for reading and writing tagged sections in GitHub comment bodies.

All consumers (review_runner.py, future incremental review, progress
indicators) import the same constants — no drift between writer and reader.

Tag format: ``<!-- koan-{name} -->``
GitHub's 65536-char comment limit constrains total comment size; keep
marker strings short.  No ``--`` inside comment bodies (invalid HTML).
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Marker constants
# ---------------------------------------------------------------------------

SUMMARY_TAG = "<!-- koan-summary -->"
"""Top-level marker that identifies the bot's summary comment on a PR."""

COMMIT_IDS_START = "<!-- koan-commits-start -->"
COMMIT_IDS_END = "<!-- koan-commits-end -->"
"""Hidden block storing reviewed commit SHAs (one per line)."""

RAW_SUMMARY_START = "<!-- koan-raw-summary-start -->"
RAW_SUMMARY_END = "<!-- koan-raw-summary-end -->"
"""Hidden block caching the raw changeset summary for prompt injection."""

SHORT_SUMMARY_START = "<!-- koan-short-summary-start -->"
SHORT_SUMMARY_END = "<!-- koan-short-summary-end -->"
"""Short summary block injected into future prompts."""

RELEASE_NOTES_START = "<!-- koan-release-notes-start -->"
RELEASE_NOTES_END = "<!-- koan-release-notes-end -->"
"""Auto-generated release notes block in the PR description."""

IN_PROGRESS_START = "<!-- koan-in-progress-start -->"
IN_PROGRESS_END = "<!-- koan-in-progress-end -->"
"""Temporary placeholder inserted while a review is actively running."""


# ---------------------------------------------------------------------------
# String manipulation helpers
# ---------------------------------------------------------------------------

def extract_between_markers(body: str, start: str, end: str) -> Optional[str]:
    """Return the text between ``start`` and ``end`` markers.

    Args:
        body: The full comment body to search.
        start: Opening marker string (e.g. ``COMMIT_IDS_START``).
        end: Closing marker string (e.g. ``COMMIT_IDS_END``).

    Returns:
        The text between the markers (stripped), or ``None`` if either
        marker is absent or the start marker appears after the end marker.
    """
    start_idx = body.find(start)
    if start_idx == -1:
        return None
    end_idx = body.find(end, start_idx + len(start))
    if end_idx == -1:
        return None
    return body[start_idx + len(start):end_idx]


def remove_section(body: str, start: str, end: str) -> str:
    """Strip a tagged block (including the markers) from ``body``.

    If the markers are absent the original body is returned unchanged.
    Leading/trailing whitespace around the removed block is preserved to
    avoid creating double blank lines.

    Args:
        body: The full comment body.
        start: Opening marker string.
        end: Closing marker string.

    Returns:
        Body with the tagged section removed.
    """
    start_idx = body.find(start)
    if start_idx == -1:
        return body
    end_idx = body.find(end, start_idx + len(start))
    if end_idx == -1:
        return body
    return body[:start_idx] + body[end_idx + len(end):]


def wrap_section(content: str, start: str, end: str) -> str:
    """Wrap ``content`` between ``start`` and ``end`` marker tags.

    Args:
        content: The text to wrap.
        start: Opening marker string.
        end: Closing marker string.

    Returns:
        ``"{start}{content}{end}"``
    """
    return f"{start}{content}{end}"


def replace_section(body: str, start: str, end: str, new_content: str) -> str:
    """Idempotent upsert of a tagged block in ``body``.

    If the block already exists it is replaced; otherwise the block is
    appended to the end of ``body``.

    Args:
        body: The full comment body.
        start: Opening marker string.
        end: Closing marker string.
        new_content: New content to place between the markers.

    Returns:
        Updated body with the tagged section containing ``new_content``.
    """
    new_block = wrap_section(new_content, start, end)
    start_idx = body.find(start)
    if start_idx == -1:
        # Block absent — append
        return body + new_block
    end_idx = body.find(end, start_idx + len(start))
    if end_idx == -1:
        # Malformed (start present, end absent) — append block, leave orphan
        return body + new_block
    return body[:start_idx] + new_block + body[end_idx + len(end):]
